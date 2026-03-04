#!/bin/bash
# =============================================================================
# setup_ubuntu.sh — RPA TokioMarine — Ubuntu 22.04 LTS (Jammy)
# Servidor: 8 GB RAM · 4 vCPU · IP 5.161.79.179
#
# O que este script faz:
#   1.  Força IPv4 em apt (evita falhas de conectividade IPv6 em VPS)
#   2.  Atualiza sistema e instala dependências de sistema
#   3.  Instala Google Chrome (versão atual estável)
#   4.  Instala ChromeDriver manualmente (sem webdriver-manager)
#   5.  Instala e configura Redis (cache de placas)
#   6.  Instala RabbitMQ (fila de tarefas — pronto para uso futuro)
#   7.  Cria swap de 2 GB (segurança contra OOM)
#   8.  Configura /dev/shm de 512 MB (necessário para Chrome headless)
#   9.  Cria venv Python e instala dependências
#  10.  Cria .env com configurações otimizadas para este servidor
#  11.  Instala serviço systemd com limites adequados
#  12.  Configura logrotate para journald e SQLite backup
#  13.  Testa ChromeDriver e Redis antes de finalizar
#
# Uso:
#   scp setup_ubuntu.sh root@5.161.79.179:/root/
#   scp -r . root@5.161.79.179:/root/rpa-tokio/
#   ssh root@5.161.79.179 "bash /root/rpa-tokio/setup_ubuntu.sh"
# =============================================================================
set -euo pipefail

# Cores para output
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()  { echo -e "${GREEN}  ✓ $*${NC}"; }
inf() { echo -e "${CYAN}  → $*${NC}"; }
wrn() { echo -e "${YELLOW}  ! $*${NC}"; }
err() { echo -e "${RED}  ✗ $*${NC}"; exit 1; }

APP_DIR="/root/rpa-tokio"
VENV="$APP_DIR/.venv"
SERVICE_NAME="rpa-tokio"

echo ""
echo -e "${CYAN}============================================================${NC}"
echo -e "${CYAN}  RPA TokioMarine — Setup Ubuntu 22.04 (Jammy)${NC}"
echo -e "${CYAN}  Servidor: 8 GB RAM · 4 vCPU · IP 5.161.79.179${NC}"
echo -e "${CYAN}============================================================${NC}"
echo ""

# ─────────────────────────────────────────────────────────────────
# ETAPA 0: Forçar IPv4 em apt (crítico para VPS com IPv6 quebrado)
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[0/13] Forçando IPv4 para apt e gai.conf...${NC}"
echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4

# Prioriza IPv4 sobre IPv6 nas conexões do sistema
sed -i 's/^#\?precedence ::ffff:0:0\/96.*/precedence ::ffff:0:0\/96  100/' /etc/gai.conf || \
    echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf
ok "IPv4 forçado"

# ─────────────────────────────────────────────────────────────────
# ETAPA 1: Atualizar sistema
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[1/13] Atualizando sistema...${NC}"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -q
ok "Sistema atualizado"

# ─────────────────────────────────────────────────────────────────
# ETAPA 2: Dependências de sistema
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[2/13] Instalando dependências do sistema...${NC}"
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    python3.11 python3.11-venv python3-pip \
    curl wget unzip gnupg ca-certificates lsb-release \
    fonts-liberation libappindicator3-1 \
    libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
    libgdk-pixbuf2.0-0 libnspr4 libnss3 libxss1 \
    libx11-xcb1 libxcomposite1 libxcursor1 libxdamage1 \
    libxfixes3 libxi6 libxrandr2 libxtst6 \
    libasound2 libgbm1 libxkbcommon0 \
    xdg-utils jq net-tools htop iotop \
    sqlite3
ok "Dependências de sistema instaladas"

# ─────────────────────────────────────────────────────────────────
# ETAPA 3: Google Chrome
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[3/13] Instalando Google Chrome...${NC}"
if ! command -v google-chrome &>/dev/null; then
    inf "Baixando Chrome estável..."
    wget -4 -q -O /tmp/chrome.deb \
        "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q /tmp/chrome.deb
    rm /tmp/chrome.deb
fi
CHROME_VERSION=$(google-chrome --version | grep -oP '[\d.]+' | head -1)
ok "Chrome instalado: $CHROME_VERSION"

# ─────────────────────────────────────────────────────────────────
# ETAPA 4: ChromeDriver (sem webdriver-manager para evitar problema IPv6)
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[4/13] Instalando ChromeDriver...${NC}"
CHROME_MAJOR=$(echo "$CHROME_VERSION" | cut -d. -f1)
inf "Chrome major version: $CHROME_MAJOR"

# API de versões do ChromeDriver (Chrome for Testing)
DRIVER_VERSION=$(curl -4 -fsSL \
    "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_${CHROME_MAJOR}" \
    2>/dev/null || echo "")

