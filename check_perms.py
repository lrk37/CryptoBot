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
        all_balances = acct.get('balances', [])
        balances = [b for b in all_balances if float(b['free']) > 0 or float(b['locked']) > 0]
        if not balances:
            print("  [i] All balances are 0.00")
        else:
            for b in balances:
                free = float(b['free'])
                locked = float(b['locked'])
                print(f"  {b['asset']:<8}: Free = {free:<12.6f} | Locked = {locked:<12.6f}")

        # Check BNB fee reserve on Live Spot
        bnb_free = 0.0
        for b in all_balances:
            if b.get('asset') == 'BNB':
                bnb_free = float(b.get('free', 0) or 0)
                break
        bnb_usdt = 0.0
        if bnb_free > 0:
            try:
                bnb_px = float(client.get_symbol_ticker(symbol='BNBUSDT')['price'])
                bnb_usdt = bnb_free * bnb_px
            except Exception:
                bnb_usdt = 0.0
        min_bnb_usdt = float(getattr(cfg, 'MIN_BNB_FEE_RESERVE_USDT', 1.0) or 1.0)
        print("\n--- BNB Fee Reserve Check ---")
        if bnb_free <= 0:
            print("  [WARNING] ZERO BNB BALANCE (0.00 BNB)!")
            print("            Without BNB, Binance deducts 0.10% fees from bought altcoins, creating")
            print("            unsellable LOT_SIZE dust and forfeiting the 25% BNB fee discount.")
            print("            Recommended: Keep ~$2-$5 of BNB in Spot and enable 'Use BNB to pay fees'.")
        elif 0 < bnb_usdt < min_bnb_usdt:
            print(f"  [WARNING] LOW BNB FEE RESERVE: {bnb_free:.6f} BNB (~${bnb_usdt:.2f} USDT < ${min_bnb_usdt:.2f})")
            print("            Top up BNB soon so Spot fees do not fall back to base-asset deduction.")
        else:
            val_str = f" (~${bnb_usdt:.2f} USDT)" if bnb_usdt > 0 else ""
            print(f"  [OK] BNB Fee Reserve: {bnb_free:.6f} BNB{val_str}")

        print("\n" + "=" * 60)
        if can_trade:
            print("[SUCCESS] API Key is valid and authorized for automated Spot trading!")
        else:
            print("[WARNING] API Key connected, but SPOT TRADING is disabled on Binance.")
        print("=" * 60)

    except BinanceAPIException as e:
        print(f"\n[!] Binance API Error ({e.code}): {e.message}")
        if e.code == -2014 or e.code == -2015:
            if getattr(cfg, 'USE_TESTNET', False):
                print("    -> TESTNET MODE IS ON: Normal binance.com keys do NOT work on Testnet!")
                print("    -> Go to https://testnet.binance.vision/ -> Log in with GitHub -> Generate HMAC_SHA256 Key")
                print("    -> Put those keys in BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET in .env")
            else:
                print("    -> Check your API key/secret, or verify your IP whitelist settings on Binance.")
    except Exception as e:
        print(f"\n[!] Connection Failed: {e}")

if __name__ == "__main__":
    main()
