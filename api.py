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
import httpx
import ipaddress
import json
import logging
import os
import threading
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
from selenium.common.exceptions import TimeoutException as SeleniumTimeoutException

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
from config import (
    MAX_RETRIES,
    API_MAX_RETRIES,
    API_POOL_RESERVE,
    API_REQUEST_TIMEOUT_S,
    POOL_ACQUIRE_TIMEOUT_S,
    QUERY_PLATE_TIMEOUT_S,
    POOL_SIZE,
    REDIS_URL,
    CACHE_TTL,
    QUEUE_DISPATCH_PARALLELISM,
    QUEUE_POLL_INTERVAL_S,
    QUEUE_REDIS_KEY,
    QUEUE_RESULT_WEBHOOK_URL,
    QUEUE_WEBHOOK_TIMEOUT_S,
    SECURITY_EVENT_LIMIT,
    get_allowed_ip_seeds,
    get_headless,
)
import database

# ---------------------------------------------------------------------------
# API Key (opcional)
# ---------------------------------------------------------------------------
_API_KEY = os.getenv("API_KEY", "").strip()
_allowed_ip_rules: list[dict] = []
_allowed_ip_rules_lock = threading.Lock()


def _extract_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    candidate = forwarded.split(",")[0].strip() if forwarded else ""
    candidate = candidate or request.headers.get("x-real-ip", "") or (request.client.host if request.client else "")
    return (candidate or "unknown").strip()


def _normalize_ip_rule(rule: str) -> str:
    raw = (rule or "").strip()
    if not raw:
        raise ValueError("IP/CIDR vazio.")
    if "/" in raw:
        return str(ipaddress.ip_network(raw, strict=False))
    return str(ipaddress.ip_address(raw))


def _reload_allowed_ip_rules() -> None:
    global _allowed_ip_rules
    rules = database.get_allowed_ips(enabled_only=True)
    with _allowed_ip_rules_lock:
        _allowed_ip_rules = rules


def _match_allowed_ip(ip: str) -> Optional[str]:
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return None
    with _allowed_ip_rules_lock:
        rules = list(_allowed_ip_rules)
    for item in rules:
        rule = (item.get("ip") or "").strip()
        try:
            if "/" in rule:
                if ip_obj in ipaddress.ip_network(rule, strict=False):
                    return rule
            elif ip_obj == ipaddress.ip_address(rule):
                return rule
        except ValueError:
            continue
    return None


def _security_event(
    *,
    ip: str,
    method: str,
    path: str,
    action: str,
    reason: Optional[str] = None,
    request_mode: Optional[str] = None,
    user_agent: Optional[str] = None,
    status_code: Optional[int] = None,
    allowed_rule: Optional[str] = None,
) -> None:
    try:
        database.insert_security_event(
            ip=ip,
            method=method,
            path=path,
            action=action,
            reason=reason,
            request_mode=request_mode,
            user_agent=user_agent,
            status_code=status_code,
            allowed_rule=allowed_rule,
        )
    except Exception:
        pass


def _blocked_ip_response(ip: str) -> HTMLResponse:
    html = f"""
    <!DOCTYPE html>
    <html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Acesso negado</title>
    <style>
      body{{margin:0;background:#f5f7fb;color:#0f172a;font-family:Inter,Segoe UI,Arial,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center}}
      .card{{width:min(92vw,560px);background:#fff;border:1px solid #dbe5f0;border-radius:18px;box-shadow:0 18px 60px rgba(15,23,42,.14);overflow:hidden}}
      .top{{background:linear-gradient(90deg,#0554f2,#2fb7ff);padding:18px 24px;color:#fff;font-weight:700;letter-spacing:.08em;text-transform:uppercase;font-size:12px}}
      .body{{padding:28px 24px}}
      .badge{{display:inline-flex;padding:4px 10px;border-radius:999px;background:#eff6ff;color:#0554f2;border:1px solid #bfdbfe;font-size:12px;font-weight:700}}
      h1{{margin:12px 0 10px;font-size:28px}} p{{color:#475569;line-height:1.55}} code{{background:#eef2ff;padding:2px 6px;border-radius:6px}}
    </style></head>
    <body><div class="card"><div class="top">UniFi Shield • Prisma RPA</div><div class="body">
    <span class="badge">Firewall / Allowlist</span><h1>IP não autorizado</h1>
    <p>Seu acesso à RPA foi bloqueado pela política de allowlist. Solicite ao administrador a inclusão do IP <code>{ip}</code>.</p>
    </div></div></body></html>
    """
    return HTMLResponse(content=html, status_code=403)


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
_local_cache: dict[str, tuple[float, dict]] = {}
_local_cache_lock = threading.Lock()
_queue_dispatch_task: Optional[asyncio.Task] = None
_queue_dispatch_parallelism = max(1, QUEUE_DISPATCH_PARALLELISM)


