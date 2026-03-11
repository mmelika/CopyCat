#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ec2-user/CopyCat}"
BRANCH="${BRANCH:-main}"
WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-copycat-web}"
ENGINE_SERVICE_NAME="${ENGINE_SERVICE_NAME:-copycat-engine}"

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

sudo systemctl restart "$WEB_SERVICE_NAME" "$ENGINE_SERVICE_NAME"
sudo systemctl is-active --quiet "$WEB_SERVICE_NAME"
sudo systemctl is-active --quiet "$ENGINE_SERVICE_NAME"

echo "Deployed $(git rev-parse --short HEAD) to $APP_DIR"
