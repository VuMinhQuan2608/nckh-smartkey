#!/bin/bash
# Khôi phục kiosk_data từ file .tar.gz do backup_kiosk_data.sh tạo
# Usage:
#   ./restore_kiosk_data.sh backups/kiosk_data_20260926_180000.tar.gz
#   KIOSK_DATA_DIR=./kiosk_data ./restore_kiosk_data.sh file.tar.gz

set -euo pipefail

ARCHIVE="${1:-}"
DATA_DIR="${KIOSK_DATA_DIR:-./kiosk_data}"

if [[ -z "$ARCHIVE" || ! -f "$ARCHIVE" ]]; then
  echo "Usage: $0 <backup.tar.gz>"
  echo "  KIOSK_DATA_DIR=./kiosk_data $0 backups/kiosk_data_XXX.tar.gz"
  exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

tar -xzf "$ARCHIVE" -C "$TMP"
# thư mục con duy nhất
INNER=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)
if [[ -z "$INNER" ]]; then
  echo "Archive không hợp lệ"
  exit 1
fi

mkdir -p "$DATA_DIR"
# backup hiện tại trước khi ghi đè
if [[ -d "$DATA_DIR" ]] && [[ -n "$(ls -A "$DATA_DIR" 2>/dev/null || true)" ]]; then
  SAFETY="./backups/pre_restore_$(date +%Y%m%d_%H%M%S).tar.gz"
  mkdir -p ./backups
  tar -czf "$SAFETY" -C "$(dirname "$DATA_DIR")" "$(basename "$DATA_DIR")" 2>/dev/null || true
  echo "Đã snapshot hiện tại → $SAFETY"
fi

rm -rf "${DATA_DIR:?}/"*
cp -a "$INNER"/. "$DATA_DIR/"
echo "OK restore → $DATA_DIR"
ls -la "$DATA_DIR"
