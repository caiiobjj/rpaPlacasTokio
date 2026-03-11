"""
Persistência SQLite para logs e histórico de consultas da RPA.

Tabelas:
  queries – uma linha por requisição GET /placa/{placa}
  logs    – eventos vinculados a cada req_id (INFO / WARNING / ERROR)
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

_DB_PATH = "rpa_logs.db"
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def init_db(path: str = _DB_PATH) -> None:
    global _DB_PATH
    _DB_PATH = path
    with _connect() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS queries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                req_id      TEXT    NOT NULL UNIQUE,
                placa       TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'pending',
                cached      INTEGER NOT NULL DEFAULT 0,
                attempts    INTEGER NOT NULL DEFAULT 0,
                duration_s  REAL,
                error_msg   TEXT,
                dados       TEXT,
                created_at  TEXT    NOT NULL,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                req_id     TEXT NOT NULL,
                level      TEXT NOT NULL,
                message    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS batch_jobs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id       TEXT    NOT NULL UNIQUE,
                name         TEXT    NOT NULL DEFAULT 'Lote',
                status       TEXT    NOT NULL DEFAULT 'pending',
                total        INTEGER NOT NULL DEFAULT 0,
                done         INTEGER NOT NULL DEFAULT 0,
                ok           INTEGER NOT NULL DEFAULT 0,
                not_found    INTEGER NOT NULL DEFAULT 0,
                errors       INTEGER NOT NULL DEFAULT 0,
                scheduled_at TEXT,
                started_at   TEXT,
                finished_at  TEXT,
                created_at   TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS batch_results (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id       TEXT NOT NULL,
                placa        TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                veiculo      TEXT,
                dados        TEXT,
                error_msg    TEXT,
                duration_s   REAL,
                processed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_queries_placa      ON queries(placa);
            CREATE INDEX IF NOT EXISTS idx_queries_created    ON queries(created_at);
            CREATE INDEX IF NOT EXISTS idx_logs_req_id        ON logs(req_id);
            CREATE INDEX IF NOT EXISTS idx_batch_results_job  ON batch_results(job_id);
            CREATE INDEX IF NOT EXISTS idx_batch_results_placa ON batch_results(placa);

            CREATE TABLE IF NOT EXISTS access_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ip          TEXT,
                method      TEXT,
                path        TEXT,
                status      INTEGER,
                duration_ms REAL,
                placa       TEXT,
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_access_logs_created ON access_logs(created_at);

            CREATE TABLE IF NOT EXISTS queued_requests (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                req_id              TEXT    NOT NULL UNIQUE,
                placa               TEXT    NOT NULL,
                status              TEXT    NOT NULL DEFAULT 'queued',
                source              TEXT,
                no_cache            INTEGER NOT NULL DEFAULT 0,
                webhook_url         TEXT,
                payload             TEXT,
                result_body         TEXT,
                http_status         INTEGER,
                enqueue_attempts    INTEGER NOT NULL DEFAULT 0,
                callback_status     TEXT    NOT NULL DEFAULT 'pending',
                callback_attempts   INTEGER NOT NULL DEFAULT 0,
                callback_last_error TEXT,
                created_at          TEXT    NOT NULL,
                started_at          TEXT,
                finished_at         TEXT,
                callback_sent_at    TEXT,
                updated_at          TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_queued_requests_status ON queued_requests(status);
            CREATE INDEX IF NOT EXISTS idx_queued_requests_created ON queued_requests(created_at);

            CREATE TABLE IF NOT EXISTS allowed_ips (
                ip          TEXT PRIMARY KEY,
                label       TEXT,
                notes       TEXT,
                enabled     INTEGER NOT NULL DEFAULT 1,
                hit_count   INTEGER NOT NULL DEFAULT 0,
                last_seen   TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_allowed_ips_enabled ON allowed_ips(enabled);

            CREATE TABLE IF NOT EXISTS security_events (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                ip                 TEXT,
                method             TEXT,
                path               TEXT,
                action             TEXT NOT NULL,
                reason             TEXT,
                user_agent         TEXT,
                request_mode       TEXT,
                status_code        INTEGER,
                allowed_rule       TEXT,
                created_at         TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_security_events_created ON security_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_security_events_ip ON security_events(ip);
            CREATE INDEX IF NOT EXISTS idx_security_events_action ON security_events(action);
        """)


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------------------
# Escrita
# ---------------------------------------------------------------------------

