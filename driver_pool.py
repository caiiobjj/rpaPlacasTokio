"""
Pool de drivers Chrome para execução paralela de consultas RPA.

Cada requisição faz checkout de um driver livre do pool, executa a consulta
e faz checkin. O checkin é assíncrono: verifica se o driver está vivo e o
prepara para a próxima consulta (reload do iframe) sem bloquear a resposta.
Se o driver morreu, uma nova instância é criada em background.

Uso:
    pool = DriverPool(size=3, headless=True)
    pool.initialize()   # pré-aquece todos os drivers (paralelo)

    with pool.acquire(timeout=300) as lease:
        result = query_plate(lease.driver, placa)

    pool.shutdown()     # fecha tudo (chamado no shutdown da API)
"""

import logging
import os
import queue
import subprocess
import threading
import time as _time
from typing import Optional

from selenium.common.exceptions import TimeoutException as SeleniumTimeoutException

from config import POOL_BUSY_STALE_S, POOL_RECOVERY_STUCK_S, POOL_WATCHDOG_INTERVAL_S

from tokio_automation import (
    build_driver,
    go_to_nova_cotacao,
    is_session_alive,
    login,
    navigate_to_nova_cotacao_fast,
    reload_cotador_iframe,
    try_reuse_form,
)

logger = logging.getLogger("driver_pool")


