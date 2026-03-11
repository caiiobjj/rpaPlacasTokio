import logging
import time
from typing import Callable, Dict, Optional

from selenium import webdriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException, ElementClickInterceptedException, ElementNotInteractableException
import shutil
import requests
import re

from config import (
    get_credentials, get_urls, get_headless, nova_cotacao_url, login_url_with_goto,
    TIMEOUT_DRIVER, TIMEOUT_MODAL, TIMEOUT_IFRAME, TIMEOUT_PAGE,
)


logger = logging.getLogger("tokio_automation")


class PlacaNaoEncontradaError(ValueError):
    """Levantada quando o portal exibe 'Placa não localizada' — não adianta retentar."""
    pass


class DadosVaziosError(ValueError):
    """Todos os campos do veículo vieram vazios — veículo não cadastrado no sistema Tokio Marine."""
    pass


def _check_placa_nao_encontrada(driver: webdriver.Chrome) -> bool:
    """Verifica se o alerta 'Placa não localizada' está visível na página (dentro do iframe corrente)."""
    try:
        # Texto exato do portal Tokio Marine
        alerts = driver.find_elements(
            By.XPATH,
            "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
            "'placa não localizada')]"
        )
        for a in alerts:
            try:
                if a.is_displayed():
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def build_driver(headless: Optional[bool] = None) -> webdriver.Chrome:
    if headless is None:
        headless = get_headless()

    def _make(headless_flag: bool) -> webdriver.Chrome:
        options = webdriver.ChromeOptions()
        options.page_load_strategy = 'eager'
        if headless_flag:
            options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-setuid-sandbox')       # necessário no Linux
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-software-rasterizer')
        options.add_argument('--disable-extensions')
        options.add_argument('--disable-features=NetworkService')
        options.add_argument('--window-size=1600,1200')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--lang=pt-BR')
        options.add_argument('--remote-allow-origins=*')
        options.add_argument('--disable-blink-features=AutomationControlled')
        # Redução de consumo de memória por instância
        options.add_argument('--disable-background-networking')
        options.add_argument('--disable-sync')
        options.add_argument('--disable-translate')
        options.add_argument('--disable-client-side-phishing-detection')
        options.add_argument('--disable-hang-monitor')
        options.add_argument('--disable-renderer-backgrounding')
        options.add_argument('--disable-backgrounding-occluded-windows')
        options.add_argument('--disable-background-timer-throttling')
        options.add_argument('--disable-popup-blocking')
        options.add_argument('--no-first-run')
        options.add_argument('--metrics-recording-only')
        options.add_argument('--js-flags=--max-old-space-size=512')  # evita travas do renderer sob carga
        options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_experimental_option('prefs', {
            'profile.managed_default_content_settings.images': 2,
            'profile.default_content_setting_values.notifications': 2,
            'profile.default_content_setting_values.geolocation': 2,
        })
        # Usa chromedriver do sistema (instalado em /usr/local/bin) ou deixa Selenium Manager encontrar
        chromedriver_path = shutil.which('chromedriver') or '/usr/local/bin/chromedriver'
        try:
            service = ChromeService(chromedriver_path)
            driver = webdriver.Chrome(service=service, options=options)
        except Exception:
            # Fallback: Selenium Manager sem especificar service
            driver = webdriver.Chrome(options=options)

        try:
            driver.set_page_load_timeout(TIMEOUT_PAGE)
        except Exception:
            pass
        return driver

    # Tenta criar em headless; se falhar, tenta com janela normal
    try:
        return _make(headless)
    except Exception:
        if headless:
            return _make(False)
        raise


def _focus_element(driver: webdriver.Chrome, element: WebElement) -> None:
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'center'});"
        "arguments[0].focus();",
        element,
    )


def _type_into_field(driver: webdriver.Chrome, selector: str, value: str, timeout: int = 15) -> None:
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            element = WebDriverWait(driver, timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, selector))
            )
            WebDriverWait(driver, timeout).until(lambda d: element.is_displayed() and element.is_enabled())
            _focus_element(driver, element)
            try:
                element.click()
            except Exception:
                driver.execute_script("arguments[0].click();", element)

            try:
                element.clear()
            except Exception:
                driver.execute_script("arguments[0].value = '';", element)

            element.send_keys(value)
            current_value = (element.get_attribute('value') or '').strip()
            if current_value != value:
                driver.execute_script(
                    "arguments[0].value = arguments[1];"
                    "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
                    "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
                    element,
                    value,
                )
                current_value = (element.get_attribute('value') or '').strip()

            if current_value == value:
                return

            raise ElementNotInteractableException(f"Campo {selector} não confirmou o valor informado.")
        except (TimeoutException, StaleElementReferenceException, ElementClickInterceptedException, ElementNotInteractableException) as exc:
            last_error = exc
            time.sleep(0.4 * (attempt + 1))

    if last_error:
        raise last_error
    raise TimeoutException(f"Campo {selector} não ficou pronto para digitação.")