if [ -z "$DRIVER_VERSION" ]; then
    wrn "Não foi possível detectar versão do ChromeDriver. Usando mesma versão do Chrome: $CHROME_VERSION"
    DRIVER_VERSION="$CHROME_VERSION"
fi
inf "ChromeDriver versão: $DRIVER_VERSION"

DRIVER_URL="https://storage.googleapis.com/chrome-for-testing-public/${DRIVER_VERSION}/linux64/chromedriver-linux64.zip"
inf "Baixando de: $DRIVER_URL"
wget -4 -q -O /tmp/chromedriver.zip "$DRIVER_URL" || \
    wget -4 -q -O /tmp/chromedriver.zip \
        "https://chromedriver.storage.googleapis.com/${DRIVER_VERSION}/chromedriver_linux64.zip"

unzip -o -q /tmp/chromedriver.zip -d /tmp/chromedriver_extract/
DRIVER_BIN=$(find /tmp/chromedriver_extract -name "chromedriver" -type f | head -1)
mv "$DRIVER_BIN" /usr/local/bin/chromedriver
chmod +x /usr/local/bin/chromedriver
rm -rf /tmp/chromedriver.zip /tmp/chromedriver_extract
ok "ChromeDriver instalado: $(/usr/local/bin/chromedriver --version)"

# ─────────────────────────────────────────────────────────────────
# ETAPA 5: Redis
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[5/13] Instalando e configurando Redis...${NC}"
DEBIAN_FRONTEND=noninteractive apt-get install -y -q redis-server

# Configura Redis para performance em cache de RPA
REDIS_CONF="/etc/redis/redis.conf"
# Máximo de memória: 512MB com política LRU
sed -i 's/^# maxmemory .*/maxmemory 512mb/' "$REDIS_CONF"
grep -q '^maxmemory ' "$REDIS_CONF" || echo 'maxmemory 512mb' >> "$REDIS_CONF"
sed -i 's/^# maxmemory-policy .*/maxmemory-policy allkeys-lru/' "$REDIS_CONF"
grep -q '^maxmemory-policy ' "$REDIS_CONF" || echo 'maxmemory-policy allkeys-lru' >> "$REDIS_CONF"
# Save desabilitado (cache efêmero, não precisa persistir)
sed -i 's/^save 900/# save 900/' "$REDIS_CONF"
sed -i 's/^save 300/# save 300/' "$REDIS_CONF"
sed -i 's/^save 60/# save 60/' "$REDIS_CONF"
# Bind apenas localhost (segurança)
sed -i 's/^bind .*/bind 127.0.0.1 ::1/' "$REDIS_CONF"

systemctl enable redis-server
systemctl restart redis-server
sleep 2
redis-cli ping | grep -q PONG && ok "Redis rodando (512 MB, LRU, localhost-only)" || err "Redis não respondeu ao ping"

# ─────────────────────────────────────────────────────────────────
# ETAPA 6: RabbitMQ (fila de tarefas — pronto para uso futuro)
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[6/13] Instalando RabbitMQ...${NC}"

# Adiciona repositório oficial do RabbitMQ
curl -4 -fsSL https://packagecloud.io/rabbitmq/rabbitmq-server/gpgkey \
    | gpg --dearmor > /usr/share/keyrings/rabbitmq-archive-keyring.gpg

cat > /etc/apt/sources.list.d/rabbitmq.list << 'RMQEOF'
deb [signed-by=/usr/share/keyrings/rabbitmq-archive-keyring.gpg] \
    https://packagecloud.io/rabbitmq/rabbitmq-server/ubuntu/ jammy main
RMQEOF

# Instala Erlang (dependência do RabbitMQ)
DEBIAN_FRONTEND=noninteractive apt-get install -y -q erlang-base \
    erlang-asn1 erlang-crypto erlang-eldap erlang-ftp erlang-inets \
    erlang-mnesia erlang-os-mon erlang-parsetools erlang-public-key \
    erlang-runtime-tools erlang-snmp erlang-ssl erlang-syntax-tools \
    erlang-tftp erlang-tools erlang-xmerl 2>/dev/null || \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q erlang 2>/dev/null || \
    wrn "Erlang completo não instalado — tentando versão mínima..."

apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q rabbitmq-server || {
    wrn "Repositório packagecloud falhou. Tentando instalação direta..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q rabbitmq-server
}

# Habilita painel de administração web (porta 15672)
rabbitmq-plugins enable rabbitmq_management 2>/dev/null || true

# Cria usuário admin para a RPA
rabbitmqctl add_user rpa_admin "RpaT0ki0@2026" 2>/dev/null || \
    rabbitmqctl change_password rpa_admin "RpaT0ki0@2026"
