"""
TokioMarine RPA – API de consulta de placas.
v3 – Pool de drivers paralelos + cache Redis.

Arquitetura:
  - DriverPool  : N instâncias Chrome rodando em paralelo (padrão: POOL_SIZE=3).
                  Cada requisição faz checkout de um driver livre, executa e
                  devolve. Se todos estiverem ocupados, a fila aguarda (timeout 5min).
  - Redis Cache : resultado de cada placa é armazenado por CACHE_TTL segundos.
                  Consultas repetidas retornam instantaneamente sem acionar o Selenium.
                  Cache é opcional — se Redis não estiver disponível a RPA roda sem ele.
  - Retry       : MAX_RETRIES tentativas por consulta.
                  Dados inválidos (null/0,00) recarregam o iframe sem destruir o driver.
                  Erros de sistema destroem o driver (pool recria automaticamente).

Endpoints:
  GET /placa/{placa}          → consulta síncrona (backward-compatible)
  GET /health                 → status do pool e do Redis
  GET /pool/status            → detalhes do pool
  GET /session/reset          → força recreação de todos os drivers
  GET /cache/clear/{placa}    → remove uma placa do cache
  GET /cache/clear            → limpa todo o cache
"""

import asyncio
import json
import logging
import os
import re as _re
import subprocess as _subprocess
import traceback
import uuid
import random as _random
import string as _string
import math as _math
import os as _os

try:
    import urllib3.exceptions as _urllib3_exc
except ImportError:
    _urllib3_exc = None

from typing import List, Optional
from urllib.parse import quote as _urlquote

import time as _time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("rpa_api")

from fastapi import FastAPI, HTTPException, Query, Header, BackgroundTasks, Request, Response
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

import auth as _auth
from tokio_automation import query_plate, PlacaNaoEncontradaError, DadosVaziosError
from driver_pool import DriverPool
from config import MAX_RETRIES, POOL_SIZE, REDIS_URL, CACHE_TTL, get_headless
import database

# ---------------------------------------------------------------------------
# API Key (opcional)
# ---------------------------------------------------------------------------
_API_KEY = os.getenv("API_KEY", "").strip()


def _check_api_key(x_api_key: Optional[str]) -> None:
    # Auth is fully handled by _AuthMiddleware (session cookie OR X-API-Key header).
    # This endpoint-level check is intentionally a no-op to avoid rejecting
    # cookie-authenticated browser sessions that don't send X-API-Key.
    pass


# ---------------------------------------------------------------------------
# Pool de drivers (inicializado no startup)
# ---------------------------------------------------------------------------
_pool: Optional[DriverPool] = None


# ---------------------------------------------------------------------------
# Cache Redis (opcional)
# ---------------------------------------------------------------------------
_redis = None


def _init_redis() -> None:
    global _redis
    try:
        import redis as _redis_lib
        client = _redis_lib.from_url(REDIS_URL, socket_connect_timeout=3, decode_responses=True)
        client.ping()
        _redis = client
        logger.info(f"[cache] Redis conectado: {REDIS_URL}")
    except Exception as e:
        _redis = None
        logger.warning(f"[cache] Redis indisponível ({e}) — cache desabilitado, RPA funciona normalmente.")


def _cache_get(placa: str) -> Optional[dict]:
    if _redis is None:
        return None
    try:
        raw = _redis.get(f"placa:{placa.upper()}")
        if raw:
            logger.info(f"[cache] HIT para placa '{placa}'")
            return json.loads(raw)
    except Exception as e:
        logger.warning(f"[cache] Erro ao ler cache: {e}")
    return None


def _cache_set(placa: str, dados: dict) -> None:
    if _redis is None:
        return
    try:
        _redis.setex(f"placa:{placa.upper()}", CACHE_TTL, json.dumps(dados))
        logger.info(f"[cache] Armazenado '{placa}' por {CACHE_TTL}s.")
    except Exception as e:
        logger.warning(f"[cache] Erro ao gravar cache: {e}")


def _cache_delete(placa: str) -> bool:
    if _redis is None:
        return False
    try:
        deleted = _redis.delete(f"placa:{placa.upper()}")
        return deleted > 0
    except Exception:
        return False