def _safe_click(driver: webdriver.Chrome, selector: str, timeout: int = 15) -> None:
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            element = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            _focus_element(driver, element)
            WebDriverWait(driver, timeout).until(lambda d: element.is_displayed() and element.is_enabled())
            try:
                WebDriverWait(driver, timeout).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                ).click()
            except Exception:
                driver.execute_script("arguments[0].click();", element)
            return
        except (TimeoutException, StaleElementReferenceException, ElementClickInterceptedException, ElementNotInteractableException) as exc:
            last_error = exc
            time.sleep(0.4 * (attempt + 1))

    if last_error:
        raise last_error
    raise TimeoutException(f"Elemento {selector} não ficou clicável.")


def login(driver: webdriver.Chrome, timeout: int = 60) -> None:
    username, password = get_credentials()
    login_url, portal_url = get_urls()

    wait = WebDriverWait(driver, timeout)
    driver.get(login_url)

    # Campos padrão do OpenAM XUI
    wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '#idToken1')))
    _type_into_field(driver, '#idToken1', username, timeout=min(timeout, 20))
    _type_into_field(driver, '#idToken2', password, timeout=min(timeout, 20))

    # Entrar
    _safe_click(driver, '#loginButton_0', timeout=min(timeout, 20))

    # Não depender do redirecionamento automático do OpenAM
    # Apenas aguarda uma transição curta (perfil/portal/login) e segue adiante
    try:
        wait_short = WebDriverWait(driver, 5)
        wait_short.until(EC.any_of(
            EC.url_contains('portalparceiros.tokiomarine.com.br'),
            EC.url_contains('/profile'),
            EC.url_contains('openam')
        ))
    except TimeoutException:
        pass


def is_session_alive(driver: webdriver.Chrome) -> bool:
    """Verifica se o driver ainda está ativo e a sessão no portal ainda é válida."""
    try:
        # Coleta URL + title num único round-trip JS
        result = driver.execute_script(
            "return {url: document.location.href, title: document.title};"
        ) or {}
        url   = result.get('url',   '') or ''
        title = result.get('title', '') or ''
        if 'ssoportais3.tokiomarine.com.br' in url:
            return False
        if not url or url in ('about:blank', 'data:,'):
            return False
        if 'tokiomarine.com.br' not in url:
            return False
        if not title.strip():
            return False
        return True
    except Exception:
        return False


def navigate_to_nova_cotacao_fast(driver: webdriver.Chrome) -> None:
    """
    Navega para Nova Cotação de forma rápida, assumindo que a sessão SSO do
    Liferay já está estabelecida (cookie ativo). Pula o passo do portal-corretor.

    Use após a primeira chamada bem-sucedida de go_to_nova_cotacao().
    """
    _, portal_url = get_urls()
    base = portal_url.rstrip('/')
    target = nova_cotacao_url()
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    # Apaga cookies do Liferay para forçar redirecionamento via OpenAM
    # (sem isso, driver.get(portal-corretor) carrega direto do Liferay, mesmo domínio,
    # e o Angular redireciona nova-cotacao → #/brokertech em vez de carregá-la)
    try:
        all_cookies = driver.get_cookies()
        for cookie in all_cookies:
            if 'portalparceiros' in cookie.get('domain', ''):
                try:
                    driver.delete_cookie(cookie['name'])
                except Exception:
                    pass
    except Exception:
        pass
    # Navega portal-corretor → OpenAM (cross-domain) → nova-cotacao (full Angular reload)
    driver.get(base + '/group/portal-corretor')
    try:
        WebDriverWait(driver, TIMEOUT_PAGE).until(lambda d: d.current_url != 'about:blank')
    except TimeoutException:
        pass
    driver.get(target)
    try:
        WebDriverWait(driver, TIMEOUT_IFRAME).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'iframe[src*="CotadorAutoService"]'))
        )
    except TimeoutException:
        pass  # iframe pode ainda não existir; query_plate tentará de novo
    try:
        driver.switch_to.default_content()
    except Exception:
        pass