def insert_query(req_id: str, placa: str) -> None:
    """Registra início da consulta."""
    with _lock, _connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO queries (req_id, placa, status, created_at) VALUES (?,?,?,?)",
            (req_id, placa.upper(), "pending", _now()),
        )


def finish_query(
    req_id: str,
    *,
    status: str,
    cached: bool = False,
    attempts: int = 1,
    duration_s: Optional[float] = None,
    error_msg: Optional[str] = None,
    dados: Optional[dict] = None,
) -> None:
    """Atualiza a consulta com resultado final."""
    dados_json = json.dumps(dados, ensure_ascii=False) if dados else None
    with _lock, _connect() as con:
        con.execute(
            """UPDATE queries SET
                status=?, cached=?, attempts=?, duration_s=?,
                error_msg=?, dados=?, finished_at=?
               WHERE req_id=?""",
            (
                status,
                int(cached),
                attempts,
                duration_s,
                error_msg,
                dados_json,
                _now(),
                req_id,
            ),
        )


def insert_log(req_id: str, level: str, message: str) -> None:
    """Registra um evento de log vinculado a req_id."""
    with _lock, _connect() as con:
        con.execute(
            "INSERT INTO logs (req_id, level, message, created_at) VALUES (?,?,?,?)",
            (req_id, level.upper(), message, _now()),
        )


# ---------------------------------------------------------------------------
# Leitura (usada pelos endpoints do dashboard)
# ---------------------------------------------------------------------------

def get_recent_queries(limit: int = 100) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            """SELECT req_id, placa, status, cached, attempts, duration_s,
                      error_msg, dados, created_at, finished_at
               FROM queries
               ORDER BY id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_query_logs(req_id: str) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT level, message, created_at FROM logs WHERE req_id=? ORDER BY id ASC",
            (req_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    with _connect() as con:
        row = con.execute("""
            SELECT
                COUNT(*)                                        AS total,
                SUM(status='ok')                                AS ok,
                SUM(status='error')                             AS errors,
                SUM(status='invalid_data')                      AS invalid,
                SUM(status='pending')                           AS pending,
                SUM(cached=1)                                   AS from_cache,
                ROUND(AVG(CASE WHEN status='ok' AND cached=0
                               THEN duration_s END), 2)         AS avg_duration_s,
                ROUND(MIN(CASE WHEN status='ok' AND cached=0
                               THEN duration_s END), 2)         AS min_duration_s,
                ROUND(MAX(CASE WHEN status='ok' AND cached=0
                               THEN duration_s END), 2)         AS max_duration_s,
                COUNT(DISTINCT placa)                           AS unique_placas,
                COUNT(DISTINCT CASE WHEN dados IS NOT NULL THEN placa END) AS db_placas
            FROM queries
        """).fetchone()
    return dict(row)


def get_error_summary(limit: int = 20) -> list[dict]:
    """Últimos erros agrupados por mensagem."""
    with _connect() as con:
        rows = con.execute("""
            SELECT error_msg, COUNT(*) AS count, MAX(created_at) AS last_seen
            FROM queries
            WHERE status IN ('error','invalid_data') AND error_msg IS NOT NULL
            GROUP BY error_msg
            ORDER BY last_seen DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Batch jobs
# ---------------------------------------------------------------------------

def insert_batch_job(job_id: str, name: str, placas: list) -> None:
    """Cria um novo lote para execução imediata e insere as placas pendentes."""
    now = _now()
    with _lock, _connect() as con:
        con.execute(
            """INSERT INTO batch_jobs (job_id, name, status, total, scheduled_at, created_at)
               VALUES (?,?,?,?,?,?)""",
            (job_id, name, "aguardando", len(placas), None, now),
        )
        con.executemany(
            "INSERT INTO batch_results (job_id, placa, status) VALUES (?,?,?)",
            [(job_id, p.upper(), "pendente") for p in placas],
        )


