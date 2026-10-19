# VPS Deployment Guide for CryptoBot

This guide provides step-by-step instructions for deploying and running **CryptoBot** 24/7 on a secure Linux Cloud VPS (Virtual Private Server) such as **Hetzner, DigitalOcean, Linode, Vultr, or AWS**.

---

## 🖥️ 1. Recommended VPS Specs

| Component | Minimum Specification | Recommended Provider |
| :--- | :--- | :--- |
| **OS** | Ubuntu 22.04 LTS or 24.04 LTS | Hetzner Cloud (CX22 / CPX11) (~€4/mo) |
| **CPU** | 1–2 vCPUs | DigitalOcean Basic Droplet ($4–$6/mo) |
| **RAM** | 1 GB – 2 GB RAM | Vultr Cloud Compute ($5/mo) |
| **Disk** | 20 GB SSD | Any standard cloud NVMe/SSD |
| **Location** | Close to Binance servers (Frankfurt/EU or Tokyo/AP) | Lowest latency for API calls |

---

## 🚀 2. Method A: Automated 1-Click Setup (Recommended)

Once you log into your newly created VPS via SSH:

```bash
# 1. Download and run the automated installer script
curl -sSL https://raw.githubusercontent.com/lrk37/CryptoBot/master/deploy/vps_setup.sh | bash
```

The script will automatically:
1. Update system packages and install Python 3, Git, and build tools.
2. Clone your repository into `~/CryptoBot`.
3. Create a Python virtual environment and install all dependencies.
4. Generate the `.env` configuration file from `.env.example`.
5. Install and configure the background `systemd` service (`cryptobot.service`).

---

## ⚙️ 3. Configure Your API Keys

Navigate to the project folder and edit your `.env` file:

```bash
cd ~/CryptoBot
nano .env
```

Enter your Binance credentials:
```ini
BINANCE_API_KEY=your_actual_binance_api_key_here
BINANCE_API_SECRET=your_actual_binance_api_secret_here

# Set to True if using Binance Testnet, False for Live
USE_TESTNET=False

# Optional: Telegram Alerts
ENABLE_TELEGRAM=False
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
```
*Press `Ctrl + O` and `Enter` to save, then `Ctrl + X` to exit.*

---

## 🔍 4. Verify API Connection & Permissions

Before running the bot in the background, run the built-in diagnostic tool to verify that your API key is active and has Spot Trading permissions:

```bash
source venv/bin/activate
python check_perms.py
```

Expected output:
```text
============================================================
      BINANCE API KEY & PERMISSIONS DIAGNOSTIC
============================================================
[*] Target Environment : LIVE SPOT
[+] Server Time Check  : OK
--- Account Permissions ---
  Spot Trading Enabled : YES
  Withdrawals Allowed  : NO (Recommended: keep disabled)
--- Current Account Balances ---
  USDT    : Free = 50.000000 | Locked = 0.000000
============================================================
[SUCCESS] API Key is valid and authorized for automated Spot trading!
============================================================
```

---

## 🏃 5. Start and Manage the Background Service

CryptoBot runs as a background `systemd` service that **automatically starts on server boot and restarts if a crash or network hiccup occurs**.

```bash
# Start the bot
sudo systemctl start cryptobot

# Check status (active/running)
sudo systemctl status cryptobot

# View live log output
tail -f bot.log

# Stop or restart the bot
sudo systemctl restart cryptobot
sudo systemctl stop cryptobot
```

---

## 🐳 6. Method B: Running with Docker (Alternative)

If you prefer using Docker and Docker Compose:

```bash
# 1. Install Docker & Compose on Ubuntu
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
newgrp docker

# 2. Clone repo and enter directory
git clone https://github.com/lrk37/CryptoBot.git ~/CryptoBot
cd ~/CryptoBot

# 3. Create .env with your keys
cp .env.example .env
nano .env

# 4. Start the container in background
docker compose up -d --build

# 5. View live logs
docker compose logs -f

# 6. Stop container
docker compose down
```

---

## 🛡️ 7. Security Best Practices for VPS

1. **Keep Withdrawals Disabled**: On your Binance API Key management page, **NEVER** enable "Enable Withdrawals". Only enable "Enable Reading" and "Enable Spot & Margin Trading".
2. **Restrict API Key to VPS IP**: On Binance, select **"Restrict access to trusted IPs only"** and enter your VPS's static public IP address. Even if someone steals your keys, they cannot place trades from another IP.
3. **Set up UFW Firewall on VPS**:
   ```bash
   sudo ufw default deny incoming
   sudo ufw default allow outgoing
   sudo ufw allow ssh
   sudo ufw enable
   ```
4. **Regular Monitoring**: Check your `bot.log` and `trade_log.json` periodically, or enable Telegram alerts in `.env` to receive instant notifications when trades open or close.