class DriverPool:
    """Pool thread-safe de instâncias Chrome."""

    def __init__(self, size: int = 3, headless: bool = True) -> None:
        self.size = size
        self.headless = headless
        self._pool: queue.Queue = queue.Queue(maxsize=size)
        self._shutdown = False
        self._active_count = 0
        self._count_lock = threading.Lock()
        self._acquire_cv = threading.Condition()
        self._workers_lock = threading.Lock()
        self._recovery_lock = threading.Lock()
        self._recovering_workers: set[int] = set()
        now = _time.time()
        self._workers: dict[int, dict] = {
            worker_id: {
                "worker_id": worker_id,
                "driver": None,
                "status": "starting",
                "phase": "Aguardando inicialização",
                "status_since": now,
                "phase_since": now,
                "updated_at": now,
                "req_id": None,
                "placa": None,
                "attempt": None,
                "mode": None,
                "last_error": None,
                "force_stop_requested": False,
            }
            for worker_id in range(1, size + 1)
        }
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    # ------------------------------------------------------------------
    # Helpers de estado
    # ------------------------------------------------------------------

    def _update_worker(self, worker_id: int, **changes) -> None:
        now = _time.time()
        with self._workers_lock:
            worker = self._workers[worker_id]
            if "status" in changes and changes["status"] != worker.get("status"):
                worker["status_since"] = now
            if "phase" in changes and changes["phase"] != worker.get("phase"):
                worker["phase_since"] = now
            worker.update(changes)
            worker["updated_at"] = now

    def _worker_driver(self, worker_id: int):
        with self._workers_lock:
            return self._workers[worker_id].get("driver")

    def set_worker_phase(self, worker_id: int, phase: str, **extra) -> None:
        payload = {"phase": phase}
        payload.update(extra)
        self._update_worker(worker_id, **payload)

    def assign_request(
        self,
        worker_id: int,
        *,
        req_id: Optional[str] = None,
        placa: Optional[str] = None,
        attempt: Optional[int] = None,
        mode: Optional[str] = None,
    ) -> None:
        self._update_worker(
            worker_id,
            req_id=req_id,
            placa=placa,
            attempt=attempt,
            mode=mode,
            status="busy",
            force_stop_requested=False,
        )

    def snapshot_workers(self) -> list[dict]:
        now = _time.time()
        with self._workers_lock:
            rows = []
            for worker_id in sorted(self._workers):
                item = dict(self._workers[worker_id])
                item.pop("driver", None)
                item["busy_for_s"] = round(now - item.get("status_since", now), 1)
                item["phase_for_s"] = round(now - item.get("phase_since", now), 1)
                rows.append(item)
        return rows

    def _remove_from_available_queue(self, worker_id: int) -> bool:
        with self._pool.mutex:
            try:
                self._pool.queue.remove(worker_id)
                return True
            except ValueError:
                return False

    def _enqueue_worker(self, worker_id: int) -> None:
        self._pool.put(worker_id)
        with self._acquire_cv:
            self._acquire_cv.notify_all()

    def _is_force_stop_requested(self, worker_id: int) -> bool:
        with self._workers_lock:
            return bool(self._workers[worker_id].get("force_stop_requested"))

    def _begin_recovery(self, worker_id: int) -> bool:
        with self._recovery_lock:
            if worker_id in self._recovering_workers:
                return False
            self._recovering_workers.add(worker_id)
            return True

    def _finish_recovery(self, worker_id: int) -> None:
        with self._recovery_lock:
            self._recovering_workers.discard(worker_id)

    def _schedule_recovery(self, worker_id: int, force_recreate: bool = False) -> bool:
        if self._shutdown:
            return False
        if not self._begin_recovery(worker_id):
            return False
        threading.Thread(
            target=self._recover_worker,
            args=(worker_id, force_recreate),
            daemon=True,
        ).start()
        return True

    def _watchdog_loop(self) -> None:
        while not self._shutdown:
            try:
                now = _time.time()
                with self._workers_lock:
                    workers = [dict(item) for item in self._workers.values()]

                for worker in workers:
                    worker_id = worker["worker_id"]
                    status = worker.get("status")
                    status_since = worker.get("status_since", now)
                    updated_at = worker.get("updated_at", status_since)

                    if status in {"error", "recovering", "stopping", "starting"} and now - status_since >= POOL_RECOVERY_STUCK_S:
                        logger.warning(f"[pool] Watchdog: reciclando W{worker_id} preso em '{status}'.")
                        self._update_worker(
                            worker_id,
                            status="recovering",
                            phase="Watchdog: reciclando worker preso",
                            force_stop_requested=True,
                        )
                        driver = self._worker_driver(worker_id)
                        if driver is not None:
                            self._safe_quit(driver)
                            self._update_worker(worker_id, driver=None)
                        self._schedule_recovery(worker_id, True)
                        continue

                    if status == "busy" and now - updated_at >= POOL_BUSY_STALE_S:
                        logger.warning(f"[pool] Watchdog: W{worker_id} sem progresso há {now - updated_at:.1f}s; forçando reciclagem.")
                        self._update_worker(
                            worker_id,
                            status="stopping",
                            phase="Watchdog: interrompendo worker sem progresso",
                            force_stop_requested=True,
                            last_error="Watchdog reiniciou worker sem progresso recente.",
                        )
                        driver = self._worker_driver(worker_id)
                        if driver is not None:
                            self._safe_quit(driver)
                            self._update_worker(worker_id, driver=None)
                        self._schedule_recovery(worker_id, True)
            except Exception as e:
                logger.exception(f"[pool] Watchdog falhou ao inspecionar workers: {e}")

            _time.sleep(POOL_WATCHDOG_INTERVAL_S)

    # ------------------------------------------------------------------
    # Inicialização
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Cria todos os drivers em paralelo. Deve ser chamado na startup da API."""
        threads = []
        for i in range(self.size):
            t = threading.Thread(target=self._init_one, args=(i,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        ready = self._pool.qsize()
        logger.info(f"[pool] {ready}/{self.size} drivers prontos.")
        if ready == 0:
            raise RuntimeError(
                "Nenhum driver inicializou com sucesso. "
                "Verifique Chrome e chromedriver."
            )

    def _init_one(self, idx: int) -> None:
        worker_id = idx + 1
        try:
            self._update_worker(worker_id, status="starting", phase="Inicializando Chrome", last_error=None)
            logger.info(f"[pool] Inicializando driver #{worker_id}...")
            t0 = _time.time()
            driver = self._create_driver()
            self._update_worker(
                worker_id,
                driver=driver,
                status="idle",
                phase="Pronto para uso",
                req_id=None,
                placa=None,
                attempt=None,
                mode=None,
                last_error=None,
                force_stop_requested=False,
            )
            self._enqueue_worker(worker_id)
            with self._count_lock:
                self._active_count += 1
            logger.info(f"[pool] Driver #{worker_id} pronto em {_time.time() - t0:.1f}s.")
        except Exception as e:
            self._update_worker(worker_id, status="error", phase="Falha na inicialização", last_error=str(e))
            logger.error(f"[pool] Falha ao inicializar driver #{worker_id}: {e}")

    # ------------------------------------------------------------------
    # Criação e destruição de driver
    # ------------------------------------------------------------------

    def _create_driver(self):
        """Cria driver, faz login e navega para nova-cotação."""
        driver = build_driver(headless=self.headless)
        login(driver)
        go_to_nova_cotacao(driver)
        return driver

    def _safe_quit(self, driver) -> None:
        proc = getattr(getattr(driver, "service", None), "process", None)
        try:
            driver.quit()
        except Exception:
            pass
        try:
            if proc:
                try:
                    if os.name != "nt":
                        subprocess.run(["pkill", "-TERM", "-P", str(proc.pid)], capture_output=True, timeout=3)
                except Exception:
                    pass
                if proc.poll() is None:
                    proc.kill()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Checkout / checkin
    # ------------------------------------------------------------------

    def acquire(self, timeout: float = 300, min_available: int = 0) -> "DriverContext":
        """Retorna um DriverContext. Pode reservar `min_available` drivers para tráfego prioritário."""
        deadline = _time.time() + timeout
        with self._acquire_cv:
            while True:
                if self._shutdown:
                    raise RuntimeError("Pool está em processo de shutdown.")
                if self._pool.qsize() > min_available:
                    try:
                        worker_id = self._pool.get_nowait()
                        self._update_worker(
                            worker_id,
                            status="busy",
                            phase="Driver alocado",
                            last_error=None,
                            force_stop_requested=False,
                        )
                        return DriverContext(self, worker_id)
                    except queue.Empty:
                        pass
                remaining = deadline - _time.time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Nenhum driver disponível após {timeout}s — pool lotado."
                    )
                self._acquire_cv.wait(timeout=remaining)

    def _recover_worker(self, worker_id: int, force_recreate: bool = False) -> None:
        try:
            driver = self._worker_driver(worker_id)
            if driver is None:
                force_recreate = True

            self._update_worker(
                worker_id,
                status="recovering",
                phase="Recriando worker" if force_recreate else "Preparando próxima consulta",
            )

            alive = False
            if driver is not None and not force_recreate:
                try:
                    alive = is_session_alive(driver)
                except Exception:
                    pass

            if alive:
                try:
                    if try_reuse_form(driver):
                        self._update_worker(worker_id, phase="Recarregando iframe")
                        reload_cotador_iframe(driver)
                    else:
                        self._update_worker(worker_id, phase="Navegando para Nova Cotação")
                        navigate_to_nova_cotacao_fast(driver)
                    self._update_worker(
                        worker_id,
                        status="idle",
                        phase="Pronto para uso",
                        req_id=None,
                        placa=None,
                        attempt=None,
                        mode=None,
                        last_error=None,
                        force_stop_requested=False,
                    )
                    self._enqueue_worker(worker_id)
                    return
                except Exception as e:
                    logger.warning(f"[pool] Erro ao preparar driver W{worker_id} no checkin: {e}")
                    self._update_worker(worker_id, phase="Recriando após falha no preparo", last_error=str(e))

            if driver is not None:
                self._safe_quit(driver)

            logger.info(f"[pool] Recriando driver morto W{worker_id}...")
            self._update_worker(worker_id, driver=None, status="recovering", phase="Inicializando novo Chrome")
            new_driver = self._create_driver()
            self._update_worker(
                worker_id,
                driver=new_driver,
                status="idle",
                phase="Pronto para uso",
                req_id=None,
                placa=None,
                attempt=None,
                mode=None,
                last_error=None,
                force_stop_requested=False,
            )
            self._enqueue_worker(worker_id)
            logger.info(f"[pool] Driver W{worker_id} recuperado e devolvido ao pool.")
        except Exception as e:
            logger.error(f"[pool] Falha ao recriar driver W{worker_id}: {e}")
            self._update_worker(worker_id, driver=None, status="error", phase="Falha ao recriar worker", last_error=str(e))
            with self._count_lock:
                self._active_count = max(0, self._active_count - 1)
        finally:
            self._finish_recovery(worker_id)

    def _checkin(self, worker_id: int, force_recreate: bool = False) -> None:
        """Devolve o driver ao pool em background, recriando se necessário."""
        if self._shutdown:
            driver = self._worker_driver(worker_id)
            if driver is not None:
                self._safe_quit(driver)
            self._update_worker(worker_id, driver=None, status="stopped", phase="Pool encerrado")
            return

        self._schedule_recovery(worker_id, force_recreate)

    def force_stop_worker(self, worker_id: int) -> dict:
        if worker_id not in self._workers:
            raise ValueError(f"Worker {worker_id} não existe.")

        driver = self._worker_driver(worker_id)
        self._update_worker(
            worker_id,
            status="stopping",
            phase="Parada forçada solicitada",
            force_stop_requested=True,
        )

        if self._remove_from_available_queue(worker_id):
            if driver is not None:
                self._safe_quit(driver)
                self._update_worker(worker_id, driver=None)
            self._schedule_recovery(worker_id, True)
            return {
                "ok": True,
                "worker_id": worker_id,
                "message": f"Worker W{worker_id} parado e recriação iniciada.",
            }

        if driver is not None:
            self._safe_quit(driver)
        return {
            "ok": True,
            "worker_id": worker_id,
            "message": f"Parada forçada enviada para W{worker_id}.",
        }

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def available(self) -> int:
        """Número de drivers disponíveis no momento."""
        return self._pool.qsize()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Fecha todos os drivers do pool."""
        self._shutdown = True
        with self._acquire_cv:
            self._acquire_cv.notify_all()
        while not self._pool.empty():
            try:
                worker_id = self._pool.get_nowait()
                driver = self._worker_driver(worker_id)
                if driver is not None:
                    self._safe_quit(driver)
                self._update_worker(worker_id, driver=None, status="stopped", phase="Pool encerrado")
            except Exception:
                pass
        logger.info("[pool] Pool encerrado.")


class DriverContext:
    """Context manager — garante checkin automático ao sair do bloco `with`."""

    def __init__(self, pool: DriverPool, worker_id: int) -> None:
        self._pool = pool
        self.worker_id = worker_id

    @property
    def driver(self):
        return self._pool._worker_driver(self.worker_id)

    def set_phase(self, phase: str, **extra) -> None:
        self._pool.set_worker_phase(self.worker_id, phase, **extra)

    def assign_request(
        self,
        *,
        req_id: Optional[str] = None,
        placa: Optional[str] = None,
        attempt: Optional[int] = None,
        mode: Optional[str] = None,
    ) -> None:
        self._pool.assign_request(
            self.worker_id,
            req_id=req_id,
            placa=placa,
            attempt=attempt,
            mode=mode,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        force_recreate = bool(
            (exc_type and issubclass(exc_type, (SeleniumTimeoutException, TimeoutError)))
            or self._pool._is_force_stop_requested(self.worker_id)
        )
        self._pool._checkin(self.worker_id, force_recreate=force_recreate)
        return False
