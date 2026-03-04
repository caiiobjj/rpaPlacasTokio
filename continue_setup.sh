#!/bin/bash
# Continua setup a partir da etapa 6 (RabbitMQ e posteriores)
set -euo pipefail
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()  { echo -e "${GREEN}  ✓ $*${NC}"; }
inf() { echo -e "${CYAN}  → $*${NC}"; }
wrn() { echo -e "${YELLOW}  ! $*${NC}"; }

APP_DIR="/root/rpa-tokio"
VENV="$APP_DIR/.venv"
SERVICE_NAME="rpa-tokio"

echo -e "\n${CYAN}=== Continuando setup (etapas 6-13) ===${NC}\n"

# ── Limpa qualquer lock de apt anterior ──────────────────────────
inf "Limpando locks de apt..."
rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock 2>/dev/null || true
dpkg --configure -a 2>/dev/null || true
apt-get update -q
ok "apt pronto"

# ─────────────────────────────────────────────────────────────────
# ETAPA 6: RabbitMQ (via repositório padrão Ubuntu 22.04)
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[6/13] Instalando RabbitMQ...${NC}"
DEBIAN_FRONTEND=noninteractive apt-get install -y -q rabbitmq-server

rabbitmq-plugins enable rabbitmq_management 2>/dev/null || true

# Cria usuário admin para a RPA (ignora se já existir)
rabbitmqctl add_user rpa_admin "RpaT0ki0@2026" 2>/dev/null || \
    rabbitmqctl change_password rpa_admin "RpaT0ki0@2026"
rabbitmqctl set_user_tags rpa_admin administrator
rabbitmqctl set_permissions -p / rpa_admin ".*" ".*" ".*"
rabbitmqctl delete_user guest 2>/dev/null || true

systemctl enable rabbitmq-server
systemctl start rabbitmq-server
sleep 3
systemctl is-active rabbitmq-server && ok "RabbitMQ rodando (painel: http://5.161.79.179:15672)" \
    || wrn "RabbitMQ pode não ter iniciado — continuando mesmo assim"

# ─────────────────────────────────────────────────────────────────
# ETAPA 7: Swap de 2 GB
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[7/13] Configurando swap de 2 GB...${NC}"
if ! swapon --show | grep -q '/swapfile'; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    ok "Swap de 2 GB criado e ativado"
else
    ok "Swap já configurado: $(swapon --show | tail -1)"
fi
sysctl -w vm.swappiness=10 > /dev/null
grep -q 'vm.swappiness' /etc/sysctl.d/99-rpa.conf 2>/dev/null || \
    echo 'vm.swappiness=10' >> /etc/sysctl.d/99-rpa.conf

# ─────────────────────────────────────────────────────────────────
# ETAPA 8: /dev/shm 512 MB
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[8/13] Configurando /dev/shm para Chrome headless...${NC}"
mount -o remount,size=512M /dev/shm 2>/dev/null || true
if ! grep -q 'tmpfs /dev/shm' /etc/fstab; then
    echo 'tmpfs /dev/shm tmpfs rw,nosuid,nodev,size=512M 0 0' >> /etc/fstab
fi
ok "/dev/shm: $(df -h /dev/shm | tail -1 | awk '{print $2}')"

# ─────────────────────────────────────────────────────────────────
# ETAPA 9: Python venv + dependências
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[9/13] Configurando ambiente Python...${NC}"
mkdir -p "$APP_DIR"

# Recria venv se não existir
if [ ! -f "$VENV/bin/python" ]; then
    python3 -m venv "$VENV"
fi

"$VENV/bin/pip" install --upgrade pip -q

# Garante que redis está no requirements.txt
grep -q '^redis' "$APP_DIR/requirements.txt" 2>/dev/null || \
    echo 'redis>=5.0.0' >> "$APP_DIR/requirements.txt"

"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt" -q
ok "Dependências Python instaladas"
"$VENV/bin/python" --version

# ─────────────────────────────────────────────────────────────────
# ETAPA 10: Arquivo .env
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[10/13] Configurando .env...${NC}"
ENV_FILE="$APP_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
cat > "$ENV_FILE" << 'ENVEOF'
# ─── Credenciais do Portal ────────────────────────────────────────
USE_STATIC_CREDENTIALS=true

# ─── Pool de Drivers ─────────────────────────────────────────────
POOL_SIZE=5
HEADLESS=true

# ─── Timeouts (segundos) ─────────────────────────────────────────
TIMEOUT_DRIVER=60
TIMEOUT_MODAL=20
TIMEOUT_IFRAME=30
TIMEOUT_PAGE=15
MAX_RETRIES=3

# ─── Redis Cache ─────────────────────────────────────────────────
REDIS_URL=redis://127.0.0.1:6379/0
CACHE_TTL=3600

# ─── API Security ────────────────────────────────────────────────
# API_KEY=troque_por_chave_segura

# ─── RabbitMQ ────────────────────────────────────────────────────
RABBITMQ_URL=amqp://rpa_admin:RpaT0ki0@2026@127.0.0.1:5672/
ENVEOF
    ok ".env criado"