rabbitmqctl set_user_tags rpa_admin administrator
rabbitmqctl set_permissions -p / rpa_admin ".*" ".*" ".*"
rabbitmqctl delete_user guest 2>/dev/null || true

systemctl enable rabbitmq-server
systemctl start rabbitmq-server
sleep 3
systemctl is-active rabbitmq-server && ok "RabbitMQ rodando (painel: http://5.161.79.179:15672)" || wrn "RabbitMQ pode não ter iniciado — verifique manualmente"

# ─────────────────────────────────────────────────────────────────
# ETAPA 7: Swap de 2 GB (proteção contra OOM com 5 Chromes)
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[7/13] Configurando swap de 2 GB...${NC}"
if ! swapon --show | grep -q '/swapfile'; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    ok "Swap de 2 GB criado e ativado"
else
    ok "Swap já configurado"
fi

# Swappiness baixo: só usa swap em emergência
sysctl -w vm.swappiness=10 > /dev/null
echo 'vm.swappiness=10' >> /etc/sysctl.d/99-rpa.conf

# ─────────────────────────────────────────────────────────────────
# ETAPA 8: /dev/shm 512 MB (Chrome headless precisa de shm grande)
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[8/13] Configurando /dev/shm para Chrome headless...${NC}"
# Remonta /dev/shm com 512 MB (padrão é 64 MB em muitos VPS)
mount -o remount,size=512M /dev/shm 2>/dev/null || true

# Persiste via /etc/fstab
if ! grep -q 'tmpfs /dev/shm' /etc/fstab; then
    echo 'tmpfs /dev/shm tmpfs rw,nosuid,nodev,size=512M 0 0' >> /etc/fstab
fi
ok "/dev/shm configurado: $(df -h /dev/shm | tail -1 | awk '{print $2}')"

# ─────────────────────────────────────────────────────────────────
# ETAPA 9: Python venv + dependências
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[9/13] Configurando ambiente Python...${NC}"
mkdir -p "$APP_DIR"

# Copia arquivos do diretório atual se necessário
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$SCRIPT_DIR" != "$APP_DIR" ]; then
    inf "Copiando arquivos de $SCRIPT_DIR para $APP_DIR..."
    rsync -a --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
          --exclude='rpa_logs.db' "$SCRIPT_DIR/" "$APP_DIR/"
fi

python3.11 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip -q

# Garante que redis está no requirements.txt
grep -q '^redis' "$APP_DIR/requirements.txt" || echo 'redis>=5.0.0' >> "$APP_DIR/requirements.txt"

"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt" -q
ok "Dependências Python instaladas"
"$VENV/bin/python" --version

# ─────────────────────────────────────────────────────────────────
# ETAPA 10: Arquivo .env
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[10/13] Criando .env otimizado para este servidor...${NC}"
ENV_FILE="$APP_DIR/.env"

# Só cria se não existir (preserva configurações manuais)
if [ ! -f "$ENV_FILE" ]; then
cat > "$ENV_FILE" << 'ENVEOF'
# ─── Credenciais do Portal ────────────────────────────────────────
USE_STATIC_CREDENTIALS=true

# ─── Pool de Drivers ─────────────────────────────────────────────
# 5 Chromes × ~350 MB = ~1.75 GB | Servidor tem 8 GB
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
# Descomente e defina para habilitar autenticação:
# API_KEY=troque_por_chave_segura

# ─── RabbitMQ (pronto para uso futuro) ───────────────────────────
RABBITMQ_URL=amqp://rpa_admin:RpaT0ki0@2026@127.0.0.1:5672/
ENVEOF
    ok ".env criado"
else
    # Atualiza apenas POOL_SIZE se arquivo já existir
    sed -i 's/^POOL_SIZE=.*/POOL_SIZE=5/' "$ENV_FILE"
    sed -i 's/^MAX_RETRIES=.*/MAX_RETRIES=3/' "$ENV_FILE"
    ok ".env já existia — POOL_SIZE e MAX_RETRIES atualizados"
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

# Logs
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

# Limites de recursos (5 Chromes × ~350 MB + API ~200 MB = ~2 GB)
MemoryMax=5G
MemoryHigh=4G
TasksMax=512

# Timeout generoso para startup (3 Chromes inicializando em paralelo)
TimeoutStartSec=180
TimeoutStopSec=60
KillMode=mixed
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
ok "Serviço systemd configurado e habilitado"

# ─────────────────────────────────────────────────────────────────
# ETAPA 12: Logrotate para journald + backup SQLite
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[12/13] Configurando logrotate e backup SQLite...${NC}"

# Limita tamanho do journal a 200 MB
mkdir -p /etc/systemd/journald.conf.d/
cat > /etc/systemd/journald.conf.d/rpa.conf << 'JEOF'
[Journal]
SystemMaxUse=200M
SystemKeepFree=500M
MaxFileSec=7day
JEOF
systemctl restart systemd-journald 2>/dev/null || true

