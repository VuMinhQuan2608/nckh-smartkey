#!/bin/bash
# Cài toàn bộ unit Smart Key trên Raspberry Pi
# Usage:
#   sudo KIOSK_DIR=/home/pi/kiosk REMOTE_DIR=/home/pi/smartkey_remote \
#        OPS_DIR=/home/pi/smartkey_ops ./install_systemd.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Chạy: sudo $0"
  exit 1
fi

RUN_USER="${SUDO_USER:-pi}"
HOME_DIR="$(getent passwd "$RUN_USER" | cut -d: -f6)"
HOME_DIR="${HOME_DIR:-/home/$RUN_USER}"

KIOSK_DIR="${KIOSK_DIR:-$HOME_DIR/kiosk}"
REMOTE_DIR="${REMOTE_DIR:-$HOME_DIR/smartkey_remote}"
OPS_DIR="${OPS_DIR:-$HOME_DIR/smartkey_ops}"
UNIT_SRC="$(cd "$(dirname "$0")/../units" && pwd)"

echo "User      : $RUN_USER"
echo "Home      : $HOME_DIR"
echo "Kiosk     : $KIOSK_DIR"
echo "Remote    : $REMOTE_DIR"
echo "Ops       : $OPS_DIR"
echo "Unit src  : $UNIT_SRC"

# Python
PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
  echo "Không tìm thấy python3"
  exit 1
fi

# Chọn script telegram
TG_SCRIPT=""
if [[ -f "$REMOTE_DIR/telegram_remote.py" ]]; then
  TG_SCRIPT="$REMOTE_DIR/telegram_remote.py"
elif [[ -f "$OPS_DIR/scripts/telegram_remote_basic.py" ]]; then
  TG_SCRIPT="$OPS_DIR/scripts/telegram_remote_basic.py"
elif [[ -f "$OPS_DIR/scripts/telegram_remote_confirm.py" ]]; then
  TG_SCRIPT="$OPS_DIR/scripts/telegram_remote_confirm.py"
fi

gen() {
  local src="$1"
  local dst="/etc/systemd/system/$(basename "$1")"
  sed \
    -e "s|/home/pi/kiosk|$KIOSK_DIR|g" \
    -e "s|/home/pi/smartkey_remote|$REMOTE_DIR|g" \
    -e "s|/home/pi/smartkey_ops|$OPS_DIR|g" \
    -e "s|/home/pi/.Xauthority|$HOME_DIR/.Xauthority|g" \
    -e "s|User=pi|User=$RUN_USER|g" \
    -e "s|Group=pi|Group=$RUN_USER|g" \
    -e "s|/usr/bin/python3|$PYTHON|g" \
    "$src" > "$dst"
  echo "  installed $dst"
}

echo "==> Cài unit files"
gen "$UNIT_SRC/smartkey-kiosk.service"
gen "$UNIT_SRC/smartkey-telegram-remote.service"
gen "$UNIT_SRC/smartkey-wifi-connect.service"
gen "$UNIT_SRC/smartkey-backup.service"
gen "$UNIT_SRC/smartkey-backup.timer"

# Sửa ExecStart telegram nếu tìm được script
if [[ -n "$TG_SCRIPT" ]]; then
  sed -i "s|ExecStart=.*telegram_remote.py|ExecStart=$PYTHON $TG_SCRIPT|" \
    /etc/systemd/system/smartkey-telegram-remote.service || true
  # confirm variant path
  if [[ "$TG_SCRIPT" == *confirm* ]]; then
    sed -i "s|^ExecStart=.*|ExecStart=$PYTHON $TG_SCRIPT|" \
      /etc/systemd/system/smartkey-telegram-remote.service
  fi
  echo "  Telegram script: $TG_SCRIPT"
else
  echo "  CẢNH BÁO: chưa thấy telegram_remote.py — sửa tay ExecStart trong unit"
fi

# Backup script path
if [[ -f "$OPS_DIR/scripts/backup_kiosk_data.sh" ]]; then
  sed -i "s|ExecStart=.*backup_kiosk_data.sh|ExecStart=/bin/bash $OPS_DIR/scripts/backup_kiosk_data.sh|" \
    /etc/systemd/system/smartkey-backup.service
fi

systemctl daemon-reload

echo ""
echo "==> Bật service (khuyến nghị):"
echo "    sudo systemctl enable --now smartkey-kiosk"
echo "    sudo systemctl enable --now smartkey-telegram-remote"
echo "    sudo systemctl enable --now smartkey-backup.timer"
echo ""
echo "    # WiFi portal (chỉ khi đã có /usr/local/bin/wifi-connect):"
echo "    sudo systemctl enable smartkey-wifi-connect"
echo ""
echo "==> Kiểm tra:"
echo "    systemctl status smartkey-kiosk"
echo "    journalctl -u smartkey-kiosk -f"