def start_batch_job(job_id: str) -> None:
    with _lock, _connect() as con:
        con.execute(
            "UPDATE batch_jobs SET status='executando', started_at=? WHERE job_id=?",
            (_now(), job_id),
        )


def update_batch_result(
    job_id: str,
    placa: str,
    status: str,                  # ok | not_found | error
    dados: Optional[dict] = None,
    error_msg: Optional[str] = None,
    duration_s: Optional[float] = None,
) -> None:
    """Atualiza resultado individual de uma placa no lote."""
    veiculo = (dados or {}).get("veiculo") if dados else None
    dados_json = json.dumps(dados, ensure_ascii=False) if dados else None
    now = _now()
    col_inc = {"ok": "ok", "not_found": "not_found", "error": "errors"}.get(status, "errors")
    with _lock, _connect() as con:
        con.execute(
            """UPDATE batch_results SET status=?, veiculo=?, dados=?, error_msg=?,
               duration_s=?, processed_at=? WHERE job_id=? AND placa=?""",
            (status, veiculo, dados_json, error_msg, duration_s, now, job_id, placa),
        )
        con.execute(
            f"UPDATE batch_jobs SET done=done+1, {col_inc}={col_inc}+1 WHERE job_id=?",
            (job_id,),
        )


def finish_batch_job(job_id: str) -> None:
    with _lock, _connect() as con:
        con.execute(
            "UPDATE batch_jobs SET status='concluido', finished_at=? WHERE job_id=?",
            (_now(), job_id),
        )


def cancel_batch_job(job_id: str) -> bool:
    with _lock, _connect() as con:
        cur = con.execute(
            "UPDATE batch_jobs SET status='cancelado', finished_at=? WHERE job_id=? AND status IN ('aguardando','pendente','executando')",
            (_now(), job_id),
        )
        return cur.rowcount > 0


