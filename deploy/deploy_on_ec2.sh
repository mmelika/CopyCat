#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ec2-user/CopyCat}"
BRANCH="${BRANCH:-main}"
SERVICE_NAME="${SERVICE_NAME:-copycat}"

if [ ! -d "$APP_DIR/.git" ]; then
  echo "Missing git checkout at $APP_DIR"
  exit 1
fi

cd "$APP_DIR"

git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

sudo systemctl restart "$SERVICE_NAME"
sudo systemctl is-active --quiet "$SERVICE_NAME"

echo "Deployed $(git rev-parse --short HEAD) to $APP_DIR"
