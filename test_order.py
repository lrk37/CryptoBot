"""
test_order.py — Binance Dry-Run Test Order Utility
===================================================
Tests Binance LOT_SIZE and MIN_NOTIONAL filters without executing real trades
using Binance's `create_test_order` endpoint.
"""

import sys
import logging
from binance.client import Client
from binance.exceptions import BinanceAPIException
import config as cfg

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("TestOrder")

def test_dry_run_order(symbol: str = "BTCUSDT", usdt_amount: float = 15.0):
    print("=" * 60)
    print(f"      BINANCE TEST ORDER (DRY-RUN): {symbol}")
    print("=" * 60)

    api_key = getattr(cfg, 'API_KEY', '')
    api_secret = getattr(cfg, 'API_SECRET', '')

    if not api_key or not api_secret:
        print("[!] ERROR: API_KEY or API_SECRET is empty.")
        sys.exit(1)

    client = Client(api_key, api_secret, testnet=getattr(cfg, 'USE_TESTNET', False))

    try:
        # Get symbol ticker price
        ticker = client.get_symbol_ticker(symbol=symbol)
        curr_price = float(ticker['price'])
        print(f"[*] Current Price for {symbol}: ${curr_price:,.2f}")
        print(f"[*] Testing Quote Order Qty: ${usdt_amount:.2f} USDT")

        # Send test order (validates parameters and filters without placing order)
        client.create_test_order(
            symbol=symbol,
            side='BUY',
            type='MARKET',
            quoteOrderQty=usdt_amount
        )
        print("\n[SUCCESS] create_test_order passed Binance filter validation!")
        print("          Order size, price filters, and notional limits are all valid.")
        print("=" * 60)

    except BinanceAPIException as e:
        print(f"\n[!] Test Order Failed ({e.code}): {e.message}")
        print("=" * 60)
    except Exception as e:
        print(f"\n[!] Error: {e}")
        print("=" * 60)

if __name__ == "__main__":
    test_dry_run_order()