def _cache_flush() -> int:
    if _redis is None:
        return 0
    try:
        keys = _redis.keys("placa:*")
        if keys:
            return _redis.delete(*keys)
        return 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Validação dos dados retornados pelo portal
# ---------------------------------------------------------------------------
_ZERO_VALUES = {"r$ 0,00", "0,00", "r$0,00", ""}
_CAMPOS_OBRIGATORIOS = ("chassi", "veiculo", "valor_base_do_veiculo", "codigo_fipe")


def _validate_dados(dados: dict) -> list:
    invalidos = []
    for campo in _CAMPOS_OBRIGATORIOS:
        v = (dados.get(campo) or "").strip()
        if not v or v.lower() in _ZERO_VALUES:
            invalidos.append(campo)
    # Todos os campos vazios = veículo não cadastrado, não adianta retentar
    if len(invalidos) == len(_CAMPOS_OBRIGATORIOS):
        raise DadosVaziosError(
            f"Veículo não encontrado no sistema Tokio Marine "
            f"(todos os campos vieram vazios: {', '.join(invalidos)})"
        )
    return invalidos


# ---------------------------------------------------------------------------
# App FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(title="TokioMarine RPA API", version="3.0.0")
import os as _os
if _os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Auth middleware — protege todas as rotas exceto /login e /logout
# ---------------------------------------------------------------------------
_OPEN_PATHS = {"/login", "/logout"}

# Paths que NÃO devem gerar access_log (assets, health checks de alta frequência)
_NO_LOG_PATHS = {"/", "/health", "/pool/status", "/api/stats",
                 "/api/queries", "/api/errors", "/batch/jobs",
                 "/api/access-logs"}
_PLACA_RE = _re.compile(r'^/placa/([A-Za-z0-9]+)')


class _AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        import time as _t
        t0 = _t.time()
        response = await call_next(request)
        path = request.url.path
        if (not path.startswith("/static/")
                and path not in _NO_LOG_PATHS
                and path not in _OPEN_PATHS):
            dur = (_t.time() - t0) * 1000
            ip = (request.headers.get("x-forwarded-for") or
                  (request.client.host if request.client else "?"))
            ip = ip.split(",")[0].strip()
            m = _PLACA_RE.match(path)
            placa = m.group(1).upper() if m else None
            try:
                database.insert_access_log(
                    ip=ip,
                    method=request.method,
                    path=path,
                    status=response.status_code,
                    duration_ms=dur,
                    placa=placa,
                )
            except Exception:
                pass
        return response


class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # rotas abertas e assets estáticos são liberados
        if path in _OPEN_PATHS or path.startswith("/static/"):
            return await call_next(request)
        # Requisições com X-API-Key válida passam sem cookie (acesso programático / n8n)
        if _API_KEY:
            api_key_header = request.headers.get("x-api-key", "")
            if api_key_header == _API_KEY:
                return await call_next(request)
        token = request.cookies.get(_auth.COOKIE_NAME, "")
        if not _auth.validate_session(token):
            logger.info(f"[auth] Acesso negado em {path} — redirecionando para /login")
            return RedirectResponse(url="/login", status_code=302)
        return await call_next(request)


app.add_middleware(_AuthMiddleware)
app.add_middleware(_AccessLogMiddleware)


@app.on_event("startup")
async def startup():
    global _pool
    database.init_db()
    logger.info("[startup] Banco SQLite inicializado.")
    _init_redis()
    headless = get_headless()
    logger.info(f"[startup] Inicializando pool com {POOL_SIZE} drivers (headless={headless})...")
    _pool = DriverPool(size=POOL_SIZE, headless=headless)
    _pool.initialize()
    logger.info("[startup] Pool pronto. API disponível.")
    asyncio.create_task(_scheduled_job_watcher())


@app.on_event("shutdown")
def shutdown():
    if _pool:
        _pool.shutdown()


# ---------------------------------------------------------------------------
# Modelos Pydantic para batch
# ---------------------------------------------------------------------------

class BatchManualRequest(BaseModel):
    placas: List[str]
    nome: Optional[str] = "Lote Manual"


class BatchScheduleRequest(BaseModel):
    placas: List[str]
    scheduled_at: str          # ISO 8601 UTC, ex: "2026-03-05T02:00:00Z"
    nome: Optional[str] = "Lote Agendado"