def _local_cache_get(placa: str) -> Optional[dict]:
    key = placa.upper()
    with _local_cache_lock:
        item = _local_cache.get(key)
        if not item:
            return None
        expires_at, dados = item
        if _time.time() >= expires_at:
            _local_cache.pop(key, None)
            return None
        return dados


def _local_cache_set(placa: str, dados: dict) -> None:
    key = placa.upper()
    with _local_cache_lock:
        _local_cache[key] = (_time.time() + CACHE_TTL, dados)


def _local_cache_delete(placa: str) -> bool:
    key = placa.upper()
    with _local_cache_lock:
        return _local_cache.pop(key, None) is not None


def _local_cache_flush() -> int:
    with _local_cache_lock:
        count = len(_local_cache)
        _local_cache.clear()
        return count


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
    local = _local_cache_get(placa)
    if local:
        logger.info(f"[cache] L1 HIT para placa '{placa}'")
        return local

    if _redis is None:
        return None
    try:
        raw = _redis.get(f"placa:{placa.upper()}")
        if raw:
            dados = json.loads(raw)
            _local_cache_set(placa, dados)
            logger.info(f"[cache] Redis HIT para placa '{placa}'")
            return dados
    except Exception as e:
        logger.warning(f"[cache] Erro ao ler cache: {e}")
    return None


def _cache_set(placa: str, dados: dict) -> None:
    _local_cache_set(placa, dados)
    if _redis is None:
        return
    try:
        _redis.setex(f"placa:{placa.upper()}", CACHE_TTL, json.dumps(dados))
        logger.info(f"[cache] Armazenado '{placa}' por {CACHE_TTL}s.")
    except Exception as e:
        logger.warning(f"[cache] Erro ao gravar cache: {e}")


def _cache_delete(placa: str) -> bool:
    local_deleted = _local_cache_delete(placa)
    if _redis is None:
        return local_deleted
    try:
        deleted = _redis.delete(f"placa:{placa.upper()}")
        return local_deleted or deleted > 0
    except Exception:
        return local_deleted


def _cache_flush() -> int:
    local_count = _local_cache_flush()
    if _redis is None:
        return local_count
    try:
        keys = _redis.keys("placa:*")
        if keys:
            return max(local_count, _redis.delete(*keys))
        return local_count
    except Exception:
        return local_count


def _queue_push(req_id: str) -> bool:
    if _redis is None:
        return False
    try:
        _redis.rpush(QUEUE_REDIS_KEY, req_id)
        return True
    except Exception as e:
        logger.error(f"[queue] Falha ao enfileirar {req_id} no Redis: {e}")
        return False


def _queue_pop_one() -> Optional[str]:
    if _redis is None:
        return None
    try:
        item = _redis.lpop(QUEUE_REDIS_KEY)
        if item is None:
            return None
        return str(item)
    except Exception as e:
        logger.error(f"[queue] Falha ao consumir fila Redis: {e}")
        return None


def _queue_rebuild_from_db() -> int:
    if _redis is None:
        return 0
    try:
        _redis.delete(QUEUE_REDIS_KEY)
    except Exception:
        pass
    reset_count = database.reset_processing_queued_requests()
    req_ids = database.list_queued_request_ids(statuses=("queued",), limit=10000)
    if req_ids:
        try:
            _redis.rpush(QUEUE_REDIS_KEY, *req_ids)
        except Exception as e:
            logger.error(f"[queue] Falha ao reconstruir fila no Redis: {e}")
            return 0
    if reset_count:
        logger.info(f"[queue] {reset_count} requisições em processamento voltaram para queued após restart.")
    return len(req_ids)