def go_to_nova_cotacao(driver: webdriver.Chrome, timeout: int = 60) -> None:
    """
    Navega para a tela de Nova Cotação e aguarda o Angular carregar.

    Retorna com o driver no contexto padrão (default_content).
    O iframe do CotadorAutoService será acessado por query_plate() quando necessário.
    """
    _, portal_url = get_urls()
    base = portal_url.rstrip('/')
    target = nova_cotacao_url()

    # Passo 1: acessa portal-corretor para estabelecer sessão SSO do Liferay
    driver.get(base + '/group/portal-corretor')
    try:
        WebDriverWait(driver, TIMEOUT_PAGE).until(lambda d: d.current_url != 'about:blank')
    except TimeoutException:
        pass

    # Passo 2: navega para nova-cotação (cross-domain a partir do OpenAM = full Angular reload)
    driver.get(target)

    # Passo 3: aguarda Angular + CotadorAutoService iframe carregarem
    try:
        WebDriverWait(driver, TIMEOUT_IFRAME).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'iframe[src*="CotadorAutoService"]'))
        )
    except TimeoutException:
        pass  # iframe pode ainda não existir; query_plate tentará de novo
    # Retorna ao contexto padrão para que query_plate() possa encontrar os iframes
    try:
        driver.switch_to.default_content()
    except Exception:
        pass


def _find_plate_input_in_context(driver: webdriver.Chrome):
    """Localiza o campo de Placa no contexto atual do driver (frame já selecionado)."""
    # 1) CSS class direta, confirmada via DevTools: class="form-control input-sm placa"
    els = driver.find_elements(By.CSS_SELECTOR, 'input.placa')
    if els:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", els[0])
        except Exception:
            pass
        return els[0]

    # 2) XPath por atributos (fallback)
    xpaths = [
        "//input[contains(translate(@placeholder,'PLACA','placa'),'placa')]",
        "//input[contains(translate(@name,'PLACA','placa'),'placa')]",
        "//input[contains(translate(@id,'PLACA','placa'),'placa')]",
        "//input[contains(translate(@formcontrolname,'PLACA','placa'),'placa')]",
        "//input[contains(translate(@aria-label,'PLACA','placa'),'placa')]",
        # por rótulo em containers próximos
        "//label[contains(normalize-space(),'Placa')]/following::input[1]",
        "(//div[.//label[contains(normalize-space(),'Placa')]]//input[not(@type) or @type='text'])[1]",
        "//div[contains(@class,'form-group') or contains(@class,'campo')][.//label[contains(normalize-space(),'Placa')]]//input[1]",
    ]
    for xp in xpaths:
        els = driver.find_elements(By.XPATH, xp)
        if els:
            el = els[0]
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
            except Exception:
                pass
            return el
    return None


def _find_plate_input(driver: webdriver.Chrome):
    # Primeiro, tenta no contexto principal
    el = _find_plate_input_in_context(driver)
    if el:
        return el
    # Se não achou, tenta percorrer iframes
    driver.switch_to.default_content()
    frames = driver.find_elements(By.TAG_NAME, 'iframe')
    for idx, fr in enumerate(frames):
        try:
            driver.switch_to.frame(fr)
            el = _find_plate_input_in_context(driver)
            if el:
                # Mantém o driver dentro do iframe onde o elemento foi encontrado
                return el
        except Exception:
            pass
        # Volta para o topo somente se não encontrou no frame atual
        driver.switch_to.default_content()
    raise NoSuchElementException('Campo de Placa não encontrado (nem em iframes)')


def _click_pesquisar_if_present(driver: webdriver.Chrome, context_el=None):
    # Procura botão Pesquisar próximo ao contexto primeiro
    if context_el is not None:
        try:
            container = context_el.find_element(By.XPATH, "ancestor::div[1]")
            local_hits = container.find_elements(
                By.XPATH,
                (
                    
                    ".//button[contains(translate(.,'PESQUISAR','pesquisar'),'pesquisar')] | "
                    ".//a[contains(@class,'btn-pesquisar') or contains(translate(.,'PESQUISAR','pesquisar'),'pesquisar')] | "
                    ".//i[contains(@class,'search') or contains(@class,'fa-search')]/ancestor::button[1]"
                ),
            )
            if local_hits:
                try:
                    local_hits[0].click()
                    return
                except Exception:
                    pass
        except Exception:
            pass

    # Fallback global — usa o seletor confirmado pelo DevTools primeiro
    css_candidates = driver.find_elements(By.CSS_SELECTOR, 'button.btn-pesquisar-veiculos')
    if not css_candidates:
        css_candidates = driver.find_elements(By.CSS_SELECTOR, 'a.btn-pesquisar-cotacao')
    if css_candidates:
        try:
            css_candidates[0].click()
            return
        except Exception:
            pass

    candidates = driver.find_elements(
        By.XPATH,
        (
            "//button//*[contains(.,'Pesquisar')]/ancestor::button | "
            "//button[contains(.,'Pesquisar')] | "
            "//a[contains(@class,'btn-pesquisar')] | "
            "//label[contains(normalize-space(),'Placa')]/following::*[(self::button or self::a) and (contains(@class,'search') or contains(@class,'lupa') or contains(.,'Pesquisar'))][1] | "
            "//label[contains(normalize-space(),'Placa')]/following::i[contains(@class,'search') or contains(@class,'fa-search')]/ancestor::button[1]"
        ),
    )
    if candidates:
        try:
            candidates[0].click()
        except Exception:
            pass