# Backup diário do SQLite (mantém 7 dias)
cat > /etc/cron.daily/rpa-sqlite-backup << CRONEOF
#!/bin/bash
DB="${APP_DIR}/rpa_logs.db"
BAK="${APP_DIR}/backups"
mkdir -p "\$BAK"
[ -f "\$DB" ] && sqlite3 "\$DB" ".backup \$BAK/rpa_logs_\$(date +%Y%m%d).db"
# Remove backups com mais de 7 dias
find "\$BAK" -name "rpa_logs_*.db" -mtime +7 -delete
CRONEOF
chmod +x /etc/cron.daily/rpa-sqlite-backup
mkdir -p "$APP_DIR/backups"
ok "Logrotate e backup SQLite configurados"

# ─────────────────────────────────────────────────────────────────
# ETAPA 13: Testes finais
# ─────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}[13/13] Executando testes finais...${NC}"

# Teste ChromeDriver
inf "Testando ChromeDriver headless..."
"$VENV/bin/python" - << 'PYEOF'
import subprocess, shutil, sys
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
    print(f"  ChromeDriver OK — título: {d.title}")
    d.quit()
except Exception as e:
    print(f"  ERRO ChromeDriver: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
ok "ChromeDriver testado com sucesso"

# Teste Redis
inf "Testando Redis..."
redis-cli set test_key "ok" EX 5 > /dev/null
VAL=$(redis-cli get test_key)
[ "$VAL" = "ok" ] && ok "Redis leitura/escrita OK" || err "Redis falhou no teste de leitura"
redis-cli del test_key > /dev/null

# Inicia o serviço
inf "Iniciando ${SERVICE_NAME}..."
systemctl start "$SERVICE_NAME"
sleep 12  # pool de 5 drivers leva ~60s para inicializar (paralelo ~20s)

inf "Verificando status..."
if systemctl is-active "$SERVICE_NAME" > /dev/null 2>&1; then
    ok "Serviço ativo!"
    sleep 5
    HEALTH=$(curl -sf http://localhost:8000/health 2>/dev/null || echo '{}')
    echo "  Health: $HEALTH"
else
    wrn "Serviço ainda inicializando (pool de Chromes demora ~20s). Verifique:"
    wrn "  journalctl -u ${SERVICE_NAME} -f"
fi

# ─────────────────────────────────────────────────────────────────
# Resumo final
# ─────────────────────────────────────────────────────────────────
CHROME_V=$(google-chrome --version 2>/dev/null || echo "—")
CHROMEDRV_V=$(chromedriver --version 2>/dev/null || echo "—")
REDIS_V=$(redis-server --version | awk '{print $3}' | tr -d 'v')
RABBIT_V=$(rabbitmqctl version 2>/dev/null || echo "—")

echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  Instalação concluída!${NC}"
echo -e "${GREEN}============================================================${NC}"
echo ""
echo -e "  ${CYAN}Versões instaladas:${NC}"
echo    "    $CHROME_V"
echo    "    $CHROMEDRV_V"
echo    "    Redis $REDIS_V"
echo    "    RabbitMQ $RABBIT_V"
echo ""
echo -e "  ${CYAN}Configuração do servidor:${NC}"
echo    "    Pool de Drivers : 5 instâncias Chrome paralelas"
echo    "    Cache Redis     : 512 MB, TTL 1h, LRU"
echo    "    Swap            : 2 GB (proteção OOM)"
echo    "    /dev/shm        : 512 MB (Chrome headless)"
echo    "    Memória limit   : 5 GB (systemd)"
echo ""
echo -e "  ${CYAN}Endpoints disponíveis:${NC}"
echo    "    http://5.161.79.179:8000/          ← Dashboard"
echo    "    http://5.161.79.179:8000/health    ← Status"
echo    "    http://5.161.79.179:8000/placa/ABC1234"
echo    "    http://5.161.79.179:15672/         ← RabbitMQ (rpa_admin / RpaT0ki0@2026)"
echo ""
echo -e "  ${CYAN}Comandos úteis:${NC}"
echo    "    journalctl -u rpa-tokio -f         ← Logs em tempo real"
echo    "    systemctl status rpa-tokio          ← Status do serviço"
echo    "    redis-cli monitor                   ← Monitor do cache"
echo    "    sqlite3 /root/rpa-tokio/rpa_logs.db 'SELECT * FROM queries ORDER BY id DESC LIMIT 10;'"
echo ""
echo -e "  ${YELLOW}⚠  Ajuste API_KEY em ${APP_DIR}/.env e reinicie:${NC}"
echo    "    systemctl restart rpa-tokio"
echo ""
