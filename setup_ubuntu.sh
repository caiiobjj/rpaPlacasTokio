#!/bin/bash
# =============================================================================
# setup_ubuntu.sh — Instalação do RPA TokioMarine no Ubuntu Server
# Testado em Ubuntu 22.04 LTS / 24.04 LTS
# Execute como root ou com sudo: sudo bash setup_ubuntu.sh
# =============================================================================
set -e

APP_DIR="/opt/rpa-tokio"
APP_USER="rpa"
PYTHON_MIN="3.11"

echo "=========================================="
echo " RPA TokioMarine — Setup Ubuntu Server"
echo "=========================================="

# 1. Atualizar sistema
echo "[1/7] Atualizando pacotes..."
apt-get update -q && apt-get upgrade -y -q

# 2. Instalar dependências do sistema
echo "[2/7] Instalando dependências do sistema..."
apt-get install -y -q \
    python3 python3-pip python3-venv \
    curl wget unzip gnupg ca-certificates \
    fonts-liberation libappindicator3-1 libasound2 \
    libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
    libgdk-pixbuf2.0-0 libnspr4 libnss3 \
    libx11-xcb1 libxcomposite1 libxcursor1 libxdamage1 \
    libxfixes3 libxi6 libxrandr2 libxss1 libxtst6 \
    xdg-utils

# 3. Instalar Google Chrome
echo "[3/7] Instalando Google Chrome..."
if ! command -v google-chrome &> /dev/null; then
    wget -q -O /tmp/chrome.deb \
        "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
    apt-get install -y -q /tmp/chrome.deb
    rm /tmp/chrome.deb
    echo "    Chrome instalado: $(google-chrome --version)"
else
    echo "    Chrome já instalado: $(google-chrome --version)"
fi

# 4. Criar usuário e diretório da aplicação
echo "[4/7] Configurando usuário e diretório..."
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --shell /bin/bash --create-home "$APP_USER"
    echo "    Usuário '$APP_USER' criado."
fi

mkdir -p "$APP_DIR"
cp -r . "$APP_DIR/"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

# 5. Criar virtual environment e instalar dependências Python
echo "[5/7] Instalando dependências Python..."
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip -q
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
echo "    Dependências instaladas."

# 6. Instalar serviço systemd
echo "[6/7] Configurando serviço systemd..."
cat > /etc/systemd/system/rpa-tokio.service << 'EOF'
[Unit]
Description=TokioMarine RPA API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=rpa
WorkingDirectory=/opt/rpa-tokio
EnvironmentFile=/opt/rpa-tokio/.env
ExecStart=/opt/rpa-tokio/.venv/bin/python api.py
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal
SyslogIdentifier=rpa-tokio

# Limites de recursos
TimeoutStopSec=30
KillMode=mixed

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable rpa-tokio
echo "    Serviço systemd configurado e habilitado."

# 7. Testar ChromeDriver
echo "[7/7] Testando ChromeDriver..."
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -c "
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
opts = webdriver.ChromeOptions()
opts.add_argument('--headless=new')
opts.add_argument('--no-sandbox')
opts.add_argument('--disable-dev-shm-usage')
opts.add_argument('--disable-setuid-sandbox')
d = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
d.get('https://www.google.com')
print('    ChromeDriver OK — título:', d.title)
d.quit()
"

echo ""
echo "=========================================="
echo " Instalação concluída!"
echo "=========================================="
echo ""
echo " Próximos passos:"
echo "   1. Edite /opt/rpa-tokio/.env e defina API_KEY"
echo "   2. Inicie o serviço:  sudo systemctl start rpa-tokio"
echo "   3. Veja os logs:      sudo journalctl -u rpa-tokio -f"
echo "   4. Teste:             curl http://localhost:8000/health"
echo ""