else
    sed -i 's/^POOL_SIZE=.*/POOL_SIZE=5/' "$ENV_FILE"
    sed -i 's/^MAX_RETRIES=.*/MAX_RETRIES=3/' "$ENV_FILE"
    grep -q '^REDIS_URL' "$ENV_FILE" || echo 'REDIS_URL=redis://127.0.0.1:6379/0' >> "$ENV_FILE"
    grep -q '^CACHE_TTL' "$ENV_FILE" || echo 'CACHE_TTL=3600' >> "$ENV_FILE"
    grep -q '^RABBITMQ_URL' "$ENV_FILE" || echo 'RABBITMQ_URL=amqp://rpa_admin:RpaT0ki0@2026@127.0.0.1:5672/' >> "$ENV_FILE"
    ok ".env atualizado"
fi
chmod 600 "$ENV_FILE"

# ─────────────────────────────────────────────────────────────────
# ETAPA 11: Serviço systemd
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[11/13] Configurando serviço systemd...${NC}"

cat > /etc/systemd/system/${SERVICE_NAME}.service << SVCEOF
[Unit]
Description=TokioMarine RPA API — Pool de Drivers + Redis Cache
After=network-online.target redis-server.service
Wants=network-online.target
Requires=redis-server.service

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env

ExecStart=${VENV}/bin/python api.py
ExecStop=/bin/kill -SIGTERM \$MAINPID

Restart=on-failure
RestartSec=20
StartLimitIntervalSec=180
StartLimitBurst=5

StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

MemoryMax=5G
MemoryHigh=4G
TasksMax=512
TimeoutStartSec=180
TimeoutStopSec=60
KillMode=mixed
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
ok "Serviço systemd configurado"

# ─────────────────────────────────────────────────────────────────
# ETAPA 12: Logrotate + backup SQLite
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[12/13] Configurando logrotate e backup SQLite...${NC}"

mkdir -p /etc/systemd/journald.conf.d/
cat > /etc/systemd/journald.conf.d/rpa.conf << 'JEOF'
[Journal]
SystemMaxUse=200M
SystemKeepFree=500M
MaxFileSec=7day
JEOF
systemctl restart systemd-journald 2>/dev/null || true

cat > /etc/cron.daily/rpa-sqlite-backup << CRONEOF
#!/bin/bash
DB="${APP_DIR}/rpa_logs.db"
BAK="${APP_DIR}/backups"
mkdir -p "\$BAK"
[ -f "\$DB" ] && sqlite3 "\$DB" ".backup \$BAK/rpa_logs_\$(date +%Y%m%d).db"
find "\$BAK" -name "rpa_logs_*.db" -mtime +7 -delete
CRONEOF
chmod +x /etc/cron.daily/rpa-sqlite-backup
mkdir -p "$APP_DIR/backups"
ok "Logrotate e backup SQLite configurados"

# ─────────────────────────────────────────────────────────────────
# ETAPA 13: Testes finais + iniciar serviço
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[13/13] Testes finais e inicialização...${NC}"

# Teste ChromeDriver
inf "Testando ChromeDriver headless..."
"$VENV/bin/python" - << 'PYEOF'
import shutil, sys
from selenium import webdriver
from selenium.webdriver.chrome.service import Service

driver_path = shutil.which('chromedriver') or '/usr/local/bin/chromedriver'
opts = webdriver.ChromeOptions()
opts.add_argument('--headless=new')
opts.add_argument('--no-sandbox')
opts.add_argument('--disable-dev-shm-usage')
opts.add_argument('--disable-setuid-sandbox')
opts.add_argument('--disable-gpu')
try:
    d = webdriver.Chrome(service=Service(driver_path), options=opts)
    d.get('https://www.google.com')
    print(f"  ChromeDriver OK — titulo: {d.title}")
    d.quit()
except Exception as e:
    print(f"  ERRO ChromeDriver: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
ok "ChromeDriver OK"

# Teste Redis
inf "Testando Redis..."
redis-cli set rpa_test "ok" EX 5 > /dev/null
VAL=$(redis-cli get rpa_test)
[ "$VAL" = "ok" ] && ok "Redis OK" || { echo "Redis falhou"; exit 1; }
redis-cli del rpa_test > /dev/null

# Para serviço antigo se estiver rodando
systemctl stop "$SERVICE_NAME" 2>/dev/null || true
sleep 3

# Inicia o serviço
inf "Iniciando $SERVICE_NAME..."
systemctl start "$SERVICE_NAME"

inf "Aguardando pool de drivers inicializar (~25s)..."
sleep 25

if systemctl is-active "$SERVICE_NAME" > /dev/null 2>&1; then
    ok "Serviço ativo!"
    HEALTH=$(curl -sf http://localhost:8000/health 2>/dev/null || echo '{}')
    echo "  Health: $HEALTH"
else
    echo -e "${RED}  Serviço não iniciou. Logs:${NC}"
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager
fi

# ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  Setup concluído!${NC}"
echo -e "${GREEN}============================================================${NC}"
echo ""
echo    "  Dashboard : http://5.161.79.179:8000/"
echo    "  Health    : http://5.161.79.179:8000/health"
echo    "  RabbitMQ  : http://5.161.79.179:15672  (rpa_admin / RpaT0ki0@2026)"
echo ""
echo    "  Logs      : journalctl -u rpa-tokio -f"
echo    "  Banco     : sqlite3 /root/rpa-tokio/rpa_logs.db"
echo ""
