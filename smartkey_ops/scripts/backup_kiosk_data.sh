#!/bin/bash
# Sao lưu kiosk_data (users, face DB, logs, config)
# Usage:
#   ./backup_kiosk_data.sh
#   KIOSK_DATA_DIR=/path/to/kiosk_data ./backup_kiosk_data.sh
#   ./backup_kiosk_data.sh /path/to/kiosk_data

set -euo pipefail

DATA_DIR="${1:-${KIOSK_DATA_DIR:-./kiosk_data}}"
DATA_DIR="$(cd "$(dirname "$DATA_DIR")" 2>/dev/null && pwd)/$(basename "$DATA_DIR")" 2>/dev/null || true
# fallback nếu chưa tồn tại
if [[ ! -d "${1:-${KIOSK_DATA_DIR:-./kiosk_data}}" ]]; then
  DATA_DIR="${1:-${KIOSK_DATA_DIR:-./kiosk_data}}"
fi

BACKUP_ROOT="${BACKUP_ROOT:-./backups}"
STAMP=$(date +%Y%m%d_%H%M%S)
DEST="${BACKUP_ROOT}/kiosk_data_${STAMP}"

mkdir -p "$BACKUP_ROOT"

if [[ ! -d "$DATA_DIR" ]]; then
  echo "Không thấy thư mục: $DATA_DIR"
  echo "  export KIOSK_DATA_DIR=... hoặc truyền path làm tham số."
  exit 1
fi

mkdir -p "$DEST"
# copy có bảo toàn quyền cơ bản
cp -a "$DATA_DIR/." "$DEST/" 2>/dev/null || cp -r "$DATA_DIR/." "$DEST/"

# nén
ARCHIVE="${DEST}.tar.gz"
tar -czf "$ARCHIVE" -C "$BACKUP_ROOT" "kiosk_data_${STAMP}"
rm -rf "$DEST"

# giữ tối đa 20 bản (xoá cũ)
ls -1t "$BACKUP_ROOT"/kiosk_data_*.tar.gz 2>/dev/null | tail -n +21 | while read -r old; do
  rm -f "$old"
done

echo "OK backup → $ARCHIVE"
ls -lh "$ARCHIVE"
