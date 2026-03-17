#!/usr/bin/env python3
"""
Hyperliquid Address Position Tracker - Telegram Bot
=====================================================
Monitors a specific address on Hyperliquid and sends Telegram alerts
when positions are opened, modified, or closed.

Uses a hybrid approach:
- WebSocket subscription for real-time fill notifications
- Periodic REST polling of clearinghouseState for position snapshots
"""

import asyncio
import json
import logging
import os
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone

import aiohttp
import websockets

# ─── Configuration ──────────────────────────────────────────────────────────
CONFIG = {
    # Hyperliquid API
    "HL_WS_URL": "wss://api.hyperliquid.xyz/ws",
    "HL_REST_URL": "https://api.hyperliquid.xyz/info",
    
    # Address to track
    "TARGET_ADDRESS": "0xb322c1b811eaf8b3840133a2fc936f4c7185d51f",
    "TARGET_LABEL": "oren-hl",  # Friendly name for alerts
    
    # Telegram - set via environment variables or edit here
    "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", "8578516714:AAFajR3rEJwLFqWct8OEWJG4A_e_hxkRheo"),
    "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID", "1454298447"),
    
    # Polling interval for position snapshots (seconds)
    "POLL_INTERVAL": 5,
    
    # Reconnect settings
    "RECONNECT_DELAY": 5,
    "MAX_RECONNECT_DELAY": 60,
}

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hl-tracker")


