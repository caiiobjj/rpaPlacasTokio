"""
Pool de drivers Chrome para execução paralela de consultas RPA.

Cada requisição faz checkout de um driver livre do pool, executa a consulta
e faz checkin. O checkin é assíncrono: verifica se o driver está vivo e o
prepara para a próxima consulta (reload do iframe) sem bloquear a resposta.
Se o driver morreu, uma nova instância é criada em background.

Uso:
    pool = DriverPool(size=3, headless=True)
    pool.initialize()   # pré-aquece todos os drivers (paralelo)

    with pool.acquire(timeout=300) as driver:
        result = query_plate(driver, placa)

    pool.shutdown()     # fecha tudo (chamado no shutdown da API)
"""

import queue
import subprocess
import threading
import logging
import time as _time
from typing import Optional

from tokio_automation import (
    build_driver,
    login,
    go_to_nova_cotacao,
    is_session_alive,
    try_reuse_form,
    reload_cotador_iframe,
)

logger = logging.getLogger("driver_pool")


class DriverPool:
    """Pool thread-safe de instâncias Chrome.

    Atributos:
        size      -- número máximo de drivers simultâneos
        headless  -- modo headless do Chrome
    """

    def __init__(self, size: int = 3, headless: bool = True) -> None:
        self.size = size
        self.headless = headless
        self._pool: queue.Queue = queue.Queue(maxsize=size)
        self._shutdown = False
        self._active_count = 0
        self._count_lock = threading.Lock()

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
        try:
            logger.info(f"[pool] Inicializando driver #{idx + 1}...")
            t0 = _time.time()
            driver = self._create_driver()
            self._pool.put(driver)
            with self._count_lock:
                self._active_count += 1
            logger.info(f"[pool] Driver #{idx + 1} pronto em {_time.time() - t0:.1f}s.")
        except Exception as e:
            logger.error(f"[pool] Falha ao inicializar driver #{idx + 1}: {e}")

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
        try:
            driver.quit()
        except Exception:
            pass
        try:
            subprocess.run(
                ["pkill", "-f", "google-chrome"],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Checkout / checkin
    # ------------------------------------------------------------------

    def acquire(self, timeout: float = 300) -> "DriverContext":
        """Retorna um DriverContext. Bloqueia até `timeout`s se pool ocupado."""
        if self._shutdown:
            raise RuntimeError("Pool está em processo de shutdown.")
        try:
            driver = self._pool.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(
                f"Nenhum driver disponível após {timeout}s — pool lotado."
            )
        return DriverContext(self, driver)

    def _checkin(self, driver) -> None:
        """Devolve o driver ao pool em background, recriando se necessário."""
        if self._shutdown:
            self._safe_quit(driver)
            return

        def _recover():
            alive = False
            try:
                alive = is_session_alive(driver)
            except Exception:
                pass

            if alive:
                # Prepara para próxima consulta
                try:
                    if try_reuse_form(driver):
                        reload_cotador_iframe(driver)
                    else:
                        go_to_nova_cotacao(driver)
                    self._pool.put(driver)
                    return
                except Exception as e:
                    logger.warning(f"[pool] Erro ao preparar driver no checkin: {e}")

            # Driver morto ou preparação falhou — recria
            self._safe_quit(driver)
            try:
                logger.info("[pool] Recriando driver morto...")
                new_driver = self._create_driver()
                self._pool.put(new_driver)
                logger.info("[pool] Driver recuperado e devolvido ao pool.")
            except Exception as e:
                logger.error(f"[pool] Falha ao recriar driver: {e}")
                with self._count_lock:
                    self._active_count -= 1

        threading.Thread(target=_recover, daemon=True).start()

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
        while not self._pool.empty():
            try:
                driver = self._pool.get_nowait()
                self._safe_quit(driver)
            except Exception:
                pass
        logger.info("[pool] Pool encerrado.")


class DriverContext:
    """Context manager — garante checkin automático ao sair do bloco `with`."""

    def __init__(self, pool: DriverPool, driver) -> None:
        self._pool = pool
        self.driver = driver

    def __enter__(self):
        return self.driver

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._pool._checkin(self.driver)
        return False  # não suprime exceções
