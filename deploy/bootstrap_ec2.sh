#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/PolyCopy}"
PORT="${PORT:-8060}"
WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-polycopy-web}"
ENGINE_SERVICE_NAME="${ENGINE_SERVICE_NAME:-polycopy-engine}"

if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip nginx
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y python3 python3-pip nginx
elif command -v yum >/dev/null 2>&1; then
  sudo yum install -y python3 python3-pip nginx
else
  echo "Unsupported package manager. Need apt-get, dnf, or yum."
  exit 1
fi

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

sed "s|User=ec2-user|User=$RUN_USER|g; s|/home/ec2-user/CopyCat|$APP_DIR|g; s|/home/ubuntu/CopyCat|$APP_DIR|g; s|/home/ec2-user/PolyCopy|$APP_DIR|g; s|/home/ubuntu/PolyCopy|$APP_DIR|g; s|PORT=8060|PORT=$PORT|g" deploy/copycat.service | sudo tee "/etc/systemd/system/${WEB_SERVICE_NAME}.service" >/dev/null
sed "s|User=ec2-user|User=$RUN_USER|g; s|/home/ec2-user/CopyCat|$APP_DIR|g; s|/home/ubuntu/CopyCat|$APP_DIR|g; s|/home/ec2-user/PolyCopy|$APP_DIR|g; s|/home/ubuntu/PolyCopy|$APP_DIR|g" deploy/copycat-engine.service | sudo tee "/etc/systemd/system/${ENGINE_SERVICE_NAME}.service" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$WEB_SERVICE_NAME" "$ENGINE_SERVICE_NAME"
sudo systemctl restart "$WEB_SERVICE_NAME" "$ENGINE_SERVICE_NAME"

if [ -d /etc/nginx/sites-available ] && [ -d /etc/nginx/sites-enabled ]; then
  sudo cp deploy/nginx-copycat.conf /etc/nginx/sites-available/polycopy
  sudo rm -f /etc/nginx/sites-enabled/default
  sudo ln -sf /etc/nginx/sites-available/polycopy /etc/nginx/sites-enabled/polycopy
elif [ -d /etc/nginx/conf.d ]; then
  sudo cp deploy/nginx-copycat.conf /etc/nginx/conf.d/polycopy.conf
else
  echo "Unsupported nginx layout. Expected sites-available/sites-enabled or conf.d."
  exit 1
fi
sudo nginx -t
sudo systemctl restart nginx

echo "PolyCopy deployed on port $PORT with services $WEB_SERVICE_NAME and $ENGINE_SERVICE_NAME as user $RUN_USER"