def _maybe_select_first_vehicle_in_modal(driver: webdriver.Chrome, timeout: int = 20) -> None:
    wait = WebDriverWait(driver, timeout, poll_frequency=0.15)  # detecta modal ~150ms após aparecer
    try:
        # Aguarda modal aparecer (título Lista de Veículos)
        wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//*[contains(.,'Lista de Ve')]")
            )
        )
        # Seleciona a primeira linha — CSS é mais rápido que XPath
        rows = driver.find_elements(
            By.CSS_SELECTOR,
            '.modal[style*="block"] table tbody tr, .modal-body table tbody tr, table tbody tr'
        )
        # Pula linhas vazias (header rows ou separadores)
        real_rows = [r for r in rows if r.text.strip()]
        row = real_rows[0] if real_rows else (rows[0] if rows else None)
        if row:
            # JS click para contornar "element not interactable"
            driver.execute_script("arguments[0].click();", row)
            # Aguarda modal fechar ou formulário atualizar
            try:
                WebDriverWait(driver, TIMEOUT_PAGE, poll_frequency=0.15).until(
                    lambda d: not d.find_elements(
                        By.CSS_SELECTOR,
                        '.modal[style*="block"]'
                    )
                )
            except TimeoutException:
                pass
            # Fecha modal se ainda aberto
            close_btns = driver.find_elements(By.XPATH, "//button[contains(.,'Fechar')]")
            if close_btns:
                try:
                    driver.execute_script("arguments[0].click();", close_btns[0])
                except Exception:
                    pass
    except TimeoutException:
        # nenhum modal – segue o fluxo normal
        return


def _read_value(driver: webdriver.Chrome, label: str) -> Optional[str]:
    # Procura input/select associado ao rótulo
    xps = [
        f"//label[contains(normalize-space(), '{label}')]/following::input[1]",
        f"//label[contains(normalize-space(), '{label}')]/following::select[1]",
        f"//div[.//label[contains(normalize-space(), '{label}')]]//input",
        f"//div[.//label[contains(normalize-space(), '{label}')]]//select",
    ]
    el = None
    for xp in xps:
        els = driver.find_elements(By.XPATH, xp)
        if els:
            el = els[0]
            break
    if el is None:
        return None
    tag = el.tag_name.lower()
    if tag == 'input':
        return el.get_attribute('value') or ''
    if tag == 'select':
        try:
            return el.find_element(By.XPATH, "./option[@selected]").text
        except NoSuchElementException:
            return el.get_attribute('value') or ''
    return None


def _find_cotador_iframe(driver: webdriver.Chrome):
    """Encontra o iframe do CotadorAutoService via JS (1 round-trip vs N+1)."""
    try:
        el = driver.execute_script(
            "return document.querySelector('iframe[src*=\"CotadorAutoService\"]') || null;"
        )
        if el is not None:
            return el
    except Exception:
        pass
    # Fallback síncrono caso JS não retorne WebElement
    iframes = driver.find_elements(By.TAG_NAME, 'iframe')
    for f in iframes:
        if 'CotadorAutoService' in (f.get_attribute('src') or ''):
            return f
    return iframes[0] if iframes else None


def try_reuse_form(driver: webdriver.Chrome) -> bool:
    """
    Verifica se o formulário do CotadorAutoService ainda está acessível sem navegar.
    Retorna True se o iframe com input.placa foi encontrado, False caso contrário.
    Deixa o driver no default_content ao retornar.
    """
    try:
        driver.switch_to.default_content()
        cotador = _find_cotador_iframe(driver)  # já otimizado com JS
        if cotador is None:
            return False
        driver.switch_to.frame(cotador)
        # Único execute_script em vez de find_elements + loop
        found = bool(driver.execute_script("return !!document.querySelector('input.placa');"))
        driver.switch_to.default_content()
        return found
    except Exception:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        return False


