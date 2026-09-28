#!/bin/bash
# Cài systemd cho Smart Key (A + B). Chạy bằng: sudo ./install_services.sh
set -euo pipefail

# ========== CHỈNH 2 DÒNG NÀY CHO ĐÚNG MÁY BẠN ==========
KIOSK_DIR="${KIOSK_DIR:-/home/pi/kiosk}"
REMOTE_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-pi}"
# ========================================================

if [[ $EUID -ne 0 ]]; then
  echo "Chạy với sudo: sudo ./install_services.sh"
  exit 1
fi

echo "==> Kiosk dir : $KIOSK_DIR"
echo "==> Remote dir: $REMOTE_DIR"
echo "==> User      : $RUN_USER"

if [[ ! -f "$KIOSK_DIR/kiosk_ui_v8.py" ]]; then
  echo "CẢNH BÁO: Không thấy $KIOSK_DIR/kiosk_ui_v8.py"
  echo "  export KIOSK_DIR=/đường/dẫn/thật rồi chạy lại."
fi

if [[ ! -f "$REMOTE_DIR/telegram_remote.py" ]]; then
  echo "Thiếu telegram_remote.py trong $REMOTE_DIR"
  exit 1
fi

chmod +x "$REMOTE_DIR/telegram_remote.py" 2>/dev/null || true

# Sinh unit từ template (thay path)
gen_unit() {
  local src="$1"
  local dst="$2"
  sed -e "s|/home/pi/kiosk|$KIOSK_DIR|g" \
      -e "s|/home/pi/smartkey_remote|$REMOTE_DIR|g" \
      -e "s|User=pi|User=$RUN_USER|g" \
      -e "s|Group=pi|Group=$RUN_USER|g" \
      -e "s|/home/pi/|$HOME_DIR_PLACEHOLDER/|g" \
      "$src" > "$dst"
}

# Fix home for Xauthority path
HOME_OF_USER="$(getent passwd "$RUN_USER" | cut -d: -f6)"
HOME_OF_USER="${HOME_OF_USER:-/home/$RUN_USER}"

tmpdir=$(mktemp -d)
sed -e "s|/home/pi/kiosk|$KIOSK_DIR|g" \
    -e "s|/home/pi/smartkey_remote|$REMOTE_DIR|g" \
    -e "s|User=pi|User=$RUN_USER|g" \
    -e "s|Group=pi|Group=$RUN_USER|g" \
    -e "s|/home/pi/.Xauthority|$HOME_OF_USER/.Xauthority|g" \
    "$REMOTE_DIR/systemd/smartkey-kiosk.service" > "$tmpdir/smartkey-kiosk.service"

sed -e "s|/home/pi/kiosk|$KIOSK_DIR|g" \
    -e "s|/home/pi/smartkey_remote|$REMOTE_DIR|g" \
    -e "s|User=pi|User=$RUN_USER|g" \
    -e "s|Group=pi|Group=$RUN_USER|g" \
    "$REMOTE_DIR/systemd/smartkey-telegram-remote.service" > "$tmpdir/smartkey-telegram-remote.service"

cp "$tmpdir/smartkey-kiosk.service" /etc/systemd/system/
cp "$tmpdir/smartkey-telegram-remote.service" /etc/systemd/system/
cp "$REMOTE_DIR/systemd/wifi-connect.service" /etc/systemd/system/wifi-connect.service

systemctl daemon-reload

echo ""
echo "==> Đã cài unit. Bật service:"
echo "    sudo systemctl enable --now smartkey-kiosk"
echo "    sudo systemctl enable --now smartkey-telegram-remote"
echo ""
echo "==> WiFi-connect (tuỳ chọn):"
echo "    1. Tải binary wifi-connect (arm/aarch64) vào /usr/local/bin/wifi-connect"
echo "    2. sudo chmod +x /usr/local/bin/wifi-connect"
echo "    3. sudo systemctl enable wifi-connect"
echo ""
echo "==> Kiểm tra:"
echo "    systemctl status smartkey-kiosk"
echo "    systemctl status smartkey-telegram-remote"
echo "    journalctl -u smartkey-telegram-remote -f"

rm -rf "$tmpdir"
