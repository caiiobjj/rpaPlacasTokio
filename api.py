"""
TokioMarine RPA – API de consulta de placas.

Otimização de sessão:
  - Um único driver Chrome é mantido vivo entre requisições (singleton).
  - O login e o passo portal-corretor são feitos apenas na primeira chamada
    ou quando a sessão expira.
  - Requisições subsequentes reutilizam a sessão via reload do iframe (~1.3 s),
    pulando o login (~5-10 s) e o reload Angular (~5 s).
  - Um threading.Lock() serializa as requisições (Selenium é single-thread).

Segurança:
  - Se API_KEY estiver definida no .env, todos os endpoints requerem o header
    X-API-Key com o valor correto.
"""

import os
import subprocess
import threading
import logging
import traceback
import time as _time
import uuid
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

from tokio_automation import (
    build_driver,
    login,
    go_to_nova_cotacao,
    is_session_alive,
    try_reuse_form,
    reload_cotador_iframe,
    query_plate,
)
from config import MAX_RETRIES

logger = logging.getLogger("rpa_api")

# ---------------------------------------------------------------------------
# API Key (opcional)
# ---------------------------------------------------------------------------
_API_KEY = os.getenv("API_KEY", "").strip()


def _check_api_key(x_api_key: Optional[str]) -> None:
    """Valida X-API-Key se API_KEY estiver configurada."""
    if not _API_KEY:
        return  # autenticação desabilitada
    if x_api_key != _API_KEY:
        raise HTTPException(status_code=401, detail="X-API-Key inválida ou ausente.")


app = FastAPI(title="TokioMarine RPA API", version="1.0.0")

# ---------------------------------------------------------------------------
# Singleton de sessão
# ---------------------------------------------------------------------------
_driver = None
_driver_ready = False   # True quando já passou pelo go_to_nova_cotacao completo
_driver_headless = True
_session_lock = threading.Lock()


def _destroy_driver() -> None:
    """Fecha o driver atual e mata processos Chrome orphãos."""
    global _driver, _driver_ready
    try:
        if _driver is not None:
            _driver.quit()
    except Exception:
        pass
    # Mata processos Chrome que ficaram órfãos após falha/crash
    try:
        subprocess.run(['pkill', '-f', 'google-chrome'], capture_output=True, timeout=5)
    except Exception:
        pass
    _driver = None
    _driver_ready = False


def _ensure_session(headless: bool = True) -> None:
    """
    Garante que _driver exista, esteja vivo e já tenha feito a navegação
    inicial (go_to_nova_cotacao). Caso a sessão tenha expirado, recria tudo.
    Deve ser chamado DENTRO do _session_lock.
    """
    global _driver, _driver_ready, _driver_headless

    # Se o modo headless mudou, destruir e recriar
    if _driver is not None and headless != _driver_headless:
        logger.info("Modo headless alterado — recriando driver.")
        _destroy_driver()

    # Verifica se o driver ainda responde
    if _driver is not None and not is_session_alive(_driver):
        logger.info("Sessão expirada ou driver morto — recriando.")
        _destroy_driver()

    if _driver is None:
        logger.info("Criando novo driver e fazendo login...")
        t0 = _time.time()
        _driver = build_driver(headless=headless)
        _driver_headless = headless
        logger.info(f"  driver criado em {_time.time()-t0:.1f}s")
        t0 = _time.time()
        login(_driver)
        logger.info(f"  login em {_time.time()-t0:.1f}s")
        t0 = _time.time()
        go_to_nova_cotacao(_driver)
        logger.info(f"  go_to_nova_cotacao em {_time.time()-t0:.1f}s")
        _driver_ready = True
        logger.info("Driver pronto (login + navegação inicial concluídos).")


def _prepare_for_query(headless: bool = True) -> None:
    """
    Garante sessão ativa e navega para o formulário de nova cotação.
    - Cold start: _ensure_session já faz go_to_nova_cotacao — sem navegação extra.
    - Warm reuse: sessão já existe, basta navegar para nova-cotacao novamente.
    Deve ser chamado DENTRO do _session_lock.
    """
    global _driver_ready

    session_was_alive = _driver is not None and is_session_alive(_driver)

    _ensure_session(headless)

    if session_was_alive:
        # Sessão já existia — tenta recarregar apenas o iframe do CotadorAutoService
        # (~3-6 s vs ~10 s do go_to_nova_cotacao completo)
        if try_reuse_form(_driver):
            # Iframe acessível — recarrega para limpar estado do formulário
            try:
                t0 = _time.time()
                reload_cotador_iframe(_driver)
                logger.info(f"Iframe recarregado em {_time.time()-t0:.1f}s (formulário limpo).")
            except Exception as e_reload:
                # Reload falhou — faz navegação completa como fallback
                logger.warning(f"Reload do iframe falhou ({e_reload}), fazendo go_to_nova_cotacao...")
                go_to_nova_cotacao(_driver)
        else:
            # Iframe não disponível (Angular redirecionou) — faz navegação completa
            logger.info("Iframe não encontrado — fazendo go_to_nova_cotacao completo...")
            try:
                go_to_nova_cotacao(_driver)
            except Exception as e2:
                logger.warning(f"Navegação completa falhou ({e2}), recriando sessão...")
                _destroy_driver()
                _ensure_session(headless)
    else:
        logger.info("Cold start concluído — driver já está em nova-cotacao.")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health(x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    with _session_lock:
        alive = _driver is not None and is_session_alive(_driver)
    return {"status": "ok", "session_alive": alive}


@app.get("/session/reset")
def reset_session(x_api_key: Optional[str] = Header(default=None)):
    """Força destruição da sessão atual. Próxima requisição faz login do zero."""
    _check_api_key(x_api_key)
    with _session_lock:
        _destroy_driver()
    return {"ok": True, "mensagem": "Sessão destruída. Próxima consulta fará login novamente."}


@app.get("/placa/{placa}")
def get_por_placa(
    placa: str,
    headless: Optional[bool] = Query(True),
    x_api_key: Optional[str] = Header(default=None),
):
    _check_api_key(x_api_key)
    if not placa or len(placa) < 5:
        raise HTTPException(status_code=400, detail="Placa inválida")

    req_id = uuid.uuid4().hex[:8]
    logger.info(f"[{req_id}] Iniciando consulta para placa '{placa}'")

    with _session_lock:
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                _prepare_for_query(headless=headless)
                dados = query_plate(_driver, placa)
                logger.info(f"[{req_id}] Consulta concluída na tentativa {attempt}.")
                return JSONResponse(content={"ok": True, "dados": dados})
            except Exception as e:
                last_exc = e
                tb = traceback.format_exc()
                logger.error(f"[{req_id}] Tentativa {attempt}/{MAX_RETRIES} falhou:\n{tb}")
                _destroy_driver()
                if attempt < MAX_RETRIES:
                    logger.info(f"[{req_id}] Retentando ({attempt + 1}/{MAX_RETRIES})...")
        raise HTTPException(status_code=500, detail=str(last_exc))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=False)