def reload_cotador_iframe(driver: webdriver.Chrome, timeout: int = 30) -> None:
    """
    Recarrega APENAS o iframe do CotadorAutoService (sem navegar a página inteira).
    Usa o truque iframe.src = iframe.src para forçar um reload isolado.
    Aguarda input.placa reaparecer no iframe recarregado.
    ~3-6 s versus ~10 s do go_to_nova_cotacao completo.
    """
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    # Força reload apenas do iframe CotadorAutoService
    driver.execute_script(
        "var frames = document.querySelectorAll('iframe');"
        "for (var i = 0; i < frames.length; i++) {"
        "  if (frames[i].src && frames[i].src.indexOf('CotadorAutoService') > -1) {"
        "    frames[i].src = frames[i].src; break;"
        "  }"
        "}"
    )
    wait = WebDriverWait(driver, timeout, poll_frequency=0.1)

    def _cotador_ready(drv):
        # JS querySelector evita N get_attribute round-trips por ciclo de poll
        try:
            ok = drv.execute_script(
                "var f=document.querySelector('iframe[src*=\"CotadorAutoService\"]');"
                "if(!f)return false;"
                "try{return !!(f.contentDocument&&f.contentDocument.querySelector('input.placa'));}catch(e){return false;}"
            )
            if ok:
                return True
        except Exception:
            pass
        # Fallback: switch_to.frame (necessário se JS cross-frame bloqueado)
        try:
            for f in drv.find_elements(By.TAG_NAME, 'iframe'):
                if 'CotadorAutoService' in (f.get_attribute('src') or ''):
                    drv.switch_to.frame(f)
                    ok = bool(drv.find_elements(By.CSS_SELECTOR, 'input.placa'))
                    drv.switch_to.default_content()
                    return ok
        except Exception:
            try:
                drv.switch_to.default_content()
            except Exception:
                pass
        return False

    wait.until(_cotador_ready)


_FAST_REQUIRED_FIELDS = ('chassi', 'veiculo', 'valor_base_do_veiculo', 'codigo_fipe')
_FAST_ZERO_VALUES = {'r$ 0,00', '0,00', 'r$0,00', ''}


def _is_fast_result_usable(dados: Optional[Dict[str, Optional[str]]]) -> bool:
    if not isinstance(dados, dict):
        return False
    for campo in _FAST_REQUIRED_FIELDS:
        valor = (dados.get(campo) or '').strip().lower()
        if not valor or valor in _FAST_ZERO_VALUES:
            return False
    return True


