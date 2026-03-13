#!/bin/bash
# RPi Audio Console — Install script
# Run as: bash install_rpi.sh
set -e

INSTALL_DIR="$HOME/mixer-console"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_NAME="mixer-console"

echo "RPi Audio Console — Installing..."
echo ""

# 1. System packages
echo "[1/6] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3-pip python3-venv python3-full chromium xdotool x11-utils

# 2. Enable SPI
if ! grep -q "^dtparam=spi=on" /boot/config.txt 2>/dev/null && \
   ! grep -q "^dtparam=spi=on" /boot/firmware/config.txt 2>/dev/null; then
    echo "[2/6] Enabling SPI..."
    CFG=/boot/firmware/config.txt
    [ -f /boot/config.txt ] && CFG=/boot/config.txt
    echo "dtparam=spi=on" | sudo tee -a "$CFG"
    echo "  SPI enabled — reboot required before pots will work"
else
    echo "[2/6] SPI already enabled"
fi

# 3. Create venv
echo "[3/6] Creating virtual environment at $VENV_DIR..."
mkdir -p "$INSTALL_DIR"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

# 4. Python packages inside venv
echo "[4/6] Installing Python packages into venv..."
pip install --upgrade pip -q
pip install fastapi "uvicorn[standard]" websockets requests -q
# spidev has no wheel — install from system or build
pip install spidev -q 2>/dev/null || echo "  spidev install failed — will use system package fallback"

# 5. Copy files
echo "[5/6] Copying files..."
cp rpi_backend.py  "$INSTALL_DIR/"
cp console_ui.html "$INSTALL_DIR/"

# 6. Install systemd service (updated to use venv python)
echo "[6/6] Installing systemd service..."

# Write the service file with correct venv python path and user
ACTUAL_USER="${SUDO_USER:-$USER}"
ACTUAL_HOME=$(eval echo "~$ACTUAL_USER")
VENV_PYTHON="$ACTUAL_HOME/mixer-console/venv/bin/python"
BACKEND="$ACTUAL_HOME/mixer-console/rpi_backend.py"
WORKDIR="$ACTUAL_HOME/mixer-console"

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
[Unit]
Description=RPi Audio Console Backend
After=network.target

[Service]
Type=simple
User=$ACTUAL_USER
WorkingDirectory=$WORKDIR
ExecStart=$VENV_PYTHON $BACKEND
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
SupplementaryGroups=spi gpio

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

RPI_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║        INSTALL COMPLETE ✓                    ║"
echo "║                                              ║"
echo "║  Venv:    $VENV_DIR"
echo "║  Service: sudo systemctl status $SERVICE_NAME"
echo "║  Logs:    journalctl -u $SERVICE_NAME -f     ║"
echo "║  UI:      http://$RPI_IP:5000                ║"
echo "║                                              ║"
echo "║  IMPORTANT: Tap ⚙ in the UI to set PC IP    ║"
echo "╚══════════════════════════════════════════════╝"
