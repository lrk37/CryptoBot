"""notifier.py — Telegram Alerts (MarkdownV2)"""
import logging
import re
import requests

log = logging.getLogger(__name__)

def _escape_md2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2.
    F-25: Fixed regex (removed corrupted 'vast', correct single backslash).
    F-33: Changed default from None to required str parameter."""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', str(text))

class TelegramNotifier:
    def __init__(self, cfg):
        self.enabled = cfg.ENABLE_TELEGRAM
        self.token = cfg.TELEGRAM_TOKEN
        self.chat_id = cfg.TELEGRAM_CHAT_ID
        self._url = f"https://api.telegram.org/bot{self.token}/sendMessage"

    def send(self, msg, parse_mode=None):
        if not self.enabled:
            return None
        try:
            payload = {'chat_id': self.chat_id, 'text': msg}
            if parse_mode:
                payload['parse_mode'] = parse_mode
            requests.post(self._url, json=payload, timeout=10)
        except Exception as e:
            log.warning(f"Telegram error: {e}")

    def trade_opened(self, symbol, side, price, qty, sl, tp):
        # F-26: Use code spans for values (no escaping needed inside backticks).
        # Only escape text outside code spans.
        side_esc = _escape_md2(side)
        sym_esc = _escape_md2(symbol)
        msg = (f"\U0001f7e2 *{side_esc}* `{symbol}`\n"
               f"Price: `{price:.6f}` \\| Qty: `{qty}`\n"
               f"SL: `{sl:.6f}` \\| TP: `{tp:.6f}`")
        self.send(msg, parse_mode='MarkdownV2')

    def trade_closed(self, symbol, reason, entry, close, pnl):
        e = '\u2705' if pnl >= 0 else '\U0001f534'
        rsn = _escape_md2(reason)
        msg = (f"{e} *CLOSED* `{symbol}` \\- {rsn}\n"
               f"Entry: `{entry:.6f}` \\-> Exit: `{close:.6f}`\n"
               f"PnL: `{pnl:+.2f}%`")
        self.send(msg, parse_mode='MarkdownV2')

    def alert(self, msg):
        self.send(f"\u26a0\ufe0f {msg}")