def query_plate(
    driver: webdriver.Chrome,
    placa: str,
    timeout: int = 60,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Optional[str]]:
    """
    Preenche a placa no formulário da cotação e retorna os dados do veículo.
    Assume que o driver está no contexto padrão (default_content).
    Localiza o CotadorAutoService iframe pela URL src para robustez.
    """
    from selenium.webdriver.common.keys import Keys
    t_start = time.monotonic()

    def _progress(label: str) -> None:
        if progress_callback:
            try:
                progress_callback(label)
            except Exception:
                pass

    def _remaining_timeout(*, reserve: float = 0.0, minimum: int = 1) -> int:
        remaining = max(minimum, int(timeout - (time.monotonic() - t_start) - reserve))
        return remaining

    wait = WebDriverWait(driver, timeout, poll_frequency=0.15)  # poll 150ms vs 500ms padrão

    placa_txt = (placa or '').strip().upper()
    if not placa_txt:
        raise ValueError('Placa vazia')

    # Caminho rápido: consulta direto na API interna do portal usando cookies da sessão.
    # Se vier consistente, evita toda a interação Angular/iframe.
    try:
        _progress('Consulta rápida pela API interna')
        api_timeout = max(8, min(timeout, 15))
        dados_api = query_plate_via_api(driver, placa_txt, timeout=api_timeout)
        if _is_fast_result_usable(dados_api):
            _progress('Dados obtidos via API interna')
            return dados_api
    except Exception as e:
        logger.debug('Fallback para fluxo UI após falha no caminho direto da API: %s', e)

    # Vai ao contexto principal para buscar o iframe correto
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    # Aguarda o iframe do CotadorAutoService aparecer (por src)
    def _cotador_iframe_present(drv):
        return _find_cotador_iframe(drv) is not None

    try:
        _progress('Aguardando iframe do cotador')
        wait.until(_cotador_iframe_present)
    except TimeoutException:
        _progress('Recuperando sessão do portal')
        # Iframe não apareceu — verifica se a sessão SSO ainda está viva.
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

        session_ok = is_session_alive(driver)
        if not session_ok:
            # Sessão expirada: faz login completo antes de redirecionar
            print('[query_plate] Sessão SSO expirada — refazendo login antes de re-navegar.', flush=True)
            try:
                login(driver, timeout=_remaining_timeout(reserve=5, minimum=5))
            except Exception as login_err:
                print(f'[query_plate] Falha no re-login: {login_err}', flush=True)
        else:
            print('[query_plate] Sessão aparenta estar viva, mas iframe não carregou — re-navegando.', flush=True)

        go_to_nova_cotacao(driver, timeout=_remaining_timeout(reserve=2, minimum=5))
        try:
            wait2 = WebDriverWait(driver, _remaining_timeout(minimum=5), poll_frequency=0.15)
            wait2.until(_cotador_iframe_present)
        except TimeoutException:
            raise TimeoutException(
                'CotadorAutoService iframe não apareceu após re-navegação'
                + (' com re-login' if not session_ok else '')
                + '. Portal pode estar indisponível.'
            )

    cotador_frame = _find_cotador_iframe(driver)
    if cotador_frame is None:
        raise RuntimeError('CotadorAutoService iframe não encontrado')
    driver.switch_to.frame(cotador_frame)
    _progress('Formulário carregado')
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input.placa')))

    def _get_plate_input():
        """Busca o input.placa novamente — necessário quando Angular re-renderiza o elemento."""
        return driver.find_element(By.CSS_SELECTOR, 'input.placa')

    plate_input = _get_plate_input()

    def _retry_stale(fn, retries=3):
        """Executa fn(); se StaleElementReferenceException, recria plate_input e tenta de novo."""
        nonlocal plate_input
        for i in range(retries):
            try:
                return fn()
            except StaleElementReferenceException:
                if i < retries - 1:
                    time.sleep(0.3)
                    plate_input = _get_plate_input()
                else:
                    raise

    # Prepara campo em 1 round-trip: scroll + unlock + clear + focus + dispara eventos
    try:
        _progress('Preenchendo placa')
        _retry_stale(lambda: driver.execute_script(
            "var el=arguments[0];"
            "el.scrollIntoView({block:'center'});"
            "el.removeAttribute('readonly');"
            "el.removeAttribute('disabled');"
            "el.value='';"
            "el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true}));"
            "el.focus();",
            plate_input
        ))
    except Exception:
        pass

    # Digita via send_keys para eventos nativos do navegador (Angular detecta teclado)
    try:
        _retry_stale(lambda: plate_input.send_keys(placa_txt))
    except Exception:
        pass

    # Confirma valor e dispara eventos Angular — 1 round-trip
    try:
        _retry_stale(lambda: driver.execute_script(
            "var el=arguments[0],v=arguments[1];"
            "if(el.value!==v){el.value=v;}"
            "el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true}));",
            plate_input, placa_txt
        ))
    except Exception:
        pass

    # Passo 1: TAB dispara o lookup do chassis no backend (botão Pesquisar fica disabled até então)
    try:
        _retry_stale(lambda: plate_input.send_keys(Keys.TAB))
    except Exception:
        # Fallback: ActionChains com TAB
        from selenium.webdriver.common.action_chains import ActionChains
        try:
            ActionChains(driver).move_to_element(plate_input).send_keys(Keys.TAB).perform()
        except Exception:
            pass
    # Aguarda o botão Pesquisar ser habilitado (backend preenche chassi em background)
    try:
        WebDriverWait(driver, TIMEOUT_MODAL, poll_frequency=0.15).until(
            lambda d: d.execute_script(
                "return !!(document.querySelector('button.btn-pesquisar-veiculos:not([disabled])')"
                "|| document.querySelector('a.btn-pesquisar-cotacao'));"
            )
        )
    except TimeoutException:
        pass  # tenta clicar mesmo assim

    # Passo 2: clica no botão Pesquisar Veiculos (agora deve estar habilitado)
    # Usa JS click para ignorar qualquer overlay/span que intercepta o clique normal
    _progress('Pesquisando veículo no portal')
    btns = driver.find_elements(By.CSS_SELECTOR, 'button.btn-pesquisar-veiculos')
    if not btns:
        btns = driver.find_elements(By.CSS_SELECTOR, 'a.btn-pesquisar-cotacao')
    if btns:
        try:
            driver.execute_script("arguments[0].click();", btns[0])
        except Exception:
            pass
    else:
        _click_pesquisar_if_present(driver, context_el=plate_input)

    # Verifica alerta de placa não encontrada logo após o Pesquisar
    if _check_placa_nao_encontrada(driver):
        raise PlacaNaoEncontradaError(f"Placa {placa_txt} não localizada no sistema Tokio Marine")

    # Sempre tenta selecionar o veículo da "Lista de Veículos" (modal abre após Pesquisar)
    _progress('Selecionando veículo na lista')
    _maybe_select_first_vehicle_in_modal(driver, timeout=20)
    # Não usa sleep fixo — _vehicle_info_populated abaixo aguarda o formulário atualizar

    # Verifica alerta novamente após tentativa de seleção de veículo
    if _check_placa_nao_encontrada(driver):
        raise PlacaNaoEncontradaError(f"Placa {placa_txt} não localizada no sistema Tokio Marine")

    # Aguarda o campo veiculo/modelo ser preenchido (indica seleção bem-sucedida)
    def _vehicle_info_populated(drv):
        # Único execute_script em vez de 3+ find_elements + get_attribute round-trips
        return drv.execute_script(
            "var sel=document.querySelector('select[name*=\".modelo.\"]');"
            "if(sel){var o=sel.querySelector('option:checked');"
            "if(o&&o.text&&['selecione','-selecione-','','selecionar'].indexOf(o.text.trim().toLowerCase())===-1)return true;}"
            "var c=document.querySelector('input[name*=\"chassi\"]');"
            "return !!(c&&(c.value||'').trim());"
        )

    try:
        _progress('Aguardando dados do veículo')
        wait.until(_vehicle_info_populated)
    except TimeoutException:
        # Confirma se o timeout foi causado por placa não encontrada
        if _check_placa_nao_encontrada(driver):
            raise PlacaNaoEncontradaError(f"Placa {placa_txt} não localizada no sistema Tokio Marine")

    # Última verificação antes de ler os campos
    if _check_placa_nao_encontrada(driver):
        raise PlacaNaoEncontradaError(f"Placa {placa_txt} não localizada no sistema Tokio Marine")

    # Lê todos os campos em 1 round-trip JS em vez de 12+ find_elements/get_attribute
    _SKIP = {'selecione', '-selecione-', '', 'selecionar'}

    def _v(sel):
        """Retorna .value do primeiro input correspondente ou '' se não encontrado."""
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        return (els[0].get_attribute('value') or '').strip() if els else ''

    def _s(sel):
        """Retorna texto do option:checked do primeiro select correspondente, ou ''."""
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        for el in els:
            try:
                t = el.find_element(By.CSS_SELECTOR, 'option:checked').text.strip()
                if t.lower() not in _SKIP:
                    return t
            except Exception:
                pass
        return ''

    try:
        _progress('Lendo dados finais do veículo')
        raw = driver.execute_script(
            "var s=function(sel){"
            "  var e=document.querySelector(sel);"
            "  return e?(e.value||'').trim():null;};"
            "var q=function(sel){"
            "  var els=document.querySelectorAll(sel);"
            "  for(var i=0;i<els.length;i++){"
            "    var o=els[i].querySelector('option:checked');"
            "    if(o&&o.text&&['selecione','-selecione-','','selecionar'].indexOf(o.text.trim().toLowerCase())===-1)"
            "      return o.text.trim();} return null;};"
            "return {"
            "  placa:    s('input[name*=\".placa.\"]')||s('input[name*=\"placa\"]'),"
            "  chassi:   s('input[name*=\".chassi.\"]')||s('input[name*=\"chassi\"]'),"
            "  anoModelo:q('select[name*=\"anoModelo\"]'),"
            "  veiculo:  q('select[name*=\".modelo.\"]')||q('select[name*=\"modelo\"]'),"
            "  valorBase:s('input[name*=\"valorBase\"]'),"
            "  codFIPE:  s('input[name*=\"codFIPE\"]')||s('input[name*=\"FIPE\"]'),"
            "};"
        ) or {}
    except Exception:
        raw = {}

    def _by_name(pattern: str) -> Optional[str]:
        key = pattern.strip('.')
        v = raw.get(key) or raw.get(pattern)
        if v:
            return v
        return _v(f'input[name*="{pattern}"]') or None

    def _by_select_text(pattern: str) -> Optional[str]:
        key = pattern.strip('.')
        v = raw.get(key) or raw.get(pattern)
        if v:
            return v
        return _s(f'select[name*="{pattern}"]') or None

    dados = {
        'placa':                 raw.get('placa')     or _by_name('placa')      or placa,
        'chassi':                raw.get('chassi')    or _by_name('chassi'),
        'ano_modelo':            raw.get('anoModelo') or _read_value(driver, 'Ano modelo'),
        'veiculo':               raw.get('veiculo')   or _read_value(driver, 'Veículo'),
        'valor_base_do_veiculo': raw.get('valorBase') or _read_value(driver, 'Valor base'),
        'codigo_fipe':           raw.get('codFIPE')   or _read_value(driver, 'Código FIPE'),
    }

    # Volta ao contexto principal
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    _progress('Consulta concluída')
    return dados


