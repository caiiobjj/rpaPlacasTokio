import os
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from dotenv import load_dotenv

# Carrega variáveis de ambiente se existirem
load_dotenv()

# Credenciais estáticas (conforme solicitado)
STATIC_USERNAME = "00670482188"
STATIC_PASSWORD = "Ref@2026"


def _truthy(val: str) -> bool:
    return str(val).lower() in ("1", "true", "yes", "y")


def use_static_credentials() -> bool:
    """Define se deve usar credenciais estáticas em vez de .env."""
    return _truthy(os.getenv("USE_STATIC_CREDENTIALS", "true"))


def get_credentials():
    """Retorna (username, password) a serem usados pelos scrapers."""
    if use_static_credentials():
        return STATIC_USERNAME, STATIC_PASSWORD
    username = os.getenv("USERNAME") or STATIC_USERNAME
    password = os.getenv("PASSWORD") or STATIC_PASSWORD
    return username, password


def get_urls():
    """Retorna (login_url, portal_url) com saneamento de HTTPS para o portal."""
    login_url = os.getenv(
        "LOGIN_URL",
        "https://ssoportais3.tokiomarine.com.br/openam/XUI/?realm=TOKIOLFR",
    )
    portal_url = os.getenv(
        "PORTAL_URL",
        "https://portalparceiros.tokiomarine.com.br/",
    )
    if portal_url.startswith("http://"):
        portal_url = "https://" + portal_url.split("://", 1)[1]
    return login_url, portal_url


def nova_cotacao_url() -> str:
    """URL completa para a rota de Nova Cotação."""
    _, portal_url = get_urls()
    base = portal_url.rstrip('/')
    return base + '/group/portal-corretor#/nova-cotacao'


def login_url_with_goto(target: str) -> str:
    """Constrói LOGIN_URL com o parâmetro goto apontando para `target` (https)."""
    login_url, _ = get_urls()
    parsed = urlparse(login_url.strip('"'))
    qs = parse_qs(parsed.query)
    qs['goto'] = [target]
    query = urlencode(qs, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, parsed.fragment))


# ---------------------------------------------------------------------------
# Timeouts centralizados (configuráveis via .env)
# ---------------------------------------------------------------------------
TIMEOUT_DRIVER  = int(os.getenv("TIMEOUT_DRIVER", "60"))   # espera geral do WebDriverWait
TIMEOUT_MODAL   = int(os.getenv("TIMEOUT_MODAL",  "20"))   # espera do modal de veículos
TIMEOUT_IFRAME  = int(os.getenv("TIMEOUT_IFRAME", "30"))   # espera do iframe CotadorAutoService
TIMEOUT_PAGE    = int(os.getenv("TIMEOUT_PAGE",   "15"))   # espera page load / navegação
MAX_RETRIES     = int(os.getenv("MAX_RETRIES",    "2"))    # tentativas antes de destruir sessão


def get_headless() -> bool:
    """Retorna configuração de headless (padrão true)."""
    return _truthy(os.getenv("HEADLESS", "true"))
