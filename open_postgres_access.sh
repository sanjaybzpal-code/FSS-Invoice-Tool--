#!/bin/bash
# Run this ON the AWS server (EC2 console → Connect → Session Manager / EC2 Instance Connect).
# Allows office PC + Vercel to use database fss_invoice.

set -e
LINE='host    all    all    0.0.0.0/0    md5'
HBA=""
for f in /etc/postgresql/*/main/pg_hba.conf \
         /var/lib/pgsql/data/pg_hba.conf \
         /var/lib/pgsql/*/data/pg_hba.conf \
         /pgsql/data/pg_hba.conf; do
  if [ -f "$f" ]; then HBA="$f"; break; fi
done
if [ -z "$HBA" ]; then
  HBA=$(find /etc /var/lib -name pg_hba.conf 2>/dev/null | head -1)
fi
if [ -z "$HBA" ]; then
  echo "pg_hba.conf not found"
  exit 1
fi
echo "Using $HBA"
grep -qF "0.0.0.0/0" "$HBA" || echo "$LINE" >> "$HBA"
CONF=$(dirname "$HBA")/postgresql.conf
if [ -f "$CONF" ]; then
  sed -i "s/^#listen_addresses =.*/listen_addresses = '*'/" "$CONF" || true
  grep -q "^listen_addresses" "$CONF" || echo "listen_addresses = '*'" >> "$CONF"
fi
(systemctl reload postgresql 2>/dev/null || \
 systemctl reload postgresql-16 2>/dev/null || \
 systemctl reload postgresql-15 2>/dev/null || \
 systemctl reload postgresql-14 2>/dev/null || \
 service postgresql reload 2>/dev/null || true)
echo "Done. From office PC, invoices will use 43.205.3.136 / fss_invoice."
