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

def insert_batch_job(job_id: str, name: str, placas: list, scheduled_at: Optional[str] = None) -> None:
    """Cria um novo lote e insere as placas com status pending."""
    now = _now()
    with _lock, _connect() as con:
        con.execute(
            """INSERT INTO batch_jobs (job_id, name, status, total, scheduled_at, created_at)
               VALUES (?,?,?,?,?,?)""",
            (job_id, name, "pendente" if scheduled_at else "aguardando", len(placas), scheduled_at, now),
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


def get_pending_scheduled_jobs() -> list:
    """Retorna lotes agendados cujo scheduled_at já passou."""
    now = _now()
    with _connect() as con:
        rows = con.execute(
            "SELECT job_id FROM batch_jobs WHERE status='pendente' AND scheduled_at <= ?",
            (now,),
        ).fetchall()
    return [r["job_id"] for r in rows]


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
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
