"""
Autenticação e proteção anti-brute-force para o painel RPA Tokio Marine.

Mecanismos:
  - Sessões HTTP-only cookie (token hex 64 chars, TTL 8 h com slide)
  - Brute-force: bloqueio de IP após 5 falhas em 15 min → lock por 30 min
  - Credenciais via variáveis de ambiente (DASHBOARD_USER / DASHBOARD_PASS)
    com fallback para sysadmin / F1g2d6@3426
"""

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict
from typing import Dict, List

# ---------------------------------------------------------------------------
# Credenciais (env vars ou hardcoded como fallback seguro)
# ---------------------------------------------------------------------------
_USER: str = os.getenv("DASHBOARD_USER", "sysadmin")
_PASS: str = os.getenv("DASHBOARD_PASS", "F1g2d6@3426")

# Usamos o hash SHA-256 da senha para comparação constante no tempo
_PASS_HASH: str = hashlib.sha256(_PASS.encode()).hexdigest()


def _hash(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()


def check_credentials(username: str, password: str) -> bool:
    """Compara usuário e senha de forma segura (constant-time)."""
    user_ok = hmac.compare_digest(username.encode(), _USER.encode())
    pass_ok = hmac.compare_digest(_hash(password), _PASS_HASH)
    return user_ok and pass_ok


# ---------------------------------------------------------------------------
# Sessões
# ---------------------------------------------------------------------------
SESSION_TTL = 8 * 3600          # 8 horas
COOKIE_NAME = "rpa_session"

_sessions: Dict[str, float] = {}  # token → expiry unix timestamp


def create_session() -> str:
    """Gera um token e registra a sessão."""
    token = secrets.token_hex(32)   # 64 chars hex
    _sessions[token] = time.time() + SESSION_TTL
    _cleanup_sessions()
    return token


def validate_session(token: str) -> bool:
    """Retorna True se o token é válido e ainda não expirou (com slide)."""
    if not token:
        return False
    exp = _sessions.get(token)
    if not exp:
        return False
    if time.time() > exp:
        _sessions.pop(token, None)
        return False
    # slide expiry a cada request
    _sessions[token] = time.time() + SESSION_TTL
    return True


def destroy_session(token: str) -> None:
    _sessions.pop(token, None)


def _cleanup_sessions() -> None:
    now = time.time()
    expired = [t for t, exp in _sessions.items() if exp < now]
    for t in expired:
        del _sessions[t]


# ---------------------------------------------------------------------------
# Brute-force protection
# ---------------------------------------------------------------------------
LOCK_MAX_ATTEMPTS = 5      # tentativas permitidas
LOCK_WINDOW_S     = 900    # janela deslizante: 15 min
LOCK_DURATION_S   = 1800   # duração do bloqueio: 30 min

_ip_attempts: Dict[str, List[float]] = defaultdict(list)  # ip → [timestamps]
_ip_locked: Dict[str, float] = {}                         # ip → unlock_ts


def is_locked(ip: str) -> bool:
    """True se o IP está atualmente bloqueado."""
    unlock = _ip_locked.get(ip)
    if not unlock:
        return False
    if time.time() < unlock:
        return True
    # desbloqueado
    del _ip_locked[ip]
    return False


def remaining_lock(ip: str) -> int:
    """Segundos restantes de bloqueio (0 se não bloqueado)."""
    unlock = _ip_locked.get(ip, 0)
    secs = int(unlock - time.time())
    return max(0, secs)


def record_failure(ip: str) -> bool:
    """
    Registra tentativa falha. Retorna True se o IP acabou de ser bloqueado.
    """
    now = time.time()
    # descarta tentativas fora da janela
    _ip_attempts[ip] = [t for t in _ip_attempts[ip] if now - t < LOCK_WINDOW_S]
    _ip_attempts[ip].append(now)
    if len(_ip_attempts[ip]) >= LOCK_MAX_ATTEMPTS:
        _ip_locked[ip] = now + LOCK_DURATION_S
        _ip_attempts[ip] = []
        return True
    return False


def record_success(ip: str) -> None:
    """Limpa o histórico de falhas após login bem-sucedido."""
    _ip_attempts.pop(ip, None)


def remaining_attempts(ip: str) -> int:
    """Quantas tentativas restam antes do bloqueio."""
    now = time.time()
    recent = [t for t in _ip_attempts.get(ip, []) if now - t < LOCK_WINDOW_S]
    return max(0, LOCK_MAX_ATTEMPTS - len(recent))
