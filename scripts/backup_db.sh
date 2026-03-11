#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_DIR="${DATA_DIR:-$APP_DIR/data}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DATA_DIR/copytrader.db" ]; then
  echo "No database found at $DATA_DIR/copytrader.db"
  exit 1
fi

sqlite3 "$DATA_DIR/copytrader.db" ".backup '$BACKUP_DIR/copytrader-$STAMP.db'"
gzip -f "$BACKUP_DIR/copytrader-$STAMP.db"
find "$BACKUP_DIR" -type f -name 'copytrader-*.db.gz' -mtime +7 -delete
echo "Created backup: $BACKUP_DIR/copytrader-$STAMP.db.gz"