def _dismiss_cookies_banner(driver: webdriver.Chrome) -> None:
    """Tenta fechar/ocultar o banner de cookies para evitar bloqueio de cliques."""
    phrases = [
        'entendi', 'aceitar', 'aceito', 'ok', 'fechar', 'continuar'
    ]
    xpaths = [
        "//button|//a|//div[@role='button']"
    ]
    try:
        # contexto principal
        for xp in xpaths:
            els = driver.find_elements(By.XPATH, xp)
            for el in els:
                txt = (el.text or el.get_attribute('aria-label') or '').strip().lower()
                if any(p in txt for p in phrases):
                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                    except Exception:
                        pass
                    try:
                        el.click()
                        time.sleep(0.2)
                        return
                    except Exception:
                        continue
        # iframes
        frames = driver.find_elements(By.TAG_NAME, 'iframe')
        for fr in frames:
            try:
                driver.switch_to.frame(fr)
                for xp in xpaths:
                    els = driver.find_elements(By.XPATH, xp)
                    for el in els:
                        txt = (el.text or el.get_attribute('aria-label') or '').strip().lower()
                        if any(p in txt for p in phrases):
                            try:
                                el.click()
                                time.sleep(0.2)
                                driver.switch_to.default_content()
                                return
                            except Exception:
                                continue
            except Exception:
                pass
            finally:
                driver.switch_to.default_content()
    except Exception:
        pass


