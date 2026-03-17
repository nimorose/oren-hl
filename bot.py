#!/usr/bin/env python3
"""
Nado.xyz Position Tracker - Telegram Bot
==========================================
Monitors a specific subaccount on Nado.xyz and sends Telegram alerts
when perp positions are opened, modified, or closed.
Uses REST polling of the gateway API.
"""

import asyncio
import json
import logging
import os
import sys
from copy import deepcopy
from datetime import datetime, timezone

import aiohttp

# ─── Configuration ──────────────────────────────────────────────────────────
CONFIG = {
    # Nado API
    "NADO_GATEWAY_URL": "https://gateway.prod.nado.xyz/v1",
    
    # Subaccount to track (wallet + "default" suffix)
    "SUBACCOUNT": "0xb322c1b811eaf8b3840133a2fc936f4c7185d51f64656661756c740000000000",
    "WALLET_ADDRESS": "0xb322c1b811eaf8b3840133a2fc936f4c7185d51f",
    "LABEL": "Nado Whale",
    
    # Telegram
    "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", "8578516714:AAFajR3rEJwLFqWct8OEWJG4A_e_hxkRheo"),
    "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID", "1454298447"),
    
    # Polling interval (seconds)
    "POLL_INTERVAL": 5,
}

# Product ID → symbol mapping (will be fetched dynamically)
PRODUCT_SYMBOLS = {}

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nado-tracker")


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
                        log.error(f"Telegram error {resp.status}: {body}")
                    else:
                        log.info("Telegram notification sent")
        except Exception as e:
            log.error(f"Telegram send failed: {e}")
    
    async def send_startup_message(self):
        addr = CONFIG["WALLET_ADDRESS"]
        msg = (
            f"🟢 <b>Nado Tracker Started</b>\n\n"
            f"👤 Tracking: <b>{CONFIG['LABEL']}</b>\n"
            f"🔗 Wallet: <code>{addr[:10]}...{addr[-8:]}</code>\n"
            f"🌐 <a href='https://app.nado.xyz/'>Nado Exchange</a>\n\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        await self.send_message(msg)


# ─── Fetch Product Symbols ──────────────────────────────────────────────────
async def fetch_product_symbols(session: aiohttp.ClientSession):
    """Fetch product ID → symbol mapping from Nado."""
    global PRODUCT_SYMBOLS
    try:
        url = f"{CONFIG['NADO_GATEWAY_URL']}/symbols"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                symbols = await resp.json()
                for s in symbols:
                    pid = s.get("product_id")
                    symbol = s.get("symbol", s.get("name", f"PRODUCT_{pid}"))
                    if pid is not None:
                        PRODUCT_SYMBOLS[pid] = symbol
                log.info(f"Loaded {len(PRODUCT_SYMBOLS)} product symbols")
    except Exception as e:
        log.error(f"Failed to fetch symbols: {e}")
        # Fallback common products
        PRODUCT_SYMBOLS = {
            0: "USDT0",
            1: "BTC-PERP",
            2: "ETH-PERP",
            3: "SOL-PERP",
            4: "wBTC",
            5: "ETH",
        }


def get_symbol(product_id: int) -> str:
    return PRODUCT_SYMBOLS.get(product_id, f"PRODUCT_{product_id}")


# ─── Position Tracker ───────────────────────────────────────────────────────
class PositionTracker:
    """Tracks perp position changes by comparing snapshots."""
    
    def __init__(self):
        self.perp_positions: dict = {}  # product_id -> position data
        self.initialized = False
    
    def _parse_perp_positions(self, subaccount_info: dict) -> dict:
        """Parse perp positions from subaccount_info response."""
        positions = {}
        perp_balances = subaccount_info.get("perp_balances", [])
        
        for bal in perp_balances:
            product_id = bal.get("product_id")
            if product_id is None:
                continue
            
            # amount is in x18 format (divide by 1e18)
            amount_raw = bal.get("balance", {}).get("amount", "0")
            try:
                amount = int(amount_raw) / 1e18
            except (ValueError, TypeError):
                amount = 0
            
            if abs(amount) < 0.000001:
                continue
            
            # Get entry/vAMM price info if available
            v_quote = bal.get("balance", {}).get("v_quote_balance", "0")
            try:
                v_quote_val = int(v_quote) / 1e18
            except (ValueError, TypeError):
                v_quote_val = 0
            
            entry_px = abs(v_quote_val / amount) if amount != 0 else 0
            
            positions[product_id] = {
                "product_id": product_id,
                "symbol": get_symbol(product_id),
                "amount": amount,
                "size": abs(amount),
                "side": "LONG" if amount > 0 else "SHORT",
                "entry_px": round(entry_px, 2),
                "v_quote": v_quote_val,
            }
        
        return positions
    
    def _side_emoji(self, side: str) -> str:
        return "🟢" if side == "LONG" else "🔴"
    
    def process_snapshot(self, subaccount_info: dict) -> list:
        """Compare new snapshot with previous and return alert messages."""
        new_positions = self._parse_perp_positions(subaccount_info)
        alerts = []
        label = CONFIG["LABEL"]
        
        if not self.initialized:
            self.initialized = True
            self.perp_positions = deepcopy(new_positions)
            
            if new_positions:
                lines = [f"📋 <b>{label} — Current Perp Positions:</b>\n"]
                for pid, pos in new_positions.items():
                    emoji = self._side_emoji(pos["side"])
                    lines.append(
                        f"  {emoji} <b>{pos['symbol']}</b> {pos['side']}\n"
                        f"    Size: {pos['size']:.6f} | ~Entry: ${pos['entry_px']}"
                    )
                alerts.append("\n".join(lines))
            else:
                alerts.append(f"📋 <b>{label}</b> — No open perp positions.")
            
            return alerts
        
        # Compare
        all_products = set(list(self.perp_positions.keys()) + list(new_positions.keys()))
        
        for pid in all_products:
            old = self.perp_positions.get(pid)
            new = new_positions.get(pid)
            symbol = get_symbol(pid)
            
            old_size = old["size"] if old else 0
            old_side = old["side"] if old else None
            new_size = new["size"] if new else 0
            new_side = new["side"] if new else None
            
            # Skip if no meaningful change
            if old and new and abs(old_size - new_size) < 0.000001 and old_side == new_side:
                continue
            
            # New position
            if old is None and new is not None:
                emoji = self._side_emoji(new["side"])
                alert = (
                    f"🚨 <b>NEW POSITION — {label}</b>\n\n"
                    f"{emoji} Market: <b>{symbol}</b>\n"
                    f"📍 Side: {new['side']}\n"
                    f"📐 Size: {new['size']:.6f}\n"
                    f"💰 ~Entry: ${new['entry_px']}\n\n"
                    f"🌐 <a href='https://app.nado.xyz/'>Nado</a>"
                )
                alerts.append(alert)
            
            # Position closed
            elif old is not None and new is None:
                emoji = self._side_emoji(old["side"])
                alert = (
                    f"✅ <b>POSITION CLOSED — {label}</b>\n\n"
                    f"{emoji} Market: <b>{symbol}</b>\n"
                    f"📍 Was: {old['side']}\n"
                    f"📐 Size was: {old['size']:.6f}\n"
                    f"💰 ~Entry was: ${old['entry_px']}\n\n"
                    f"🌐 <a href='https://app.nado.xyz/'>Nado</a>"
                )
                alerts.append(alert)
            
            # Flipped direction
            elif old_side != new_side:
                alert = (
                    f"🔄 <b>POSITION FLIPPED — {label}</b>\n\n"
                    f"📌 Market: <b>{symbol}</b>\n"
                    f"📍 {self._side_emoji(old_side)} {old_side} → {self._side_emoji(new_side)} {new_side}\n"
                    f"📐 Size: {old['size']:.6f} → {new['size']:.6f}\n"
                    f"💰 ~Entry: ${new['entry_px']}\n\n"
                    f"🌐 <a href='https://app.nado.xyz/'>Nado</a>"
                )
                alerts.append(alert)
            
            # Size increased
            elif new_size > old_size * 1.001:
                diff = new_size - old_size
                emoji = self._side_emoji(new["side"])
                alert = (
                    f"⬆️ <b>POSITION INCREASED — {label}</b>\n\n"
                    f"{emoji} Market: <b>{symbol}</b> {new['side']}\n"
                    f"📐 Size: {old['size']:.6f} → {new['size']:.6f} (+{diff:.6f})\n"
                    f"💰 ~Entry: ${new['entry_px']}\n\n"
                    f"🌐 <a href='https://app.nado.xyz/'>Nado</a>"
                )
                alerts.append(alert)
            
            # Size decreased
            elif new_size < old_size * 0.999:
                diff = old_size - new_size
                emoji = self._side_emoji(new["side"])
                alert = (
                    f"⬇️ <b>PARTIAL CLOSE — {label}</b>\n\n"
                    f"{emoji} Market: <b>{symbol}</b> {new['side']}\n"
                    f"📐 Size: {old['size']:.6f} → {new['size']:.6f} (-{diff:.6f})\n"
                    f"💰 ~Entry: ${new['entry_px']}\n\n"
                    f"🌐 <a href='https://app.nado.xyz/'>Nado</a>"
                )
                alerts.append(alert)
        
        self.perp_positions = deepcopy(new_positions)
        return alerts


# ─── Poller ─────────────────────────────────────────────────────────────────
async def fetch_subaccount_info(session: aiohttp.ClientSession) -> dict:
    """Fetch subaccount info from Nado gateway."""
    url = f"{CONFIG['NADO_GATEWAY_URL']}/query?type=subaccount_info&subaccount={CONFIG['SUBACCOUNT']}"
    headers = {"Accept-Encoding": "gzip, deflate, br"}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status == 200:
            return await resp.json()
        else:
            body = await resp.text()
            log.error(f"Nado API error {resp.status}: {body}")
            return {}


async def position_poller(notifier: TelegramNotifier, tracker: PositionTracker):
    """Poll subaccount info periodically."""
    log.info(f"Starting poller (every {CONFIG['POLL_INTERVAL']}s)...")
    
    async with aiohttp.ClientSession() as session:
        # Fetch product symbols first
        await fetch_product_symbols(session)
        
        while True:
            try:
                data = await fetch_subaccount_info(session)
                if data and data.get("status") == "success":
                    alerts = tracker.process_snapshot(data)
                    for alert in alerts:
                        await notifier.send_message(alert)
            except Exception as e:
                log.error(f"Poller error: {e}")
            
            await asyncio.sleep(CONFIG["POLL_INTERVAL"])


# ─── Main ───────────────────────────────────────────────────────────────────
async def main():
    log.info("=" * 60)
    log.info("Nado Position Tracker Starting...")
    log.info("=" * 60)
    
    if CONFIG["TELEGRAM_BOT_TOKEN"] == "YOUR_BOT_TOKEN_HERE":
        log.error("Set TELEGRAM_BOT_TOKEN")
        sys.exit(1)
    if CONFIG["TELEGRAM_CHAT_ID"] == "YOUR_CHAT_ID_HERE":
        log.error("Set TELEGRAM_CHAT_ID")
        sys.exit(1)
    
    notifier = TelegramNotifier(CONFIG["TELEGRAM_BOT_TOKEN"], CONFIG["TELEGRAM_CHAT_ID"])
    tracker = PositionTracker()
    
    await notifier.send_startup_message()
    await position_poller(notifier, tracker)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")
