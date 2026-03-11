#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/CopyCat}"
PORT="${PORT:-8060}"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nginx

if [ ! -d "$APP_DIR" ]; then
  echo "Missing app directory: $APP_DIR"
  exit 1
fi

cd "$APP_DIR"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

sed "s|/home/ubuntu/CopyCat|$APP_DIR|g; s|PORT=8060|PORT=$PORT|g" deploy/copycat.service | sudo tee /etc/systemd/system/copycat.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable copycat
sudo systemctl restart copycat

sudo cp deploy/nginx-copycat.conf /etc/nginx/sites-available/copycat
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/copycat /etc/nginx/sites-enabled/copycat
sudo nginx -t
sudo systemctl restart nginx

echo "CopyCat deployed on port $PORT"
