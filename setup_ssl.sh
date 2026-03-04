#!/bin/bash
# Setup script: SSL cert + systemd update for HTTPS

APP_DIR="/root/rpa-tokio"
SSL_DIR="$APP_DIR/ssl"

# 1. Generate self-signed SSL certificate
mkdir -p "$SSL_DIR"
if [ ! -f "$SSL_DIR/cert.pem" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$SSL_DIR/key.pem" \
    -out    "$SSL_DIR/cert.pem" \
    -days   3650 \
    -subj   "/C=BR/ST=SP/L=SaoPaulo/O=TokioMarine/CN=5.161.79.179" \
    2>&1 && echo "[OK] Certificado SSL gerado."
else
  echo "[SKIP] Certificado ja existe."
fi

chmod 600 "$SSL_DIR/key.pem"
ls -lh "$SSL_DIR/"

# 2. Update systemd service to use HTTPS using Python for safe string manipulation
SERVICE_FILE="/etc/systemd/system/rpa-tokio.service"
echo "=== ExecStart atual ==="
grep ExecStart "$SERVICE_FILE"

python3 - "$SERVICE_FILE" "$SSL_DIR" <<'PYEOF'
import sys, re

service_file = sys.argv[1]
ssl_dir      = sys.argv[2]

with open(service_file) as f:
    content = f.read()

if '--ssl-keyfile' in content:
    print('[SKIP] SSL ja configurado no servico.')
    sys.exit(0)

ssl_args = f' --ssl-keyfile {ssl_dir}/key.pem --ssl-certfile {ssl_dir}/cert.pem'
content = re.sub(
    r'(^ExecStart=.+)$',
    lambda m: m.group(1) + ssl_args,
    content,
    flags=re.MULTILINE,
)
with open(service_file, 'w') as f:
    f.write(content)
print('[OK] ExecStart atualizado com SSL.')
PYEOF

echo "=== ExecStart novo ==="
grep ExecStart "$SERVICE_FILE"

# 3. Reload + restart systemd service
systemctl daemon-reload
systemctl restart rpa-tokio
sleep 3
systemctl status rpa-tokio --no-pager | head -6
echo "SETUP_DONE"
