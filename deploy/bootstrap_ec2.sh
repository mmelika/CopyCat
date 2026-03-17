#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/PolyCopy}"
PORT="${PORT:-8060}"
WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-polycopy-web}"
ENGINE_SERVICE_NAME="${ENGINE_SERVICE_NAME:-polycopy-engine}"
CONFIGURE_NGINX="${CONFIGURE_NGINX:-1}"

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

sed "s|User=ec2-user|User=$RUN_USER|g; s|/home/ec2-user/CopyCat|$APP_DIR|g; s|/home/ubuntu/CopyCat|$APP_DIR|g; s|/home/ec2-user/PolyCopy|$APP_DIR|g; s|/home/ubuntu/PolyCopy|$APP_DIR|g; s|PORT=8060|PORT=$PORT|g; s|--bind 0.0.0.0:8060|--bind 0.0.0.0:$PORT|g" deploy/copycat.service | sudo tee "/etc/systemd/system/${WEB_SERVICE_NAME}.service" >/dev/null
sed "s|User=ec2-user|User=$RUN_USER|g; s|/home/ec2-user/CopyCat|$APP_DIR|g; s|/home/ubuntu/CopyCat|$APP_DIR|g; s|/home/ec2-user/PolyCopy|$APP_DIR|g; s|/home/ubuntu/PolyCopy|$APP_DIR|g" deploy/copycat-engine.service | sudo tee "/etc/systemd/system/${ENGINE_SERVICE_NAME}.service" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$WEB_SERVICE_NAME" "$ENGINE_SERVICE_NAME"
sudo systemctl restart "$WEB_SERVICE_NAME" "$ENGINE_SERVICE_NAME"

if [ "$CONFIGURE_NGINX" = "1" ]; then
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
fi

PUBLIC_IP=""
if command -v curl >/dev/null 2>&1; then
  TOKEN="$(curl -fsS -m 2 -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)"
  if [ -n "$TOKEN" ]; then
    PUBLIC_IP="$(curl -fsS -m 2 -H "X-aws-ec2-metadata-token: $TOKEN" "http://169.254.169.254/latest/meta-data/public-ipv4" || true)"
  else
    PUBLIC_IP="$(curl -fsS -m 2 "http://169.254.169.254/latest/meta-data/public-ipv4" || true)"
  fi
fi

echo "PolyCopy deployed with services $WEB_SERVICE_NAME and $ENGINE_SERVICE_NAME as user $RUN_USER"
if [ "$CONFIGURE_NGINX" = "1" ]; then
  echo "Nginx listens on port 80 and proxies to the app on port $PORT"
  if [ -n "$PUBLIC_IP" ]; then
    echo "Open http://$PUBLIC_IP in your browser"
  else
    echo "Open http://YOUR_EC2_PUBLIC_IP in your browser"
  fi
  echo "Your EC2 security group must allow inbound TCP 80 for the public site"
  echo "Allow inbound TCP $PORT only if you want direct app access without nginx"
else
  if [ -n "$PUBLIC_IP" ]; then
    echo "Instance launched on port $PORT at http://$PUBLIC_IP:$PORT"
  else
    echo "Instance launched on port $PORT at http://YOUR_EC2_PUBLIC_IP:$PORT"
  fi
  echo "Nginx was left unchanged because CONFIGURE_NGINX=0"
  echo "Your EC2 security group must allow inbound TCP $PORT for direct access"
fi