def _json_load_if_needed(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _build_query_result(
    req_id: str,
    placa: str,
    *,
    ok: bool,
    status: str,
    dados: Optional[dict] = None,
    cache: bool = False,
    attempts: int = 0,
    duration_s: Optional[float] = None,
    detail: Optional[str] = None,
) -> dict:
    payload = {
        "req_id": req_id,
        "placa": placa,
        "ok": ok,
        "status": status,
        "cache": cache,
        "attempts": attempts,
    }
    if duration_s is not None:
        payload["duration_s"] = round(duration_s, 2)
    if dados is not None:
        payload["dados"] = dados
    if detail:
        payload["detail"] = detail
    return payload


def _execute_plate_lookup(req_id: str, placa: str, *, no_cache: bool = False, mode: str = "manual") -> tuple[int, dict]:
    placa = placa.upper().strip()
    if _pool is None:
        detail = "Pool não inicializado."
        return 503, _build_query_result(req_id, placa, ok=False, status="error", detail=detail)

    database.insert_query(req_id, placa)

    if not no_cache:
        cached = _cache_get(placa)
        if cached:
            body = _build_query_result(req_id, placa, ok=True, status="ok", dados=cached, cache=True, attempts=0, duration_s=0.0)
            database.finish_query(req_id, status="ok", cached=True, attempts=0, duration_s=0.0, dados=cached)
            return 200, body

    _log(req_id, "INFO", f"Iniciando consulta ({mode}) (pool disponível: {_pool.available()}/{_pool.size})")

    t_total = _time.time()
    deadline = t_total + API_REQUEST_TIMEOUT_S
    last_exc = None
    attempt = 0

    for attempt in range(1, API_MAX_RETRIES + 1):
        remaining_budget = deadline - _time.time()
        if remaining_budget <= 1.5:
            last_exc = TimeoutError(
                f"Tempo limite da API excedido após {API_REQUEST_TIMEOUT_S}s antes de iniciar a tentativa {attempt}."
            )
            _log(req_id, "ERROR", str(last_exc))
            break

        acquire_timeout = max(1, min(POOL_ACQUIRE_TIMEOUT_S, int(remaining_budget - 1)))
        query_timeout = max(8, min(QUERY_PLATE_TIMEOUT_S, int(remaining_budget - 1)))
        try:
            _log(
                req_id,
                "INFO",
                f"Tentativa {attempt}/{API_MAX_RETRIES} — aguardando driver livre "
                f"(timeout {acquire_timeout}s, budget restante {remaining_budget:.1f}s)..."
            )
            with _pool.acquire(timeout=acquire_timeout) as lease:
                lease.assign_request(req_id=req_id, placa=placa, attempt=attempt, mode=mode)
                lease.set_phase("Consulta iniciada")
                _log(req_id, "INFO", f"Driver obtido. Executando query_plate(timeout={query_timeout}s)...")
                t0 = _time.time()
                dados = query_plate(lease.driver, placa, timeout=query_timeout, progress_callback=lease.set_phase)
                elapsed = _time.time() - t0

                invalidos = _validate_dados(dados)
                if invalidos:
                    raise ValueError(f"Dados incompletos — campos inválidos: {', '.join(invalidos)}")

                total_elapsed = _time.time() - t_total
                _log(req_id, "INFO", f"Consulta OK em {elapsed:.1f}s RPA ({total_elapsed:.1f}s total).")
                _cache_set(placa, dados)
                database.finish_query(
                    req_id, status="ok", cached=False,
                    attempts=attempt, duration_s=round(total_elapsed, 2), dados=dados
                )
                return 200, _build_query_result(
                    req_id, placa,
                    ok=True,
                    status="ok",
                    dados=dados,
                    cache=False,
                    attempts=attempt,
                    duration_s=total_elapsed,
                )

        except (PlacaNaoEncontradaError, DadosVaziosError) as e:
            last_exc = e
            _log(req_id, "WARNING", f"Placa não encontrada no portal: {e}")
            break

        except ValueError as e:
            last_exc = e
            _log(req_id, "WARNING", f"Dados inválidos: {e}")
            if attempt < API_MAX_RETRIES and (_time.time() + 2) < deadline:
                _log(req_id, "INFO", "Retentando com outro driver...")

        except TimeoutError as e:
            last_exc = e
            _log(req_id, "ERROR", f"Tempo esgotado aguardando driver: {e}")
            database.finish_query(req_id, status="error", attempts=attempt, duration_s=round(_time.time() - t_total, 2), error_msg=str(e))
            return 503, _build_query_result(req_id, placa, ok=False, status="error", attempts=attempt, duration_s=_time.time() - t_total, detail=str(e))

        except SeleniumTimeoutException as e:
            last_exc = e
            _log(req_id, "WARNING", f"Timeout do portal na tentativa {attempt}: {e}")
            if attempt < API_MAX_RETRIES and (_time.time() + 5) < deadline:
                _log(req_id, "INFO", f"Retentando ({attempt + 1}/{API_MAX_RETRIES}) após timeout do portal...")
            else:
                break

        except Exception as e:
            last_exc = e
            tb = traceback.format_exc()
            if _is_chrome_crash(e):
                wait_s = 15 * attempt
                _log(req_id, "WARNING", f"Chrome foi encerrado (OOM?) na tentativa {attempt} — aguardando {wait_s}s para pool recriar driver...")
                database.insert_log(req_id, "DEBUG", tb)
                if attempt < API_MAX_RETRIES and (_time.time() + wait_s + 2) < deadline:
                    _time.sleep(wait_s)
                    _log(req_id, "INFO", f"Retentando ({attempt + 1}/{API_MAX_RETRIES}) após crash do Chrome...")
            else:
                _log(req_id, "ERROR", f"Falha na tentativa {attempt}: {e}")
                database.insert_log(req_id, "DEBUG", tb)
                if attempt < API_MAX_RETRIES and (_time.time() + 2) < deadline:
                    _log(req_id, "INFO", f"Retentando ({attempt + 1}/{API_MAX_RETRIES})...")

    total_elapsed = round(_time.time() - t_total, 2)

    if isinstance(last_exc, (PlacaNaoEncontradaError, DadosVaziosError)):
        detail = str(last_exc)
        database.finish_query(req_id, status="not_found", attempts=attempt, duration_s=total_elapsed, error_msg=detail)
        return 404, _build_query_result(req_id, placa, ok=False, status="not_found", attempts=attempt, duration_s=total_elapsed, detail=detail)

    if isinstance(last_exc, SeleniumTimeoutException):
        detail = f"Portal Tokio demorou além do limite operacional da API ({API_REQUEST_TIMEOUT_S}s). Tente novamente."
        database.finish_query(req_id, status="error", attempts=attempt, duration_s=total_elapsed, error_msg=detail)
        return 504, _build_query_result(req_id, placa, ok=False, status="error", attempts=attempt, duration_s=total_elapsed, detail=detail)

    if isinstance(last_exc, TimeoutError):
        detail = str(last_exc)
        database.finish_query(req_id, status="error", attempts=attempt, duration_s=total_elapsed, error_msg=detail)
        return 504, _build_query_result(req_id, placa, ok=False, status="error", attempts=attempt, duration_s=total_elapsed, detail=detail)

    final_status = "invalid_data" if isinstance(last_exc, ValueError) else "error"
    detail = str(last_exc) if last_exc else "Falha desconhecida na consulta."
    database.finish_query(req_id, status=final_status, attempts=attempt, duration_s=total_elapsed, error_msg=detail)
    http_status = 422 if isinstance(last_exc, ValueError) else 500
    return http_status, _build_query_result(req_id, placa, ok=False, status=final_status, attempts=attempt, duration_s=total_elapsed, detail=detail)


async def _deliver_queue_webhook(item: dict, body: dict, http_status: int, webhook_url_override: Optional[str] = None) -> None:
    webhook_url = (webhook_url_override or item.get("webhook_url") or QUEUE_RESULT_WEBHOOK_URL or "").strip()
    if not webhook_url:
        database.mark_queued_request_callback(item["req_id"], status="skipped", attempts=0, error_msg="Webhook não configurado.")
        return

    payload = dict(body)
    payload["http_status"] = http_status
    metadata = _json_load_if_needed(item.get("payload"))
    if metadata is not None:
        payload["request"] = metadata

    attempts = 0
    last_error = None
    async with httpx.AsyncClient(timeout=QUEUE_WEBHOOK_TIMEOUT_S, follow_redirects=True) as client:
        for attempts in range(1, 4):
            try:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
                database.mark_queued_request_callback(item["req_id"], status="sent", attempts=attempts)
                logger.info(f"[queue:{item['req_id']}] Webhook enviado com sucesso para {webhook_url}")
                return
            except Exception as e:
                last_error = str(e)
                logger.warning(f"[queue:{item['req_id']}] Falha ao enviar webhook ({attempts}/3): {e}")
                await asyncio.sleep(min(3 * attempts, 8))

    database.mark_queued_request_callback(item["req_id"], status="error", attempts=attempts, error_msg=last_error)


async def _process_queued_request(req_id: str) -> None:
    item = database.get_queued_request(req_id)
    if not item:
        return
    if item.get("status") not in {"queued", "processing"}:
        return
    if item.get("status") == "queued" and not database.start_queued_request(req_id):
        return

    placa = str(item.get("placa") or "").strip().upper()
    no_cache = bool(item.get("no_cache"))
    loop = asyncio.get_event_loop()
    http_status, body = await loop.run_in_executor(
        None,
        lambda: _execute_plate_lookup(req_id, placa, no_cache=no_cache, mode="queue"),
    )
    queue_status = "done" if http_status < 400 else body.get("status", "error")
    database.finish_queued_request(req_id, status=queue_status, http_status=http_status, result_body=body)
    await _deliver_queue_webhook(item, body, http_status)


async def _queue_dispatcher() -> None:
    active_tasks: set[asyncio.Task] = set()
    while True:
        try:
            active_tasks = {task for task in active_tasks if not task.done()}
            if _redis is None:
                await asyncio.sleep(max(2.0, QUEUE_POLL_INTERVAL_S))
                continue

            if len(active_tasks) >= _queue_dispatch_parallelism:
                await asyncio.sleep(QUEUE_POLL_INTERVAL_S)
                continue

            req_id = await asyncio.get_event_loop().run_in_executor(None, _queue_pop_one)
            if not req_id:
                await asyncio.sleep(QUEUE_POLL_INTERVAL_S)
                continue

            task = asyncio.create_task(_process_queued_request(req_id))
            active_tasks.add(task)
        except Exception as e:
            logger.error(f"[queue] Erro no dispatcher: {e}")
            await asyncio.sleep(max(2.0, QUEUE_POLL_INTERVAL_S))


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


class AllowedIPRequest(BaseModel):
    ip: str
    label: Optional[str] = None
    notes: Optional[str] = None
    enabled: bool = True


class AllowedIPUpdateRequest(BaseModel):
    label: Optional[str] = None
    notes: Optional[str] = None
    enabled: Optional[bool] = None


class QueuePlateRequest(BaseModel):
    placa: str
    no_cache: bool = False
    webhook_url: Optional[str] = None
    metadata: Optional[dict] = None


class QueueWebhookReplayRequest(BaseModel):
    webhook_url: Optional[str] = None


class _NetworkSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        ip = _extract_client_ip(request)
        request.state.client_ip = ip
        request_mode = "api_key" if request.headers.get("x-api-key") else "cookie"
        ua = request.headers.get("user-agent", "")

        allowed_rule = _match_allowed_ip(ip)
        if not allowed_rule:
            _security_event(
                ip=ip,
                method=request.method,
                path=path,
                action="blocked_ip",
                reason="IP fora da allowlist",
                request_mode=request_mode,
                user_agent=ua,
                status_code=403,
            )
            logger.warning(f"[firewall] Bloqueado {ip} em {path}")
            wants_json = path.startswith("/api/") or request.headers.get("x-api-key") or "application/json" in (request.headers.get("accept", ""))
            if wants_json:
                return JSONResponse(status_code=403, content={"detail": f"IP não autorizado: {ip}"})
            return _blocked_ip_response(ip)

        request.state.allowed_ip_rule = allowed_rule
        response = await call_next(request)
        database.touch_allowed_ip(allowed_rule)
        if not path.startswith("/static/"):
            _security_event(
                ip=ip,
                method=request.method,
                path=path,
                action="allowed",
                reason="IP permitido",
                request_mode=request_mode,
                user_agent=ua,
                status_code=response.status_code,
                allowed_rule=allowed_rule,
            )
        return response


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
app.add_middleware(_NetworkSecurityMiddleware)


@app.on_event("startup")
async def startup():
    global _pool, _queue_dispatch_task
    database.init_db()
    seed_ips = set(get_allowed_ip_seeds())
    seed_ips.update(database.get_recent_success_ips(limit=3, min_hits=10))
    database.bootstrap_allowed_ips(sorted(seed_ips))
    database.prune_bootstrap_allowed_ips(sorted(seed_ips))
    _reload_allowed_ip_rules()
    logger.info("[startup] Banco SQLite inicializado.")
    _init_redis()
    headless = get_headless()
    logger.info(f"[startup] Inicializando pool com {POOL_SIZE} drivers (headless={headless})...")
    _pool = DriverPool(size=POOL_SIZE, headless=headless)
    _pool.initialize()
    rebuilt = _queue_rebuild_from_db()
    logger.info(f"[queue] {rebuilt} requisições pendentes carregadas no Redis.")
    logger.info("[startup] Pool pronto. API disponível.")
    _queue_dispatch_task = asyncio.create_task(_queue_dispatcher())


@app.on_event("shutdown")
def shutdown():
    global _queue_dispatch_task
    if _queue_dispatch_task:
        _queue_dispatch_task.cancel()
        _queue_dispatch_task = None
    if _pool:
        _pool.shutdown()


# ---------------------------------------------------------------------------
# Modelos Pydantic para batch
# ---------------------------------------------------------------------------

class BatchManualRequest(BaseModel):
    placas: List[str]
    nome: Optional[str] = "Lote Manual"


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
    reserved_for_api = max(0, min(API_POOL_RESERVE, max(0, POOL_SIZE - 1)))
    batch_parallelism = max(1, POOL_SIZE - reserved_for_api)
    sem = asyncio.Semaphore(batch_parallelism)

    async def _process_one(placa: str) -> None:
        async with sem:
            t0 = _time.time()
            req_id = "b" + uuid.uuid4().hex[:7]
            database.insert_query(req_id, placa)
            max_attempts = 3
            for attempt_num in range(1, max_attempts + 1):
              try:
                def _sync_query():
                    with _pool.acquire(timeout=300, min_available=reserved_for_api) as lease:
                        lease.assign_request(req_id=req_id, placa=placa, attempt=attempt_num, mode="batch")
                        lease.set_phase("Consultando placa em lote")
                        dados = query_plate(lease.driver, placa, progress_callback=lease.set_phase)
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
        _security_event(ip=ip, method="POST", path="/login", action="auth_fail", reason="IP temporariamente bloqueado", request_mode="cookie", user_agent=request.headers.get("user-agent", ""), status_code=423, allowed_rule=getattr(request.state, "allowed_ip_rule", None))
        mins = max(1, _auth.remaining_lock(ip) // 60)
        err = _urlquote(f"IP bloqueado por excesso de tentativas. Aguarde {mins} min.")
        logger.warning(f"[auth] Login bloqueado para IP {ip}")
        return RedirectResponse(url=f"/login?err={err}", status_code=302)

    if _auth.check_credentials(username, password):
        _auth.record_success(ip)
        token = _auth.create_session()
        _security_event(ip=ip, method="POST", path="/login", action="auth_ok", reason="Login bem-sucedido", request_mode="cookie", user_agent=request.headers.get("user-agent", ""), status_code=302, allowed_rule=getattr(request.state, "allowed_ip_rule", None))
        logger.info(f"[auth] Login bem-sucedido: '{username}' de {ip}")
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie(
            _auth.COOKIE_NAME, token,
            httponly=True, samesite="strict", max_age=_auth.SESSION_TTL
        )
        return resp
    else:
        locked = _auth.record_failure(ip)
        _security_event(ip=ip, method="POST", path="/login", action="auth_fail", reason="Credenciais inválidas", request_mode="cookie", user_agent=request.headers.get("user-agent", ""), status_code=401, allowed_rule=getattr(request.state, "allowed_ip_rule", None))
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
        "workers": _pool.snapshot_workers() if _pool else [],
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
        "workers": _pool.snapshot_workers(),
    }


@app.post("/pool/workers/{worker_id}/force-stop")
def force_stop_pool_worker(worker_id: int, x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    if _pool is None:
        raise HTTPException(status_code=503, detail="Pool não inicializado.")
    try:
        return _pool.force_stop_worker(worker_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


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


@app.get("/security")
def security_dashboard():
    path = _os.path.join("static", "security.html")
    if not _os.path.exists(path):
        return HTMLResponse("<h1>Security dashboard não encontrado.</h1>", status_code=404)
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


@app.get("/api/security/allowed-ips")
def api_security_allowed_ips(request: Request, x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    return {
        "current_ip": getattr(request.state, "client_ip", _extract_client_ip(request)),
        "items": database.get_allowed_ips(enabled_only=False),
    }


@app.post("/api/security/allowed-ips")
def api_security_add_allowed_ip(body: AllowedIPRequest, x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    rule = _normalize_ip_rule(body.ip)
    database.upsert_allowed_ip(rule, label=body.label or rule, notes=body.notes, enabled=body.enabled)
    _reload_allowed_ip_rules()
    return {"ok": True, "rule": rule}


@app.patch("/api/security/allowed-ips/{rule:path}")
def api_security_update_allowed_ip(rule: str, body: AllowedIPUpdateRequest, x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    normalized = _normalize_ip_rule(rule)
    if body.enabled is False and database.count_enabled_allowed_ips() <= 1:
        raise HTTPException(status_code=400, detail="Não é permitido desabilitar a última regra ativa.")
    ok = database.update_allowed_ip(normalized, label=body.label, notes=body.notes, enabled=body.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="Regra não encontrada.")
    _reload_allowed_ip_rules()
    return {"ok": True, "rule": normalized}


@app.delete("/api/security/allowed-ips/{rule:path}")
def api_security_delete_allowed_ip(rule: str, x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    normalized = _normalize_ip_rule(rule)
    if database.count_enabled_allowed_ips() <= 1:
        raise HTTPException(status_code=400, detail="Não é permitido remover a última regra ativa.")
    ok = database.delete_allowed_ip(normalized)
    if not ok:
        raise HTTPException(status_code=404, detail="Regra não encontrada.")
    _reload_allowed_ip_rules()
    return {"ok": True, "rule": normalized}


@app.get("/api/security/events")
def api_security_events(
    limit: int = Query(SECURITY_EVENT_LIMIT, ge=1, le=1000),
    action: Optional[str] = Query(None),
    x_api_key: Optional[str] = Header(default=None),
):
    _check_api_key(x_api_key)
    return database.get_security_events(limit=limit, action=action)


@app.get("/api/security/overview")
def api_security_overview(
    request: Request,
    hours: int = Query(24, ge=1, le=720),
    x_api_key: Optional[str] = Header(default=None),
):
    _check_api_key(x_api_key)
    data = database.get_security_overview(hours=hours, limit=10)
    data["current_ip"] = getattr(request.state, "client_ip", _extract_client_ip(request))
    return data


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


@app.get("/api/admin/overview")
def api_admin_overview(x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    return database.get_queue_overview()


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
    database.insert_batch_job(job_id, body.nome or "Lote Manual", placas)
    asyncio.create_task(_run_batch_job(job_id, placas))
    return {"ok": True, "job_id": job_id, "total": len(placas)}


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
    database.insert_batch_job(job_id, nome, placas)
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


@app.post("/queue/placa")
def enqueue_plate_request(
    body: QueuePlateRequest,
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
):
    _check_api_key(x_api_key)
    if _redis is None:
        raise HTTPException(status_code=503, detail="Redis indisponível para fila no momento.")

    placa = (body.placa or "").strip().upper()
    if not placa or len(placa) < 5:
        raise HTTPException(status_code=400, detail="Placa inválida.")

    req_id = "q" + uuid.uuid4().hex[:11]
    metadata = body.metadata or {}
    metadata.setdefault("client_ip", getattr(request.state, "client_ip", _extract_client_ip(request)))
    metadata.setdefault("path", str(request.url.path))
    webhook_url = (body.webhook_url or QUEUE_RESULT_WEBHOOK_URL or "").strip() or None

    database.insert_query(req_id, placa)
    database.insert_queued_request(
        req_id,
        placa,
        source="api_queue",
        no_cache=body.no_cache,
        webhook_url=webhook_url,
        payload={
            "placa": placa,
            "no_cache": body.no_cache,
            "metadata": metadata,
        },
    )
    if not _queue_push(req_id):
        raise HTTPException(status_code=503, detail="Falha ao publicar a solicitação na fila Redis.")

    position = database.get_queue_position(req_id)
    return {
        "ok": True,
        "queued": True,
        "req_id": req_id,
        "placa": placa,
        "status": "queued",
        "queue_position": position,
        "webhook_url": webhook_url,
    }


@app.get("/queue/{req_id}")
def get_queue_request(req_id: str, x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)
    item = database.get_queued_request(req_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item de fila não encontrado.")
    item["payload"] = _json_load_if_needed(item.get("payload"))
    item["result_body"] = _json_load_if_needed(item.get("result_body"))
    item["logs"] = database.get_query_logs(req_id)
    return item


@app.get("/queue")
def list_queue_requests(
    limit: int = Query(100, ge=1, le=500),
    x_api_key: Optional[str] = Header(default=None),
):
    _check_api_key(x_api_key)
    items = database.get_recent_queued_requests(limit=limit)
    for item in items:
        item["payload"] = _json_load_if_needed(item.get("payload"))
        item["result_body"] = _json_load_if_needed(item.get("result_body"))
    return items


@app.post("/queue/{req_id}/requeue")
def requeue_request(
    req_id: str,
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
):
    _check_api_key(x_api_key)
    if _redis is None:
        raise HTTPException(status_code=503, detail="Redis indisponível para fila no momento.")

    item = database.get_queued_request(req_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item de fila não encontrado.")

    placa = str(item.get("placa") or "").strip().upper()
    if not placa:
        raise HTTPException(status_code=400, detail="Placa original inválida.")

    payload = _json_load_if_needed(item.get("payload"))
    metadata = payload.get("metadata") if isinstance(payload, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata = dict(metadata)
    metadata["requeued_from"] = req_id
    metadata["requeued_at_epoch"] = int(_time.time())
    metadata["client_ip"] = getattr(request.state, "client_ip", _extract_client_ip(request))

    new_req_id = "q" + uuid.uuid4().hex[:11]
    webhook_url = (item.get("webhook_url") or QUEUE_RESULT_WEBHOOK_URL or "").strip() or None
    no_cache = bool(item.get("no_cache"))

    database.insert_query(new_req_id, placa)
    database.insert_queued_request(
        new_req_id,
        placa,
        source="api_queue_requeue",
        no_cache=no_cache,
        webhook_url=webhook_url,
        payload={
            "placa": placa,
            "no_cache": no_cache,
            "metadata": metadata,
        },
    )
    if not _queue_push(new_req_id):
        raise HTTPException(status_code=503, detail="Falha ao reenfileirar a solicitação na fila Redis.")

    return {
        "ok": True,
        "queued": True,
        "req_id": new_req_id,
        "original_req_id": req_id,
        "placa": placa,
        "status": "queued",
        "queue_position": database.get_queue_position(new_req_id),
        "webhook_url": webhook_url,
    }


@app.post("/queue/{req_id}/resend-webhook")
async def resend_queue_webhook(
    req_id: str,
    body: Optional[QueueWebhookReplayRequest] = None,
    x_api_key: Optional[str] = Header(default=None),
):
    _check_api_key(x_api_key)
    item = database.get_queued_request(req_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item de fila não encontrado.")

    result_body = _json_load_if_needed(item.get("result_body"))
    if not isinstance(result_body, dict):
        raise HTTPException(status_code=400, detail="A requisição ainda não possui resultado para reenviar.")

    webhook_url = (body.webhook_url.strip() if body and body.webhook_url else "") or None
    await _deliver_queue_webhook(item, result_body, int(item.get("http_status") or 200), webhook_url_override=webhook_url)
    updated = database.get_queued_request(req_id) or item
    return {
        "ok": True,
        "req_id": req_id,
        "callback_status": updated.get("callback_status"),
        "callback_attempts": updated.get("callback_attempts"),
        "callback_last_error": updated.get("callback_last_error"),
        "webhook_url": webhook_url or item.get("webhook_url") or QUEUE_RESULT_WEBHOOK_URL,
    }


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
    req_id = uuid.uuid4().hex[:8]
    http_status, body = _execute_plate_lookup(req_id, placa, no_cache=bool(no_cache), mode="manual")
    if http_status >= 400:
        raise HTTPException(status_code=http_status, detail=body.get("detail") or body.get("status") or "Falha na consulta.")
    return JSONResponse(content=body)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=False)
