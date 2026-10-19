"""
check_perms.py — Binance API Key & Permissions Diagnostic Tool
==============================================================
Validates:
  1. API Key & Secret configuration
  2. Server connectivity & time synchronization
  3. Spot trading permissions
  4. Current USDT & Crypto balances
"""

import sys
import logging
from binance.client import Client
from binance.exceptions import BinanceAPIException
import config as cfg

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("PermissionsCheck")

def main():
    print("=" * 60)
    print("      BINANCE API KEY & PERMISSIONS DIAGNOSTIC")
    print("=" * 60)

    mode = "TESTNET" if getattr(cfg, 'USE_TESTNET', False) else "LIVE SPOT"
    print(f"[*] Target Environment : {mode}")

    api_key = getattr(cfg, 'API_KEY', '')
    api_secret = getattr(cfg, 'API_SECRET', '')

    if not api_key or not api_secret:
        print("[!] ERROR: API_KEY or API_SECRET is empty.")
        print("    Please set BINANCE_API_KEY and BINANCE_API_SECRET in your .env file.")
        sys.exit(1)

    print(f"[*] API Key (masked)   : {api_key[:6]}...{api_key[-4:]}")

    try:
        client = Client(api_key, api_secret, testnet=getattr(cfg, 'USE_TESTNET', False))

        # Check server time
        server_time = client.get_server_time()
        print(f"[+] Server Time Check  : OK ({server_time['serverTime']})")

        # Check account status & permissions
        acct = client.get_account()
        can_trade = acct.get('canTrade', False)
        can_withdraw = acct.get('canWithdraw', False)
        can_deposit = acct.get('canDeposit', False)

        print("\n--- Account Permissions ---")
        print(f"  Spot Trading Enabled : {'YES' if can_trade else 'NO [!] (Enable Spot Trading in API Management)'}")
        print(f"  Withdrawals Allowed  : {'YES' if can_withdraw else 'NO (Recommended: keep disabled for safety)'}")
        print(f"  Deposits Allowed     : {'YES' if can_deposit else 'NO'}")

        # List non-zero balances
        print("\n--- Current Account Balances ---")
        balances = [b for b in acct.get('balances', []) if float(b['free']) > 0 or float(b['locked']) > 0]
        if not balances:
            print("  [i] All balances are 0.00")
        else:
            for b in balances:
                free = float(b['free'])
                locked = float(b['locked'])
                print(f"  {b['asset']:<8}: Free = {free:<12.6f} | Locked = {locked:<12.6f}")

        print("\n" + "=" * 60)
        if can_trade:
            print("[SUCCESS] API Key is valid and authorized for automated Spot trading!")
        else:
            print("[WARNING] API Key connected, but SPOT TRADING is disabled on Binance.")
        print("=" * 60)

    except BinanceAPIException as e:
        print(f"\n[!] Binance API Error ({e.code}): {e.message}")
        if e.code == -2014 or e.code == -2015:
            print("    -> Check your API key/secret, or verify your IP whitelist settings on Binance.")
    except Exception as e:
        print(f"\n[!] Connection Failed: {e}")

if __name__ == "__main__":
    main()