class BatchDiscoverRequest(BaseModel):
    target_ok: int = 50                    # quantas placas válidas queremos encontrar (sem limite)
    formato: str = "both"                  # old | mercosul | both
    nome: Optional[str] = None


# ---------------------------------------------------------------------------
# Helper: gerador de placas aleatórias (espelho do frontend)
# ---------------------------------------------------------------------------
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DIGITS  = "0123456789"

def _gen_plates_random(qty: int, formato: str, exclude: set) -> list:
    """Gera placas aleatórias no formato solicitado, excluindo as já conhecidas."""
    def _old():
        return ("".join(_random.choices(_LETTERS, k=3))
                + "".join(_random.choices(_DIGITS, k=4)))

    def _mercosul():
        return ("".join(_random.choices(_LETTERS, k=3))
                + _random.choice(_DIGITS)
                + _random.choice(_LETTERS)
                + "".join(_random.choices(_DIGITS, k=2)))

    seen = set(exclude)
    plates: list = []
    max_attempts = qty * 50   # mais tentativas para lotes grandes
    attempts = 0
    while len(plates) < qty and attempts < max_attempts:
        attempts += 1
        if formato == "old":
            p = _old()
        elif formato == "mercosul":
            p = _mercosul()
        else:
            p = _old() if _random.random() < 0.5 else _mercosul()
        if p not in seen:
            seen.add(p)
            plates.append(p)
    return plates


def _is_chrome_crash(exc: Exception) -> bool:
    """Retorna True quando o erro indica que o Chrome foi morto (OOM Killer ou crash)."""
    msg = str(exc)
    if 'Connection refused' in msg or 'Max retries exceeded' in msg:
        return True
    if _urllib3_exc:
        cause = getattr(exc, '__cause__', None) or getattr(exc, '__context__', None)
        while cause:
            if isinstance(cause, (_urllib3_exc.MaxRetryError,
                                  _urllib3_exc.NewConnectionError,
                                  ConnectionRefusedError)):
                return True
            cause = getattr(cause, '__cause__', None) or getattr(cause, '__context__', None)
    return False


# ---------------------------------------------------------------------------
# Batch: worker assíncrono
# ---------------------------------------------------------------------------

_running_jobs: set = set()   # conjunto de job_ids em execução


async def _run_batch_job(job_id: str, placas: List[str]) -> None:
    """Processa um lote de placas usando o driver pool."""
    if job_id in _running_jobs:
        return
    _running_jobs.add(job_id)
    database.start_batch_job(job_id)
    logger.info(f"[batch:{job_id}] Iniciando lote com {len(placas)} placas")

    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(POOL_SIZE)   # não lançar mais tasks que drivers

    async def _process_one(placa: str) -> None:
        async with sem:
            t0 = _time.time()
            req_id = "b" + uuid.uuid4().hex[:7]
            database.insert_query(req_id, placa)
            max_attempts = 3
            for attempt_num in range(1, max_attempts + 1):
              try:
                def _sync_query():
                    with _pool.acquire(timeout=300) as driver:
                        dados = query_plate(driver, placa)
                        invalidos = _validate_dados(dados)
                        if invalidos:
                            raise DadosVaziosError(
                                f"Campos inválidos: {', '.join(invalidos)}"
                            )
                        return dados

                dados = await loop.run_in_executor(None, _sync_query)
                duration = round(_time.time() - t0, 2)
                _cache_set(placa, dados)
                _log(req_id, "INFO", f"Consulta OK em {duration}s — {dados.get('veiculo','')}")
                database.update_batch_result(job_id, placa, "ok", dados=dados, duration_s=duration)
                database.finish_query(req_id, status="ok", cached=False,
                                      attempts=attempt_num, duration_s=duration, dados=dados)
                return  # sucesso

              except (PlacaNaoEncontradaError, DadosVaziosError) as e:
                duration = round(_time.time() - t0, 2)
                _log(req_id, "WARNING", f"Placa não encontrada no portal: {e}")
                database.update_batch_result(job_id, placa, "not_found", error_msg=str(e), duration_s=duration)
                database.finish_query(req_id, status="not_found", attempts=attempt_num,
                                      duration_s=duration, error_msg=str(e)[:200])
                return  # resposta definitiva, não retentar

              except Exception as e:
                if _is_chrome_crash(e) and attempt_num < max_attempts:
                    wait_s = 15 * attempt_num
                    logger.warning(f"[batch:{job_id}] Chrome morto para {placa} — aguardando {wait_s}s e retentando (tentativa {attempt_num}/{max_attempts})")
                    _log(req_id, "WARNING", f"Chrome foi encerrado (OOM?) — retry {attempt_num}/{max_attempts} em {wait_s}s")
                    await asyncio.sleep(wait_s)
                    continue  # próxima tentativa

                duration = round(_time.time() - t0, 2)
                tb = traceback.format_exc()
                logger.error(f"[batch:{job_id}] Erro em {placa}: {e}")
                _log(req_id, "ERROR", f"Falha na consulta: {e}")
                database.insert_log(req_id, "DEBUG", tb)
                database.update_batch_result(job_id, placa, "error", error_msg=str(e)[:200], duration_s=duration)
                database.finish_query(req_id, status="error", attempts=attempt_num,
                                      duration_s=duration, error_msg=str(e)[:200])
                return

    tasks = [asyncio.create_task(_process_one(p)) for p in placas]
    await asyncio.gather(*tasks, return_exceptions=True)

    database.finish_batch_job(job_id)
    _running_jobs.discard(job_id)
    logger.info(f"[batch:{job_id}] Lote concluído")


