#!/bin/bash
set -e
APP_DIR="/root/rpa-tokio"
VENV="$APP_DIR/.venv"

echo "=== FINISH SETUP ==="

# ── 1. Instalar dependências Python ──────────────────────────────────────────
echo "[1/5] Instalando dependências Python..."
$VENV/bin/pip install --quiet --upgrade pip
$VENV/bin/pip install --quiet -r $APP_DIR/requirements.txt
echo "    Dependências OK"

# ── 2. Criar .env ─────────────────────────────────────────────────────────────
echo "[2/5] Criando .env..."
cat > $APP_DIR/.env << 'ENVEOF'
POOL_SIZE=5
MAX_RETRIES=3
REDIS_URL=redis://localhost:6379/0
CACHE_TTL=3600
HEADLESS=true
# API_KEY=troque_por_chave_segura
ENVEOF
echo "    .env OK"

# ── 3. Pasta static ───────────────────────────────────────────────────────────
echo "[3/5] Criando pasta static..."
mkdir -p $APP_DIR/static
echo "    static/ OK"

# ── 4. Serviço systemd ────────────────────────────────────────────────────────
echo "[4/5] Configurando serviço systemd..."
cat > /etc/systemd/system/rpa-tokio.service << 'SVCEOF'
[Unit]
Description=RPA Tokio Marine - Consulta Placas
After=network.target redis.service
Wants=redis.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/rpa-tokio
EnvironmentFile=/root/rpa-tokio/.env
ExecStart=/root/rpa-tokio/.venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000 --workers 1
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
MemoryMax=5G
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable rpa-tokio
echo "    Serviço configurado OK"

# ── 5. Logrotate + cron backup SQLite ─────────────────────────────────────────
echo "[5/5] Configurando logrotate e cron..."
cat > /etc/logrotate.d/rpa-tokio << 'LOGEOF'
/var/log/rpa-tokio.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    systemd
}
LOGEOF

# Backup diário do SQLite
(crontab -l 2>/dev/null | grep -v rpa_logs; echo "0 3 * * * cp $APP_DIR/rpa_logs.db $APP_DIR/rpa_logs_backup_\$(date +\%Y\%m\%d).db 2>/dev/null") | crontab -
echo "    Logrotate e cron OK"

# ── Verificações finais ────────────────────────────────────────────────────────
echo ""
echo "=== Testes rápidos ==="
echo -n "Redis: "
redis-cli ping

echo -n "RabbitMQ: "
systemctl is-active rabbitmq-server

echo -n "ChromeDriver: "
chromedriver --version 2>/dev/null | head -1

echo -n "Python packages: "
$VENV/bin/python -c "import fastapi, selenium, uvicorn; print('OK')" 2>&1

# ── Iniciar serviço ────────────────────────────────────────────────────────────
echo ""
echo "=== Iniciando serviço rpa-tokio ==="
systemctl start rpa-tokio
sleep 5

echo -n "Status: "
systemctl is-active rpa-tokio

echo ""
echo "=== Verificando health check ==="
curl -s --max-time 15 http://localhost:8000/health || echo "Serviço ainda inicializando (normal, demora ~30s)"

echo ""
echo "=== SETUP CONCLUÍDO ==="
echo "Dashboard: http://5.161.79.179:8000/"
echo "Health:    http://5.161.79.179:8000/health"
echo "RabbitMQ:  http://5.161.79.179:15672 (rpa_admin / RpaT0ki0@2026)"