def get_batch_jobs(limit: int = 50) -> list:
    with _connect() as con:
        rows = con.execute(
            """SELECT job_id, name, status, total, done, ok, not_found, errors,
                      scheduled_at, started_at, finished_at, created_at
               FROM batch_jobs ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_batch_job(job_id: str) -> Optional[dict]:
    with _connect() as con:
        row = con.execute(
            """SELECT job_id, name, status, total, done, ok, not_found, errors,
                      scheduled_at, started_at, finished_at, created_at
               FROM batch_jobs WHERE job_id=?""",
            (job_id,),
        ).fetchone()
    return dict(row) if row else None


def get_batch_results(job_id: str, limit: int = 500) -> list:
    with _connect() as con:
        rows = con.execute(
            """SELECT placa, status, veiculo, error_msg, duration_s, processed_at
               FROM batch_results WHERE job_id=? ORDER BY id ASC LIMIT ?""",
            (job_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_batch_placas(job_id: str) -> list:
    """Retorna placas pendentes de um lote para reprocessamento."""
    with _connect() as con:
        rows = con.execute(
            "SELECT placa FROM batch_results WHERE job_id=? AND status='pendente' ORDER BY id ASC",
            (job_id,),
        ).fetchall()
    return [r["placa"] for r in rows]


def get_database_placas(limit: int = 5000) -> list:
    """Retorna todas as placas com dados OK (banco de dados de veículos)."""
    with _connect() as con:
        rows = con.execute(
            """SELECT placa, veiculo, dados
               FROM batch_results WHERE status='ok'
               ORDER BY processed_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Access Logs
# ---------------------------------------------------------------------------

def insert_access_log(
    ip: str,
    method: str,
    path: str,
    status: int,
    duration_ms: float,
    placa: Optional[str] = None,
) -> None:
    with _lock, _connect() as con:
        con.execute(
            """INSERT INTO access_logs (ip, method, path, status, duration_ms, placa, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (ip, method.upper(), path, status, round(duration_ms, 1), placa, _now()),
        )


def get_access_logs(limit: int = 200) -> list:
    with _connect() as con:
        rows = con.execute(
            """SELECT ip, method, path, status, duration_ms, placa, created_at
               FROM access_logs ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Queue / Webhook
# ---------------------------------------------------------------------------

def insert_queued_request(
    req_id: str,
    placa: str,
    *,
    source: str = "api",
    no_cache: bool = False,
    webhook_url: Optional[str] = None,
    payload: Optional[dict] = None,
) -> None:
    now = _now()
    payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    with _lock, _connect() as con:
        con.execute(
            """INSERT OR REPLACE INTO queued_requests (
                   req_id, placa, status, source, no_cache, webhook_url, payload,
                   enqueue_attempts, callback_status, created_at, updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (req_id, placa.upper(), "queued", source, int(no_cache), webhook_url,
             payload_json, 1, "pending", now, now),
        )


def start_queued_request(req_id: str) -> bool:
    now = _now()
    with _lock, _connect() as con:
        cur = con.execute(
            """UPDATE queued_requests
               SET status='processing', started_at=COALESCE(started_at, ?), updated_at=?
               WHERE req_id=? AND status='queued'""",
            (now, now, req_id),
        )
        return cur.rowcount > 0


def finish_queued_request(
    req_id: str,
    *,
    status: str,
    http_status: int,
    result_body: Optional[dict] = None,
    callback_status: Optional[str] = None,
    callback_attempts: Optional[int] = None,
    callback_last_error: Optional[str] = None,
) -> None:
    now = _now()
    result_json = json.dumps(result_body, ensure_ascii=False) if result_body is not None else None
    fields = [
        "status=?",
        "http_status=?",
        "result_body=?",
        "finished_at=?",
        "updated_at=?",
    ]
    params: list = [status, http_status, result_json, now, now]
    if callback_status is not None:
        fields.append("callback_status=?")
        params.append(callback_status)
    if callback_attempts is not None:
        fields.append("callback_attempts=?")
        params.append(callback_attempts)
    if callback_last_error is not None:
        fields.append("callback_last_error=?")
        params.append(callback_last_error)
    params.append(req_id)
    with _lock, _connect() as con:
        con.execute(
            f"UPDATE queued_requests SET {', '.join(fields)} WHERE req_id=?",
            tuple(params),
        )


def mark_queued_request_callback(
    req_id: str,
    *,
    status: str,
    attempts: int,
    error_msg: Optional[str] = None,
) -> None:
    now = _now()
    with _lock, _connect() as con:
        con.execute(
            """UPDATE queued_requests
               SET callback_status=?, callback_attempts=?, callback_last_error=?,
                   callback_sent_at=?, updated_at=?
               WHERE req_id=?""",
            (status, attempts, error_msg, now if status == 'sent' else None, now, req_id),
        )


def get_queued_request(req_id: str) -> Optional[dict]:
    with _connect() as con:
        row = con.execute(
            """SELECT req_id, placa, status, source, no_cache, webhook_url, payload,
                      result_body, http_status, enqueue_attempts, callback_status,
                      callback_attempts, callback_last_error, created_at, started_at,
                      finished_at, callback_sent_at, updated_at
               FROM queued_requests
               WHERE req_id=?""",
            (req_id,),
        ).fetchone()
    return dict(row) if row else None


def get_recent_queued_requests(limit: int = 100) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            """SELECT req_id, placa, status, source, no_cache, webhook_url, payload,
                      result_body, http_status, enqueue_attempts, callback_status,
                      callback_attempts, callback_last_error, created_at, started_at,
                      finished_at, callback_sent_at, updated_at
               FROM queued_requests
               ORDER BY id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_queue_overview() -> dict:
    with _connect() as con:
        queue_row = con.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(status='queued') AS queued,
                SUM(status='processing') AS processing,
                SUM(status='done') AS done,
                SUM(status IN ('error','invalid_data','not_found')) AS failed,
                SUM(callback_status='sent') AS callback_sent,
                SUM(callback_status='pending') AS callback_pending,
                SUM(callback_status='error') AS callback_error,
                SUM(callback_status='skipped') AS callback_skipped,
                ROUND(AVG(CASE
                    WHEN started_at IS NOT NULL
                    THEN (julianday(started_at) - julianday(created_at)) * 86400
                END), 2) AS avg_wait_s,
                ROUND(AVG(CASE
                    WHEN started_at IS NOT NULL AND finished_at IS NOT NULL
                    THEN (julianday(finished_at) - julianday(started_at)) * 86400
                END), 2) AS avg_processing_s,
                ROUND(AVG(CASE
                    WHEN finished_at IS NOT NULL
                    THEN (julianday(finished_at) - julianday(created_at)) * 86400
                END), 2) AS avg_total_s,
                ROUND(MAX(CASE
                    WHEN status='queued'
                    THEN (julianday('now') - julianday(created_at)) * 86400
                END), 2) AS oldest_queued_s,
                ROUND(MAX(CASE
                    WHEN status='processing' AND started_at IS NOT NULL
                    THEN (julianday('now') - julianday(started_at)) * 86400
                END), 2) AS longest_processing_s,
                SUM(created_at >= datetime('now', '-1 hour')) AS enqueued_1h,
                SUM(created_at >= datetime('now', '-24 hour')) AS enqueued_24h,
                SUM(finished_at >= datetime('now', '-1 hour')) AS finished_1h,
                SUM(finished_at >= datetime('now', '-24 hour')) AS finished_24h,
                SUM(callback_sent_at >= datetime('now', '-1 hour')) AS sent_1h,
                SUM(callback_sent_at >= datetime('now', '-24 hour')) AS sent_24h
            FROM queued_requests
            """
        ).fetchone()
        outcome_row = con.execute(
            """
            SELECT
                SUM(q.status='ok') AS ok,
                SUM(q.status='not_found') AS not_found,
                SUM(q.status='invalid_data') AS invalid_data,
                SUM(q.status='error') AS error,
                SUM(q.cached=1) AS cached,
                ROUND(AVG(CASE WHEN q.status='ok' THEN q.duration_s END), 2) AS avg_success_s,
                ROUND(MIN(CASE WHEN q.duration_s IS NOT NULL AND q.duration_s > 0 THEN q.duration_s END), 2) AS min_duration_s,
                ROUND(MAX(CASE WHEN q.duration_s IS NOT NULL THEN q.duration_s END), 2) AS max_duration_s
            FROM queued_requests qr
            LEFT JOIN queries q ON q.req_id = qr.req_id
            """
        ).fetchone()
        callback_errors = con.execute(
            """
            SELECT req_id, placa, callback_last_error, updated_at
            FROM queued_requests
            WHERE callback_status='error' AND callback_last_error IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 5
            """
        ).fetchall()

    queue = dict(queue_row or {})
    outcomes = dict(outcome_row or {})

    for key, value in list(queue.items()):
        if value is None:
            queue[key] = 0
    for key, value in list(outcomes.items()):
        if value is None:
            outcomes[key] = 0

    finished_total = (queue.get("done") or 0) + (queue.get("failed") or 0)
    callback_total = (queue.get("callback_sent") or 0) + (queue.get("callback_error") or 0) + (queue.get("callback_skipped") or 0)
    queue["success_rate"] = round(((outcomes.get("ok") or 0) / finished_total) * 100, 1) if finished_total else 0.0
    queue["callback_success_rate"] = round(((queue.get("callback_sent") or 0) / callback_total) * 100, 1) if callback_total else 0.0

    return {
        "queue": queue,
        "outcomes": outcomes,
        "recent_callback_errors": [dict(r) for r in callback_errors],
    }


def list_queued_request_ids(statuses: tuple[str, ...] = ("queued",), limit: int = 1000) -> list[str]:
    placeholders = ",".join("?" for _ in statuses)
    with _connect() as con:
        rows = con.execute(
            f"""SELECT req_id
                FROM queued_requests
                WHERE status IN ({placeholders})
                ORDER BY id ASC
                LIMIT ?""",
            tuple(statuses) + (limit,),
        ).fetchall()
    return [r["req_id"] for r in rows]


def reset_processing_queued_requests() -> int:
    now = _now()
    with _lock, _connect() as con:
        cur = con.execute(
            """UPDATE queued_requests
               SET status='queued', updated_at=?
               WHERE status='processing'""",
            (now,),
        )
        return cur.rowcount


def get_queue_position(req_id: str) -> Optional[int]:
    with _connect() as con:
        row = con.execute(
            """SELECT position FROM (
                   SELECT req_id, ROW_NUMBER() OVER (ORDER BY id ASC) AS position
                   FROM queued_requests
                   WHERE status='queued'
               ) WHERE req_id=?""",
            (req_id,),
        ).fetchone()
    return int(row["position"]) if row else None


# ---------------------------------------------------------------------------
# Security / Firewall
# ---------------------------------------------------------------------------

def bootstrap_allowed_ips(ips: list[str]) -> None:
    now = _now()
    payload = [(ip.strip(), ip.strip(), "Bootstrap automático", 1, now, now) for ip in ips if ip and ip.strip()]
    if not payload:
        return
    with _lock, _connect() as con:
        con.executemany(
            """INSERT OR IGNORE INTO allowed_ips (ip, label, notes, enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            payload,
        )


def get_recent_success_ips(limit: int = 5, min_hits: int = 3) -> list[str]:
    with _connect() as con:
        rows = con.execute(
            """SELECT ip
               FROM access_logs
               WHERE ip IS NOT NULL AND ip <> '' AND status < 400
               GROUP BY ip
               HAVING COUNT(*) >= ?
               ORDER BY COUNT(*) DESC, MAX(id) DESC
               LIMIT ?""",
            (min_hits, limit),
        ).fetchall()
    return [r["ip"] for r in rows]


def prune_bootstrap_allowed_ips(keep_ips: list[str]) -> int:
    keep = {ip for ip in keep_ips if ip}
    with _lock, _connect() as con:
        rows = con.execute(
            """SELECT ip FROM allowed_ips
               WHERE notes='Bootstrap automático'
                 AND COALESCE(hit_count, 0)=0
                 AND last_seen IS NULL"""
        ).fetchall()
        targets = [r["ip"] for r in rows if r["ip"] not in keep]
        if not targets:
            return 0
        con.executemany("DELETE FROM allowed_ips WHERE ip=?", [(ip,) for ip in targets])
        return len(targets)


def get_allowed_ips(*, enabled_only: bool = False) -> list[dict]:
    sql = """SELECT ip, label, notes, enabled, hit_count, last_seen, created_at, updated_at
             FROM allowed_ips"""
    params: tuple = ()
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY enabled DESC, updated_at DESC, ip ASC"
    with _connect() as con:
        rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_enabled_allowed_ips() -> int:
    with _connect() as con:
        row = con.execute("SELECT COUNT(*) AS total FROM allowed_ips WHERE enabled=1").fetchone()
    return int((row["total"] if row else 0) or 0)


def upsert_allowed_ip(ip: str, label: Optional[str] = None, notes: Optional[str] = None, enabled: bool = True) -> None:
    now = _now()
    with _lock, _connect() as con:
        con.execute(
            """INSERT INTO allowed_ips (ip, label, notes, enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(ip) DO UPDATE SET
                   label=excluded.label,
                   notes=excluded.notes,
                   enabled=excluded.enabled,
                   updated_at=excluded.updated_at""",
            (ip, label or ip, notes, int(enabled), now, now),
        )


def update_allowed_ip(ip: str, *, label: Optional[str] = None, notes: Optional[str] = None, enabled: Optional[bool] = None) -> bool:
    fields = []
    params = []
    if label is not None:
        fields.append("label=?")
        params.append(label)
    if notes is not None:
        fields.append("notes=?")
        params.append(notes)
    if enabled is not None:
        fields.append("enabled=?")
        params.append(int(enabled))
    if not fields:
        return False
    fields.append("updated_at=?")
    params.append(_now())
    params.append(ip)
    with _lock, _connect() as con:
        cur = con.execute(f"UPDATE allowed_ips SET {', '.join(fields)} WHERE ip=?", tuple(params))
        return cur.rowcount > 0


def delete_allowed_ip(ip: str) -> bool:
    with _lock, _connect() as con:
        cur = con.execute("DELETE FROM allowed_ips WHERE ip=?", (ip,))
        return cur.rowcount > 0


def touch_allowed_ip(ip: str) -> None:
    with _lock, _connect() as con:
        con.execute(
            """UPDATE allowed_ips
               SET hit_count=hit_count+1, last_seen=?, updated_at=?
               WHERE ip=?""",
            (_now(), _now(), ip),
        )


def insert_security_event(
    *,
    ip: str,
    method: str,
    path: str,
    action: str,
    reason: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_mode: Optional[str] = None,
    status_code: Optional[int] = None,
    allowed_rule: Optional[str] = None,
) -> None:
    with _lock, _connect() as con:
        con.execute(
            """INSERT INTO security_events (
                   ip, method, path, action, reason, user_agent,
                   request_mode, status_code, allowed_rule, created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (ip, method.upper(), path, action, reason, user_agent, request_mode, status_code, allowed_rule, _now()),
        )


def get_security_events(limit: int = 300, action: Optional[str] = None) -> list[dict]:
    sql = """SELECT ip, method, path, action, reason, user_agent,
                    request_mode, status_code, allowed_rule, created_at
             FROM security_events"""
    params: list = []
    if action:
        sql += " WHERE action=?"
        params.append(action)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _connect() as con:
        rows = con.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def get_security_overview(hours: int = 24, limit: int = 10) -> dict:
    cutoff = _iso_hours_ago(hours)
    with _connect() as con:
        summary = con.execute(
            """SELECT
                   SUM(CASE WHEN action='allowed' THEN 1 ELSE 0 END) AS allowed_24h,
                   SUM(CASE WHEN action='blocked_ip' THEN 1 ELSE 0 END) AS blocked_24h,
                   SUM(CASE WHEN action='auth_fail' THEN 1 ELSE 0 END) AS auth_fail_24h,
                   COUNT(DISTINCT ip) AS unique_ips_24h,
                   MAX(CASE WHEN action='blocked_ip' THEN created_at END) AS last_blocked_at
               FROM security_events
               WHERE created_at >= ?""",
            (cutoff,),
        ).fetchone()
        top_blocked = con.execute(
            """SELECT ip, COUNT(*) AS total, MAX(created_at) AS last_seen
               FROM security_events
               WHERE action='blocked_ip' AND created_at >= ?
               GROUP BY ip
               ORDER BY total DESC, last_seen DESC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        top_allowed = con.execute(
            """SELECT ip, COUNT(*) AS total, MAX(created_at) AS last_seen
               FROM security_events
               WHERE action='allowed' AND created_at >= ?
               GROUP BY ip
               ORDER BY total DESC, last_seen DESC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        hot_paths = con.execute(
            """SELECT path,
                      COUNT(*) AS total,
                      SUM(CASE WHEN action='blocked_ip' THEN 1 ELSE 0 END) AS blocked,
                      SUM(CASE WHEN action='allowed' THEN 1 ELSE 0 END) AS allowed
               FROM security_events
               WHERE created_at >= ?
               GROUP BY path
               ORDER BY total DESC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        actions = con.execute(
            """SELECT action, COUNT(*) AS total
               FROM security_events
               WHERE created_at >= ?
               GROUP BY action
               ORDER BY total DESC""",
            (cutoff,),
        ).fetchall()
    return {
        "summary": dict(summary) if summary else {},
        "allowed_total": len(get_allowed_ips(enabled_only=False)),
        "enabled_total": count_enabled_allowed_ips(),
        "top_blocked_ips": [dict(r) for r in top_blocked],
        "top_allowed_ips": [dict(r) for r in top_allowed],
        "hot_paths": [dict(r) for r in hot_paths],
        "actions": [dict(r) for r in actions],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso_hours_ago(hours: int) -> str:
    delta = datetime.now(timezone.utc).timestamp() - (hours * 3600)
    dt = datetime.fromtimestamp(delta, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