async def _scheduled_job_watcher() -> None:
    """Verifica a cada 20s se há lotes agendados prontos para executar."""
    while True:
        try:
            await asyncio.sleep(20)
            pending = database.get_pending_scheduled_jobs()
            for job_id in pending:
                if job_id not in _running_jobs:
                    job = database.get_batch_job(job_id)
                    if job:
                        placas = database.get_batch_placas(job_id)
                        if placas:
                            logger.info(f"[scheduler] Disparando lote agendado {job_id} ({len(placas)} placas)")
                            asyncio.create_task(_run_batch_job(job_id, placas))
        except Exception as e:
            logger.error(f"[scheduler] Erro: {e}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

def _login_html() -> str:
    path = _os.path.join("static", "login.html")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>Login page not found.</h1>"


@app.get("/login")
async def login_get():
    return HTMLResponse(_login_html())


@app.post("/login")
async def login_post(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    ip = request.client.host if request.client else "unknown"

    # IP bloqueado?
    if _auth.is_locked(ip):
        mins = max(1, _auth.remaining_lock(ip) // 60)
        err = _urlquote(f"IP bloqueado por excesso de tentativas. Aguarde {mins} min.")
        logger.warning(f"[auth] Login bloqueado para IP {ip}")
        return RedirectResponse(url=f"/login?err={err}", status_code=302)

    if _auth.check_credentials(username, password):
        _auth.record_success(ip)
        token = _auth.create_session()
        logger.info(f"[auth] Login bem-sucedido: '{username}' de {ip}")
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie(
            _auth.COOKIE_NAME, token,
            httponly=True, samesite="strict", max_age=_auth.SESSION_TTL
        )
        return resp
    else:
        locked = _auth.record_failure(ip)
        logger.warning(f"[auth] Falha de login: '{username}' de {ip}")
        if locked:
            err = _urlquote("IP bloqueado por 30 min após múltiplas tentativas inválidas.")
            return RedirectResponse(url=f"/login?err={err}", status_code=302)
        att = _auth.remaining_attempts(ip)
        err = _urlquote("Usuário ou senha incorretos.")
        return RedirectResponse(url=f"/login?err={err}&att={att}", status_code=302)


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(_auth.COOKIE_NAME, "")
    _auth.destroy_session(token)
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(_auth.COOKIE_NAME)
    logger.info(f"[auth] Logout de {request.client.host if request.client else 'unknown'}")
    return resp


@app.get("/health")
def health(x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    redis_ok = False
    if _redis is not None:
        try:
            _redis.ping()
            redis_ok = True
        except Exception:
            pass
    return {
        "status": "ok",
        "pool_size": _pool.size if _pool else 0,
        "pool_available": _pool.available() if _pool else 0,
        "redis": "connected" if redis_ok else ("disabled" if _redis is None else "error"),
        "cache_ttl_s": CACHE_TTL,
    }


@app.get("/pool/status")
def pool_status(x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    if _pool is None:
        raise HTTPException(status_code=503, detail="Pool não inicializado.")
    return {
        "pool_size": _pool.size,
        "drivers_available": _pool.available(),
        "drivers_busy": _pool.size - _pool.available(),
    }


@app.get("/session/reset")
def reset_session(x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    if _pool is None:
        raise HTTPException(status_code=503, detail="Pool não inicializado.")
    _pool.shutdown()
    _pool._shutdown = False
    _pool.initialize()
    return {"ok": True, "mensagem": f"{_pool.size} drivers recriados."}


@app.get("/cache/clear/{placa}")
def cache_clear_placa(placa: str, x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    deleted = _cache_delete(placa)
    return {"ok": True, "deleted": deleted, "placa": placa.upper()}


@app.get("/cache/clear")
def cache_clear_all(x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    count = _cache_flush()
    return {"ok": True, "deleted_keys": count}


@app.get("/")
def dashboard():
    """Serve o painel de monitoramento."""
    path = _os.path.join("static", "dashboard.html")
    if not _os.path.exists(path):
        return HTMLResponse("<h1>Dashboard não encontrado.</h1><p>Coloque static/dashboard.html no servidor.</p>")
    with open(path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ---------------------------------------------------------------------------
# Endpoints do Dashboard (dados para o frontend)
# ---------------------------------------------------------------------------

@app.get("/api/stats")
def api_stats(x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    return database.get_stats()


@app.get("/api/queries")
def api_queries(
    limit: int = Query(100, ge=1, le=500),
    x_api_key: Optional[str] = Header(default=None),
):
    _check_api_key(x_api_key)
    return database.get_recent_queries(limit=limit)


@app.get("/api/queries/{req_id}/logs")
def api_query_logs(req_id: str, x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    return database.get_query_logs(req_id)


@app.get("/api/access-logs")
def api_access_logs(
    limit: int = Query(200, ge=1, le=1000),
    x_api_key: Optional[str] = Header(default=None),
):
    _check_api_key(x_api_key)
    return database.get_access_logs(limit=limit)


@app.post("/admin/restart")
async def admin_restart(x_api_key: Optional[str] = Header(default=None)):
    """Reinicia o serviço rpa-tokio via systemctl (resposta enviada antes do kill)."""
    _check_api_key(x_api_key)
    logger.warning("[admin] Reinício manual solicitado via painel.")
    async def _delayed_restart():
        await asyncio.sleep(1.5)
        try:
            _subprocess.Popen(
                ["systemctl", "restart", "rpa-tokio"],
                stdout=_subprocess.DEVNULL,
                stderr=_subprocess.DEVNULL,
            )
        except Exception as e:
            logger.error(f"[admin] Falha ao reiniciar: {e}")
    asyncio.create_task(_delayed_restart())
    return {"ok": True, "message": "Reiniciando em 1.5s... aguarde ~20s para o pool recarregar."}


@app.get("/api/errors")
def api_errors(x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    return database.get_error_summary()


# ---------------------------------------------------------------------------
# Helper: log para DB e logger simultaneamente
# ---------------------------------------------------------------------------

def _log(req_id: str, level: str, msg: str) -> None:
    getattr(logger, level.lower(), logger.info)(f"[{req_id}] {msg}")
    try:
        database.insert_log(req_id, level, msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Endpoints: Batch (criação de banco de dados)
# ---------------------------------------------------------------------------

@app.post("/batch/manual")
async def batch_manual(body: BatchManualRequest, x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    if _pool is None:
        raise HTTPException(status_code=503, detail="Pool não inicializado.")
    placas = list({p.strip().upper() for p in body.placas if p.strip()})
    if not placas:
        raise HTTPException(status_code=400, detail="Nenhuma placa fornecida.")
    job_id = uuid.uuid4().hex[:8]
    database.insert_batch_job(job_id, body.nome or "Lote Manual", placas, scheduled_at=None)
    asyncio.create_task(_run_batch_job(job_id, placas))
    return {"ok": True, "job_id": job_id, "total": len(placas)}


@app.post("/batch/schedule")
async def batch_schedule(body: BatchScheduleRequest, x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    placas = list({p.strip().upper() for p in body.placas if p.strip()})
    if not placas:
        raise HTTPException(status_code=400, detail="Nenhuma placa fornecida.")
    if not body.scheduled_at:
        raise HTTPException(status_code=400, detail="scheduled_at é obrigatório.")
    job_id = uuid.uuid4().hex[:8]
    database.insert_batch_job(job_id, body.nome or "Lote Agendado", placas, scheduled_at=body.scheduled_at)
    return {"ok": True, "job_id": job_id, "total": len(placas), "scheduled_at": body.scheduled_at}


@app.post("/batch/discover")
async def batch_discover(body: BatchDiscoverRequest, x_api_key: Optional[str] = Header(default=None)):
    """
    Gera placas aleatórias automaticamente e submete um lote com quantidade
    suficiente para atingir a meta de placas válidas (target_ok).
    Calcula a quantidade com base na taxa histórica de sucesso do banco.
    """
    _check_api_key(x_api_key)
    if _pool is None:
        raise HTTPException(status_code=503, detail="Pool não inicializado.")
    if body.target_ok < 1:
        raise HTTPException(status_code=400, detail="target_ok deve ser ao menos 1.")

    # Exclui placas que já temos com sucesso no banco
    existing_ok = {r["placa"] for r in database.get_database_placas(limit=100_000)}

    # Estima taxa de acerto histórica (ok / total_finalizados)
    stats = database.get_stats()
    total_done = (stats.get("ok") or 0) + (stats.get("errors") or 0) + (stats.get("invalid") or 0)
    ok_count   = stats.get("ok") or 0
    hit_rate   = (ok_count / total_done) if total_done >= 20 else 0.40   # default 40%
    hit_rate   = max(0.10, min(0.95, hit_rate))                          # clamp 10–95%

    # Gera placas com buffer de 1.8× para compensar erros e não-encontradas (sem limite)
    needed = _math.ceil(body.target_ok / hit_rate * 1.8)
    placas = _gen_plates_random(needed, body.formato, existing_ok)

    if not placas:
        raise HTTPException(
            status_code=400,
            detail="Não foi possível gerar placas novas (banco já inclui todos os resultados conhecidos)."
        )

    job_id = uuid.uuid4().hex[:8]
    nome = body.nome or f"Descoberta — meta {body.target_ok} OK"
    database.insert_batch_job(job_id, nome, placas, scheduled_at=None)
    asyncio.create_task(_run_batch_job(job_id, placas))
    logger.info(
        f"[discover:{job_id}] Meta={body.target_ok} OK | "
        f"hit_rate_est={hit_rate:.0%} | gerando {len(placas)} placas"
    )
    return {
        "ok": True,
        "job_id": job_id,
        "total": len(placas),
        "target_ok": body.target_ok,
        "hit_rate_est": round(hit_rate, 2),
        "existing_excluded": len(existing_ok),
    }


@app.get("/batch/jobs")
def batch_jobs(limit: int = Query(50), x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    return database.get_batch_jobs(limit=limit)


@app.get("/batch/jobs/{job_id}")
def batch_job_detail(job_id: str, x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    job = database.get_batch_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Lote não encontrado.")
    return job


@app.get("/batch/jobs/{job_id}/results")
def batch_job_results(job_id: str, limit: int = Query(500), x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    return database.get_batch_results(job_id, limit=limit)


@app.delete("/batch/jobs/{job_id}")
def batch_cancel(job_id: str, x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    ok = database.cancel_batch_job(job_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Lote não pode ser cancelado (já concluído ou não encontrado).")
    _running_jobs.discard(job_id)
    return {"ok": True, "job_id": job_id}


@app.get("/batch/database")
def batch_database(limit: int = Query(5000), x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    return database.get_database_placas(limit=limit)


@app.get("/placa/{placa}")
def get_por_placa(
    placa: str,
    headless: Optional[bool] = Query(True),
    no_cache: Optional[bool] = Query(False),
    x_api_key: Optional[str] = Header(default=None),
):
    _check_api_key(x_api_key)
    if not placa or len(placa) < 5:
        raise HTTPException(status_code=400, detail="Placa inválida.")
    if _pool is None:
        raise HTTPException(status_code=503, detail="Pool não inicializado.")

    req_id = uuid.uuid4().hex[:8]
    placa = placa.upper().strip()

    # 1. Verifica cache antes de ocupar um driver
    if not no_cache:
        cached = _cache_get(placa)
        if cached:
            database.insert_query(req_id, placa)
            database.finish_query(req_id, status="ok", cached=True, attempts=0, duration_s=0.0, dados=cached)
            return JSONResponse(content={"ok": True, "dados": cached, "cache": True})

    database.insert_query(req_id, placa)
    _log(req_id, "INFO", f"Iniciando consulta (pool disponível: {_pool.available()}/{_pool.size})")

    t_total = _time.time()
    last_exc = None
    attempt = 0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _log(req_id, "INFO", f"Tentativa {attempt}/{MAX_RETRIES} — aguardando driver livre...")
            with _pool.acquire(timeout=300) as driver:
                _log(req_id, "INFO", f"Driver obtido. Executando query_plate...")
                t0 = _time.time()
                dados = query_plate(driver, placa)
                elapsed = _time.time() - t0

                invalidos = _validate_dados(dados)
                if invalidos:
                    raise ValueError(
                        f"Dados incompletos — campos inválidos: {', '.join(invalidos)}"
                    )

                total_elapsed = _time.time() - t_total
                _log(req_id, "INFO", f"Consulta OK em {elapsed:.1f}s RPA ({total_elapsed:.1f}s total).")
                _cache_set(placa, dados)
                database.finish_query(
                    req_id, status="ok", cached=False,
                    attempts=attempt, duration_s=round(total_elapsed, 2), dados=dados
                )
                return JSONResponse(content={"ok": True, "dados": dados, "cache": False})

        except (PlacaNaoEncontradaError, DadosVaziosError) as e:
            last_exc = e
            _log(req_id, "WARNING", f"Placa não encontrada no portal: {e}")
            break  # resposta definitiva do portal — não retentar

        except ValueError as e:
            last_exc = e
            _log(req_id, "WARNING", f"Dados inválidos: {e}")
            if attempt < MAX_RETRIES:
                _log(req_id, "INFO", "Retentando com outro driver...")

        except TimeoutError as e:
            last_exc = e
            _log(req_id, "ERROR", f"Pool esgotado após 300s: {e}")
            database.finish_query(req_id, status="error", attempts=attempt,
                                  duration_s=round(_time.time()-t_total, 2), error_msg=str(e))
            raise HTTPException(status_code=503, detail=str(e))

        except Exception as e:
            last_exc = e
            tb = traceback.format_exc()
            if _is_chrome_crash(e):
                wait_s = 15 * attempt
                _log(req_id, "WARNING", f"Chrome foi encerrado (OOM?) na tentativa {attempt} — aguardando {wait_s}s para pool recriar driver...")
                database.insert_log(req_id, "DEBUG", tb)
                if attempt < MAX_RETRIES:
                    _time.sleep(wait_s)
                    _log(req_id, "INFO", f"Retentando ({attempt + 1}/{MAX_RETRIES}) após crash do Chrome...")
            else:
                _log(req_id, "ERROR", f"Falha na tentativa {attempt}: {e}")
                database.insert_log(req_id, "DEBUG", tb)
                if attempt < MAX_RETRIES:
                    _log(req_id, "INFO", f"Retentando ({attempt + 1}/{MAX_RETRIES})...")

    if isinstance(last_exc, (PlacaNaoEncontradaError, DadosVaziosError)):
        database.finish_query(
            req_id, status="not_found", attempts=attempt,
            duration_s=round(_time.time()-t_total, 2), error_msg=str(last_exc)
        )
        raise HTTPException(status_code=404, detail=str(last_exc))

    final_status = "invalid_data" if isinstance(last_exc, ValueError) else "error"
    database.finish_query(
        req_id, status=final_status, attempts=attempt,
        duration_s=round(_time.time()-t_total, 2), error_msg=str(last_exc)
    )
    http_status = 422 if isinstance(last_exc, ValueError) else 500
    raise HTTPException(status_code=http_status, detail=str(last_exc))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=False)
