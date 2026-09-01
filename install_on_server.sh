#!/bin/bash
# Run ON the AWS server (EC2 Instance Connect). Installs the invoice app
# so clients open http://SERVER_IP in a browser.
# Database stays on this machine (PostgreSQL fss_invoice).
# Invoice PDF/Excel files are stored in /var/lib/fss-invoice/Invoices.
set -euo pipefail

APP=/opt/fss-invoice
DATA=/var/lib/fss-invoice
REPO="https://github.com/sanjaybzpal-code/FSS-Invoice-Tool--.git"
DB_USER="${FSS_DB_USER:-fss2025}"
DB_PASS="${FSS_DB_PASS:-}"
DB_NAME="${FSS_DB_NAME:-fss_invoice}"
ADMIN_USER="${FSS_ADMIN_USER:-admin}"
ADMIN_PASS="${FSS_ADMIN_PASS:-}"
PUBLIC_IP="$(curl -fsS --max-time 5 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || hostname -I | awk '{print $1}')"

if [ -z "$DB_PASS" ] || [ -z "$ADMIN_PASS" ]; then
  echo "Set FSS_DB_PASS and FSS_ADMIN_PASS then re-run."
  echo "Example:"
  echo "  sudo FSS_DB_PASS='your-db-password' FSS_ADMIN_PASS='site-login-password' bash $0"
  exit 1
fi

if [ -f /etc/debian_version ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y git python3 python3-venv python3-pip nginx curl
elif [ -f /etc/redhat-release ] || [ -f /etc/os-release ]; then
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y git python3 python3-pip nginx curl
    python3 -m ensurepip --upgrade 2>/dev/null || true
  else
    yum install -y git python3 python3-pip nginx curl
  fi
fi

mkdir -p "$DATA/Invoices" "$APP"
if [ ! -d "$APP/.git" ]; then
  git clone "$REPO" "$APP"
else
  git -C "$APP" fetch --all
  git -C "$APP" reset --hard origin/main
fi

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -U pip
"$APP/.venv/bin/pip" install -r "$APP/requirements-linux.txt"

ENC_PASS=$(python3 -c "from urllib.parse import quote_plus; print(quote_plus('''$DB_PASS'''))")
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

cat > "$APP/.env" <<EOF
FSS_DATA_DIR=$DATA
DATABASE_URL=postgresql://${DB_USER}:${ENC_PASS}@127.0.0.1:5432/${DB_NAME}?sslmode=disable
PGHOST=127.0.0.1
PGPORT=5432
PGDATABASE=$DB_NAME
PGUSER=$DB_USER
PGPASSWORD=$DB_PASS
PGSSLMODE=disable
FLASK_SECRET_KEY=$SECRET
ADMIN_USERNAME=$ADMIN_USER
ADMIN_PASSWORD=$ADMIN_PASS
PUBLIC_URL=http://${PUBLIC_IP}
EOF
chmod 600 "$APP/.env"

cp "$APP/deploy/fss-invoice.service" /etc/systemd/system/fss-invoice.service
if [ -d /etc/nginx/sites-enabled ]; then
  rm -f /etc/nginx/sites-enabled/default
  cp "$APP/deploy/nginx-fss-invoice.conf" /etc/nginx/sites-available/fss-invoice
  ln -sfn /etc/nginx/sites-available/fss-invoice /etc/nginx/sites-enabled/fss-invoice
else
  cp "$APP/deploy/nginx-fss-invoice.conf" /etc/nginx/conf.d/fss-invoice.conf
  rm -f /etc/nginx/conf.d/default.conf 2>/dev/null || true
fi

systemctl daemon-reload
systemctl enable --now fss-invoice
nginx -t
systemctl enable --now nginx
systemctl reload nginx || systemctl restart nginx

if command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-service=http || true
  firewall-cmd --reload || true
fi
if command -v ufw >/dev/null 2>&1; then
  ufw allow 80/tcp || true
fi

echo
echo "OK — clients open: http://${PUBLIC_IP}"
echo "Login: $ADMIN_USER  (password you set as FSS_ADMIN_PASS)"
echo "Invoices folder: $DATA/Invoices"
echo "AWS Security Group must allow inbound TCP 80 from 0.0.0.0/0"