# ─── Telegram Notifier ──────────────────────────────────────────────────────
class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}"
    
    async def send_message(self, text: str, parse_mode: str = "HTML"):
        url = f"{self.api_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.error(f"Telegram API error {resp.status}: {body}")
                    else:
                        log.info("Telegram notification sent")
        except Exception as e:
            log.error(f"Failed to send Telegram message: {e}")
    
    async def send_startup_message(self):
        addr = CONFIG["TARGET_ADDRESS"]
        msg = (
            f"🟢 <b>Hyperliquid Tracker Started</b>\n\n"
            f"👤 Tracking: <b>{CONFIG['TARGET_LABEL']}</b>\n"
            f"🔗 Address: <code>{addr[:10]}...{addr[-8:]}</code>\n"
            f"🌐 <a href='https://hypurrscan.io/address/{addr}'>View on Hypurrscan</a>\n\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        await self.send_message(msg)


# ─── Position Tracker ───────────────────────────────────────────────────────
class PositionTracker:
    """Tracks position changes by comparing snapshots."""
    
    def __init__(self):
        self.positions: dict = {}  # coin -> position data
        self.initialized = False
    
    def _parse_positions(self, clearinghouse_state: dict) -> dict:
        """Parse clearinghouseState into a clean positions dict."""
        positions = {}
        for ap in clearinghouse_state.get("assetPositions", []):
            pos = ap.get("position", {})
            coin = pos.get("coin", "")
            if not coin:
                continue
            
            szi = float(pos.get("szi", "0"))
            if szi == 0:
                continue
            
            positions[coin] = {
                "coin": coin,
                "szi": szi,  # signed size (+ = long, - = short)
                "size": abs(szi),
                "side": "LONG" if szi > 0 else "SHORT",
                "entry_px": pos.get("entryPx", "?"),
                "position_value": pos.get("positionValue", "?"),
                "unrealized_pnl": pos.get("unrealizedPnl", "?"),
                "liquidation_px": pos.get("liquidationPx", "?"),
                "leverage": pos.get("leverage", {}),
                "return_on_equity": pos.get("returnOnEquity", "?"),
                "margin_used": pos.get("marginUsed", "?"),
            }
        return positions
    
    def _side_emoji(self, side: str) -> str:
        return "🟢" if side == "LONG" else "🔴"
    
    def process_snapshot(self, clearinghouse_state: dict) -> list:
        """Process a clearinghouseState snapshot and return alert messages."""
        new_positions = self._parse_positions(clearinghouse_state)
        alerts = []
        addr = CONFIG["TARGET_ADDRESS"]
        label = CONFIG["TARGET_LABEL"]
        link = f"https://hypurrscan.io/address/{addr}"
        
        if not self.initialized:
            self.initialized = True
            self.positions = deepcopy(new_positions)
            
            if new_positions:
                lines = [f"📋 <b>{label} — Current Positions:</b>\n"]
                for coin, pos in new_positions.items():
                    emoji = self._side_emoji(pos["side"])
                    lev = pos["leverage"]
                    lev_str = f"{lev.get('value', '?')}x {lev.get('type', '')}" if isinstance(lev, dict) else str(lev)
                    lines.append(
                        f"  {emoji} <b>{coin}</b> {pos['side']}\n"
                        f"    Size: {pos['size']} | Entry: ${pos['entry_px']}\n"
                        f"    Value: ${pos['position_value']} | uPnL: ${pos['unrealized_pnl']}\n"
                        f"    Leverage: {lev_str} | Liq: ${pos['liquidation_px']}"
                    )
                alerts.append("\n".join(lines))
            else:
                alerts.append(f"📋 <b>{label}</b> — No open positions.")
            
            return alerts
        
        # Compare with previous snapshot
        all_coins = set(list(self.positions.keys()) + list(new_positions.keys()))
        
        for coin in all_coins:
            old = self.positions.get(coin)
            new = new_positions.get(coin)
            
            old_size = old["size"] if old else 0
            old_side = old["side"] if old else None
            new_size = new["size"] if new else 0
            new_side = new["side"] if new else None
            
            # Skip if no meaningful change (allow small floating point diffs)
            if old and new and abs(old_size - new_size) < 0.000001 and old_side == new_side:
                continue
            
            # New position opened
            if old is None and new is not None:
                emoji = self._side_emoji(new["side"])
                lev = new["leverage"]
                lev_str = f"{lev.get('value', '?')}x {lev.get('type', '')}" if isinstance(lev, dict) else str(lev)
                alert = (
                    f"🚨 <b>NEW POSITION — {label}</b>\n\n"
                    f"{emoji} Market: <b>{coin}</b>\n"
                    f"📍 Side: {new['side']}\n"
                    f"📐 Size: {new['size']}\n"
                    f"💰 Entry: ${new['entry_px']}\n"
                    f"💵 Value: ${new['position_value']}\n"
                    f"⚡ Leverage: {lev_str}\n"
                    f"⚠️ Liq Price: ${new['liquidation_px']}\n\n"
                    f"🔗 <a href='{link}'>Hypurrscan</a>"
                )
                alerts.append(alert)
            
            # Position closed
            elif old is not None and new is None:
                emoji = self._side_emoji(old["side"])
                alert = (
                    f"✅ <b>POSITION CLOSED — {label}</b>\n\n"
                    f"{emoji} Market: <b>{coin}</b>\n"
                    f"📍 Was: {old['side']}\n"
                    f"📐 Size was: {old['size']}\n"
                    f"💰 Entry was: ${old['entry_px']}\n\n"
                    f"🔗 <a href='{link}'>Hypurrscan</a>"
                )
                alerts.append(alert)
            
            # Direction flipped
            elif old_side != new_side:
                alert = (
                    f"🔄 <b>POSITION FLIPPED — {label}</b>\n\n"
                    f"📌 Market: <b>{coin}</b>\n"
                    f"📍 {self._side_emoji(old_side)} {old_side} → {self._side_emoji(new_side)} {new_side}\n"
                    f"📐 Size: {old['size']} → {new['size']}\n"
                    f"💰 Entry: ${new['entry_px']}\n"
                    f"💵 Value: ${new['position_value']}\n"
                    f"⚠️ Liq: ${new['liquidation_px']}\n\n"
                    f"🔗 <a href='{link}'>Hypurrscan</a>"
                )
                alerts.append(alert)
            
            # Size increased
            elif new_size > old_size * 1.001:  # >0.1% change to avoid noise
                diff = new_size - old_size
                emoji = self._side_emoji(new["side"])
                alert = (
                    f"⬆️ <b>POSITION INCREASED — {label}</b>\n\n"
                    f"{emoji} Market: <b>{coin}</b> {new['side']}\n"
                    f"📐 Size: {old['size']:.6f} → {new['size']:.6f} (+{diff:.6f})\n"
                    f"💰 Avg Entry: ${new['entry_px']}\n"
                    f"💵 Value: ${new['position_value']}\n"
                    f"📈 uPnL: ${new['unrealized_pnl']}\n"
                    f"⚠️ Liq: ${new['liquidation_px']}\n\n"
                    f"🔗 <a href='{link}'>Hypurrscan</a>"
                )
                alerts.append(alert)
            
            # Size decreased (partial close)
            elif new_size < old_size * 0.999:  # >0.1% change
                diff = old_size - new_size
                emoji = self._side_emoji(new["side"])
                alert = (
                    f"⬇️ <b>PARTIAL CLOSE — {label}</b>\n\n"
                    f"{emoji} Market: <b>{coin}</b> {new['side']}\n"
                    f"📐 Size: {old['size']:.6f} → {new['size']:.6f} (-{diff:.6f})\n"
                    f"💰 Avg Entry: ${new['entry_px']}\n"
                    f"💵 Value: ${new['position_value']}\n"
                    f"📈 uPnL: ${new['unrealized_pnl']}\n"
                    f"⚠️ Liq: ${new['liquidation_px']}\n\n"
                    f"🔗 <a href='{link}'>Hypurrscan</a>"
                )
                alerts.append(alert)
        
        self.positions = deepcopy(new_positions)
        return alerts


# ─── REST Poller ────────────────────────────────────────────────────────────
async def fetch_clearinghouse_state(session: aiohttp.ClientSession) -> dict:
    """Fetch current positions via REST API (default + HIP-3 dexes)."""
    # Query default dex
    payload_default = {
        "type": "clearinghouseState",
        "user": CONFIG["TARGET_ADDRESS"],
    }
    # Query xyz dex (HIP-3 assets like CL, etc.)
    payload_xyz = {
        "type": "clearinghouseState",
        "user": CONFIG["TARGET_ADDRESS"],
        "dex": "xyz",
    }
    
    combined = {"assetPositions": []}
    
    for payload in [payload_default, payload_xyz]:
        try:
            async with session.post(
                CONFIG["HL_REST_URL"],
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    combined["assetPositions"].extend(data.get("assetPositions", []))
                    # Keep margin summary from first response
                    if "marginSummary" not in combined:
                        combined["marginSummary"] = data.get("marginSummary", {})
        except Exception as e:
            log.error(f"Error fetching clearinghouseState: {e}")
    
    return combined


async def position_poller(notifier: TelegramNotifier, tracker: PositionTracker):
    """Poll clearinghouseState periodically to detect position changes."""
    log.info(f"Starting position poller (every {CONFIG['POLL_INTERVAL']}s)...")
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                state = await fetch_clearinghouse_state(session)
                if state:
                    alerts = tracker.process_snapshot(state)
                    for alert in alerts:
                        await notifier.send_message(alert)
            except Exception as e:
                log.error(f"Poller error: {e}")
            
            await asyncio.sleep(CONFIG["POLL_INTERVAL"])


# ─── WebSocket Listener for Real-Time Fills ─────────────────────────────────
async def ws_fill_listener(notifier: TelegramNotifier):
    """Listen for real-time fill events via WebSocket."""
    reconnect_delay = CONFIG["RECONNECT_DELAY"]
    consecutive_failures = 0
    addr = CONFIG["TARGET_ADDRESS"]
    label = CONFIG["TARGET_LABEL"]
    link = f"https://hypurrscan.io/address/{addr}"
    
    while True:
        try:
            log.info("Connecting to Hyperliquid WebSocket...")
            async with websockets.connect(
                CONFIG["HL_WS_URL"],
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
            ) as ws:
                log.info("WebSocket connected!")
                reconnect_delay = CONFIG["RECONNECT_DELAY"]
                consecutive_failures = 0
                
                # Subscribe to user fills
                sub = {
                    "method": "subscribe",
                    "subscription": {
                        "type": "userFills",
                        "user": CONFIG["TARGET_ADDRESS"],
                    }
                }
                await ws.send(json.dumps(sub))
                log.info(f"Subscribed to userFills for {addr[:10]}...")
                
                # Subscribe to user events (liquidations, funding, etc.)
                sub_events = {
                    "method": "subscribe",
                    "subscription": {
                        "type": "userEvents",
                        "user": CONFIG["TARGET_ADDRESS"],
                    }
                }
                await ws.send(json.dumps(sub_events))
                log.info(f"Subscribed to userEvents for {addr[:10]}...")
                
                async for raw_msg in ws:
                    try:
                        msg = json.loads(raw_msg)
                        channel = msg.get("channel", "")
                        data = msg.get("data", {})
                        
                        # Skip subscription confirmations
                        if channel == "subscriptionResponse":
                            log.info(f"Subscription confirmed: {data}")
                            continue
                        
                        # Handle fills
                        if channel == "userFills":
                            fills = data if isinstance(data, list) else data.get("fills", [])
                            # Skip snapshot fills (historical)
                            if msg.get("data", {}).get("isSnapshot"):
                                log.info(f"Received fills snapshot ({len(fills)} fills)")
                                continue
                            
                            for fill in fills:
                                coin = fill.get("coin", "?")
                                side = "BUY" if fill.get("side") == "B" else "SELL"
                                px = fill.get("px", "?")
                                sz = fill.get("sz", "?")
                                direction = fill.get("dir", "?")
                                closed_pnl = fill.get("closedPnl", "0")
                                
                                pnl_str = ""
                                try:
                                    pnl_val = float(closed_pnl)
                                    if pnl_val != 0:
                                        pnl_emoji = "💚" if pnl_val > 0 else "❤️"
                                        pnl_str = f"\n{pnl_emoji} Closed PnL: ${closed_pnl}"
                                except:
                                    pass
                                
                                alert = (
                                    f"💱 <b>FILL — {label}</b>\n\n"
                                    f"📌 <b>{coin}</b> | {side}\n"
                                    f"📍 Direction: {direction}\n"
                                    f"📐 Size: {sz}\n"
                                    f"💰 Price: ${px}"
                                    f"{pnl_str}\n\n"
                                    f"🔗 <a href='{link}'>Hypurrscan</a>"
                                )
                                await notifier.send_message(alert)
                        
                        # Handle user events (liquidations etc.)
                        if channel == "userEvents":
                            events = data if isinstance(data, list) else [data]
                            for event in events:
                                if isinstance(event, dict) and "liquidation" in event:
                                    liq = event["liquidation"]
                                    alert = (
                                        f"💀 <b>LIQUIDATION — {label}</b>\n\n"
                                        f"⚠️ Account Value: ${liq.get('accountValue', '?')}\n"
                                        f"📊 Leverage: {liq.get('leverageType', '?')}\n\n"
                                        f"🔗 <a href='{link}'>Hypurrscan</a>"
                                    )
                                    await notifier.send_message(alert)
                    
                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        log.error(f"WS message error: {e}")
        
        except websockets.exceptions.ConnectionClosed as e:
            log.warning(f"WebSocket closed: {e}")
            consecutive_failures += 1
        except Exception as e:
            log.error(f"WebSocket error: {e}")
            consecutive_failures += 1
        
        if consecutive_failures >= 5:
            await notifier.send_message(
                f"⚠️ <b>HL Tracker — Connection unstable</b>\n"
                f"{consecutive_failures} reconnects. Still trying..."
            )
            consecutive_failures = 0
        
        log.info(f"Reconnecting WS in {reconnect_delay}s...")
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 1.5, CONFIG["MAX_RECONNECT_DELAY"])


# ─── Main ───────────────────────────────────────────────────────────────────
async def main():
    log.info("=" * 60)
    log.info("Hyperliquid Position Tracker Starting...")
    log.info("=" * 60)
    
    if CONFIG["TELEGRAM_BOT_TOKEN"] == "YOUR_BOT_TOKEN_HERE":
        log.error("Set TELEGRAM_BOT_TOKEN environment variable")
        sys.exit(1)
    if CONFIG["TELEGRAM_CHAT_ID"] == "YOUR_CHAT_ID_HERE":
        log.error("Set TELEGRAM_CHAT_ID environment variable")
        sys.exit(1)
    
    notifier = TelegramNotifier(CONFIG["TELEGRAM_BOT_TOKEN"], CONFIG["TELEGRAM_CHAT_ID"])
    tracker = PositionTracker()
    
    await notifier.send_startup_message()
    
    # Run both the REST poller and WebSocket listener concurrently
    await asyncio.gather(
        position_poller(notifier, tracker),
        ws_fill_listener(notifier),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")