def _get_calc_id_from_page(driver: webdriver.Chrome) -> Optional[str]:
    cached = getattr(driver, '_tokio_calc_id', None)
    if isinstance(cached, str) and cached.isdigit():
        return cached

    def _search_html(html: str) -> Optional[str]:
        # Padrão no atributo name do input: mapCotacoes{CALC}.dados...
        m = re.search(r'mapCotacoes(\d+)', html)
        if m:
            return m.group(1)
        # Padrão textual "Cálculo: 123"
        m = re.search(r'C[áa]lculo[:\s]+\s*(\d+)', html, flags=re.IGNORECASE)
        if m:
            return m.group(1)
        return None

    # 1) Tenta no contexto atual (pode já estar dentro do iframe)
    try:
        result = _search_html(driver.page_source)
        if result:
            setattr(driver, '_tokio_calc_id', result)
            return result
    except Exception:
        pass

    # 2) Volta ao topo e tenta o source da página externa
    try:
        driver.switch_to.default_content()
        result = _search_html(driver.page_source)
        if result:
            setattr(driver, '_tokio_calc_id', result)
            return result
    except Exception:
        pass

    # 3) Varre os iframes — o calc_id está no source do iframe da cotação
    try:
        frames = driver.find_elements(By.TAG_NAME, 'iframe')
        for fr in frames:
            try:
                driver.switch_to.frame(fr)
                result = _search_html(driver.page_source)
                if result:
                    setattr(driver, '_tokio_calc_id', result)
                    driver.switch_to.default_content()
                    return result
            except Exception:
                pass
            finally:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
    except Exception:
        pass

    return None


def _requests_session_from_driver(driver: webdriver.Chrome) -> requests.Session:
    s = requests.Session()
    # Copia cookies do Selenium para Requests
    for c in driver.get_cookies():
        try:
            s.cookies.set(c['name'], c['value'], domain=c.get('domain', None), path=c.get('path', '/'))
        except Exception:
            s.cookies.set(c['name'], c['value'])
    # User-Agent semelhante ao do Chrome controlado
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Accept-Language': 'pt-BR,pt;q=0.9'
    })
    return s


def _flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}{k}."))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}{i}."))
    else:
        out[prefix[:-1]] = obj
    return out


def _find_by_keywords(flat: Dict[str, object], *keywords: str) -> Optional[str]:
    keys = [k.lower() for k in flat.keys()]
    for k in flat.keys():
        lk = k.lower()
        if all(kw in lk for kw in keywords):
            v = flat[k]
            if v is not None and v != "":
                return str(v)
    return None


def query_plate_via_api(driver: webdriver.Chrome, placa: str, timeout: int = 60) -> Dict[str, Optional[str]]:
    """Consulta a placa diretamente na API do portal usando os cookies da sessão."""
    _, portal_url = get_urls()
    base = portal_url.rstrip('/')
    calc_id = _get_calc_id_from_page(driver)
    if not calc_id:
        raise RuntimeError('Não foi possível identificar o número do Cálculo na página')

    endpoint = f"{base}/CotadorAutoService/dados/obterDadosItem/{calc_id}/{placa}/26"
    s = _requests_session_from_driver(driver)
    resp = s.get(endpoint, timeout=timeout)
    resp.raise_for_status()
    data = resp.json() if 'application/json' in resp.headers.get('Content-Type', '') else None
    if not isinstance(data, (dict, list)):
        raise RuntimeError('Resposta inesperada da API obterDadosItem')

    flat = _flatten(data)

    resultado = {
        'placa': placa,
        'chassi': _find_by_keywords(flat, 'chassi') or _find_by_keywords(flat, 'vin'),
        'ano_modelo': _find_by_keywords(flat, 'anomodelo') or _find_by_keywords(flat, 'ano', 'modelo') or _find_by_keywords(flat, 'ano'),
        'veiculo': _find_by_keywords(flat, 'veiculo', 'descricao') or _find_by_keywords(flat, 'modelo') or _find_by_keywords(flat, 'veiculo'),
        'valor_base_do_veiculo': _find_by_keywords(flat, 'valor', 'veiculo') or _find_by_keywords(flat, 'valorbase'),
        'codigo_fipe': _find_by_keywords(flat, 'fipe'),
    }
    return resultado
