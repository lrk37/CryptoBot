"""notifier.py — Telegram Alerts (MarkdownV2)"""
import logging
import re
import time
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None
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
        self.tz_name = getattr(cfg, 'DISPLAY_TIMEZONE', 'Europe/Belgrade')
        self.tz_offset_hours = int(getattr(cfg, 'DISPLAY_TZ_OFFSET_HOURS', 1))
        self._url = f"https://api.telegram.org/bot{self.token}/sendMessage"

    def send(self, msg, parse_mode=None) -> bool:
        if not self.enabled or not self.token or not self.chat_id:
            return False
        try:
            payload = {'chat_id': self.chat_id, 'text': msg}
            if parse_mode:
                payload['parse_mode'] = parse_mode
            resp = requests.post(self._url, json=payload, timeout=10)
            if resp.status_code != 200:
                log.warning(f"Telegram API HTTP {resp.status_code}: {resp.text}")
                # Fallback: if MarkdownV2 formatting caused a 400 error, retry as plain text
                if parse_mode:
                    fb_resp = requests.post(self._url, json={'chat_id': self.chat_id, 'text': msg}, timeout=10)
                    if fb_resp.status_code == 200:
                        return True
                return False
            return True
        except Exception as e:
            log.warning(f"Telegram connection error: {e}")
            return False

    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m {seconds % 60}s"
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        return f"{hours}h {mins}m"

    def _format_ts(self, epoch_sec: float = None) -> str:
        ts = epoch_sec if epoch_sec is not None else time.time()
        if ZoneInfo is not None and self.tz_name:
            try:
                tz = ZoneInfo(self.tz_name)
                dt = datetime.fromtimestamp(ts, tz=tz)
                off_sec = int(dt.utcoffset().total_seconds()) if dt.utcoffset() else self.tz_offset_hours * 3600
                off_h = off_sec // 3600
                sign = f"+{off_h}" if off_h >= 0 else str(off_h)
                return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} (UTC{sign})"
            except Exception:
                pass
        tz = timezone(timedelta(hours=self.tz_offset_hours))
        dt = datetime.fromtimestamp(ts, tz=tz)
        sign = f"+{self.tz_offset_hours}" if self.tz_offset_hours >= 0 else str(self.tz_offset_hours)
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} (UTC{sign})"

    def trade_opened(self, symbol, side, price, qty, sl, tp, timeframe: str = None, max_hold_hours: int = 8, open_time: float = None):
        # F-26: Use code spans for values (no escaping needed inside backticks).
        # Only escape text outside code spans.
        side_esc = _escape_md2(side)
        tf_tag = f" \\[`{timeframe}`\\]" if timeframe else ""
        open_str = self._format_ts(open_time)
        msg = (f"\U0001f7e2 *{side_esc}* `{symbol}`{tf_tag}\n"
               f"Price: `{price:.6f}` \\| Qty: `{qty}`\n"
               f"SL: `{sl:.6f}` \\| TP: `{tp:.6f}`\n"
               f"\U0001f552 Opened: `{open_str}`\n"
               f"\u23f1 Est\\. Hold: `30m - 3h` \\(Max `{max_hold_hours}h`\\)")
        self.send(msg, parse_mode='MarkdownV2')

    def trade_closed(self, symbol, reason, entry, close, pnl, duration_sec: float = None, pnl_usdt: float = None,
                     open_time: float = None, close_time: float = None):
        e = '\u2705' if pnl >= 0 else '\U0001f534'
        rsn = _escape_md2(reason)
        pnl_str = f"`{pnl:+.2f}%` \\(`{pnl_usdt:+.2f} USDT`\\)" if pnl_usdt is not None else f"`{pnl:+.2f}%`"
        now_ts = close_time if close_time is not None else time.time()
        if open_time is None and duration_sec is not None:
            open_time = now_ts - duration_sec
        open_line = f"\n\U0001f552 Opened: `{self._format_ts(open_time)}`" if open_time is not None else ""
        close_line = f"\n\U0001f3c1 Closed: `{self._format_ts(now_ts)}`"
        dur_line = f"\n\u23f1 Duration: `{self._format_duration(duration_sec)}`" if duration_sec is not None else ""
        msg = (f"{e} *CLOSED* `{symbol}` \\- {rsn}\n"
               f"Entry: `{entry:.6f}` \\-> Exit: `{close:.6f}`\n"
               f"PnL: {pnl_str}{open_line}{close_line}{dur_line}")
        self.send(msg, parse_mode='MarkdownV2')

    def alert(self, msg):
        return self.send(f"⚠️ {msg}")

    def error(self, msg):
        return self.send(f"🚨 *CRITICAL ERROR*:\n{msg}")

