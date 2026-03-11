#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/CopyCat}"
PORT="${PORT:-8060}"
WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-copycat-web}"
ENGINE_SERVICE_NAME="${ENGINE_SERVICE_NAME:-copycat-engine}"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nginx

if [ ! -d "$APP_DIR" ]; then
  echo "Missing app directory: $APP_DIR"
  exit 1
fi

RUN_USER="${RUN_USER:-$(stat -c '%U' "$APP_DIR")}"

cd "$APP_DIR"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

sed "s|User=ec2-user|User=$RUN_USER|g; s|/home/ec2-user/CopyCat|$APP_DIR|g; s|/home/ubuntu/CopyCat|$APP_DIR|g; s|PORT=8060|PORT=$PORT|g" deploy/copycat.service | sudo tee "/etc/systemd/system/${WEB_SERVICE_NAME}.service" >/dev/null
sed "s|User=ec2-user|User=$RUN_USER|g; s|/home/ec2-user/CopyCat|$APP_DIR|g; s|/home/ubuntu/CopyCat|$APP_DIR|g" deploy/copycat-engine.service | sudo tee "/etc/systemd/system/${ENGINE_SERVICE_NAME}.service" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$WEB_SERVICE_NAME" "$ENGINE_SERVICE_NAME"
sudo systemctl restart "$WEB_SERVICE_NAME" "$ENGINE_SERVICE_NAME"

sudo cp deploy/nginx-copycat.conf /etc/nginx/sites-available/copycat
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/copycat /etc/nginx/sites-enabled/copycat
sudo nginx -t
sudo systemctl restart nginx

echo "CopyCat deployed on port $PORT with services $WEB_SERVICE_NAME and $ENGINE_SERVICE_NAME as user $RUN_USER"
