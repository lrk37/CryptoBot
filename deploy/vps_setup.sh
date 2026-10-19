#!/usr/bin/env bash
# ==============================================================================
# vps_setup.sh — Automated Setup Script for Ubuntu 22.04 / 24.04 VPS
# ==============================================================================
set -e

echo "=== [1/5] Updating System Packages ==="
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y git curl wget build-essential python3 python3-pip python3-venv htop

echo "=== [2/5] Setting Up Project Directory ==="
APP_DIR="$HOME/CryptoBot"
if [ ! -d "$APP_DIR" ]; then
    echo "Cloning repository from GitHub..."
    git clone https://github.com/lrk37/CryptoBot.git "$APP_DIR"
fi
cd "$APP_DIR"

echo "=== [3/5] Setting Up Python Virtual Environment ==="
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "=== [4/5] Environment File Check ==="
if [ ! -f "$APP_DIR/.env" ]; then
    echo "[!] Creating .env from template..."
    cp .env.example .env
    echo ">>> IMPORTANT: Please edit .env now with your Binance API keys:"
    echo "    nano $APP_DIR/.env"
fi

echo "=== [5/5] Installing systemd Background Service ==="
sudo cp deploy/cryptobot.service /etc/systemd/system/cryptobot.service
# Replace user/path dynamically if not 'ubuntu'
CURRENT_USER=$(whoami)
sudo sed -i "s|User=ubuntu|User=$CURRENT_USER|g" /etc/systemd/system/cryptobot.service
sudo sed -i "s|/home/ubuntu|$HOME|g" /etc/systemd/system/cryptobot.service

sudo systemctl daemon-reload
sudo systemctl enable cryptobot.service

echo ""
echo "=========================================================================="
echo " [SUCCESS] CryptoBot installation complete!"
echo "=========================================================================="
echo " Next Steps:"
echo " 1. Add your Binance API keys:     nano ~/.env"
echo " 2. Test your API permissions:     python check_perms.py"
echo " 3. Start the bot service:         sudo systemctl start cryptobot"
echo " 4. Check service status:          sudo systemctl status cryptobot"
echo " 5. View live logs:                tail -f bot.log"
echo "=========================================================================="
