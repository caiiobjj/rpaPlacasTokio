import time
from typing import Dict, Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import shutil
import requests
import re

from config import (
    get_credentials, get_urls, get_headless, nova_cotacao_url, login_url_with_goto,
    TIMEOUT_DRIVER, TIMEOUT_MODAL, TIMEOUT_IFRAME, TIMEOUT_PAGE,
)


def build_driver(headless: Optional[bool] = None) -> webdriver.Chrome:
    if headless is None:
        headless = get_headless()

    def _make(headless_flag: bool) -> webdriver.Chrome:
        options = webdriver.ChromeOptions()
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
        options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])
        options.add_experimental_option('useAutomationExtension', False)
        # Usa chromedriver do sistema (instalado em /usr/local/bin) ou deixa Selenium Manager encontrar
        chromedriver_path = shutil.which('chromedriver') or '/usr/local/bin/chromedriver'
        try:
            service = ChromeService(chromedriver_path)
            return webdriver.Chrome(service=service, options=options)
        except Exception:
            # Fallback: Selenium Manager sem especificar service
            return webdriver.Chrome(options=options)

    # Tenta criar em headless; se falhar, tenta com janela normal
    try:
        return _make(headless)
    except Exception:
        if headless:
            return _make(False)
        raise


def login(driver: webdriver.Chrome, timeout: int = 60) -> None:
    username, password = get_credentials()
    login_url, portal_url = get_urls()

    wait = WebDriverWait(driver, timeout)
    driver.get(login_url)

    # Campos padrão do OpenAM XUI
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '#idToken1')))
    driver.find_element(By.CSS_SELECTOR, '#idToken1').clear()
    driver.find_element(By.CSS_SELECTOR, '#idToken1').send_keys(username)

    driver.find_element(By.CSS_SELECTOR, '#idToken2').clear()
    driver.find_element(By.CSS_SELECTOR, '#idToken2').send_keys(password)

    # Entrar
    driver.find_element(By.CSS_SELECTOR, '#loginButton_0').click()

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
        url = driver.current_url
        # Tela de login explícita — sessão expirou
        if 'ssoportais3.tokiomarine.com.br' in url and '#login' in url:
            return False
        # Dentro do portal Tokio Marine — sessão válida
        if 'tokiomarine.com.br' not in url:
            return False
        # Página deve ter título (Angular carregado)
        title = driver.title or ''
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
    wait = WebDriverWait(driver, timeout)
    try:
        # Aguarda modal aparecer (título Lista de Veículos)
        wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//*[contains(.,'Lista de Ve')]")
            )
        )
        # Seleciona a primeira linha — tenta XPathS específicos primeiro
        row_xpaths = [
            "(//div[contains(@class,'modal') and contains(@style,'block')]//table//tbody//tr)",
            "(//div[contains(@class,'modal-body')]//table//tbody//tr)",
            "(//table//tbody//tr)",
        ]
        rows = []
        for xp in row_xpaths:
            rows = driver.find_elements(By.XPATH, xp)
            if rows:
                break
        # Pula linhas vazias (header rows ou separadores)
        real_rows = [r for r in rows if r.text.strip()]
        row = real_rows[0] if real_rows else (rows[0] if rows else None)
        if row:
            # JS click para contornar "element not interactable"
            driver.execute_script("arguments[0].click();", row)
            # Aguarda modal fechar ou formulário atualizar
            try:
                WebDriverWait(driver, TIMEOUT_PAGE).until(
                    lambda d: not d.find_elements(
                        By.XPATH,
                        "//div[contains(@class,'modal') and contains(@style,'block')]"
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
    """Encontra o iframe do CotadorAutoService pela URL src, com fallback para iframe[0]."""
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
        cotador = _find_cotador_iframe(driver)
        if cotador is None:
            return False
        driver.switch_to.frame(cotador)
        found = bool(driver.find_elements(By.CSS_SELECTOR, 'input.placa'))
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
    wait = WebDriverWait(driver, timeout)

    def _cotador_ready(drv):
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


def query_plate(driver: webdriver.Chrome, placa: str, timeout: int = 60) -> Dict[str, Optional[str]]:
    """
    Preenche a placa no formulário da cotação e retorna os dados do veículo.
    Assume que o driver está no contexto padrão (default_content).
    Localiza o CotadorAutoService iframe pela URL src para robustez.
    """
    from selenium.webdriver.common.keys import Keys
    wait = WebDriverWait(driver, timeout)

    placa_txt = (placa or '').strip().upper()
    if not placa_txt:
        raise ValueError('Placa vazia')

    # Vai ao contexto principal para buscar o iframe correto
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    # Aguarda o iframe do CotadorAutoService aparecer (por src)
    def _cotador_iframe_present(drv):
        return _find_cotador_iframe(drv) is not None

    wait.until(_cotador_iframe_present)
    cotador_frame = _find_cotador_iframe(driver)
    if cotador_frame is None:
        raise RuntimeError('CotadorAutoService iframe não encontrado')
    driver.switch_to.frame(cotador_frame)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input.placa')))
    plate_input = driver.find_element(By.CSS_SELECTOR, 'input.placa')

    # Scrola e foca (JS click para ignorar qualquer overlay que intercepte o clique normal)
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", plate_input)
    except Exception:
        pass
    # Remove readonly/disabled que o Angular pode ter adicionado após seleção de veículo
    try:
        driver.execute_script(
            "arguments[0].removeAttribute('readonly');"
            "arguments[0].removeAttribute('disabled');",
            plate_input
        )
    except Exception:
        pass
    try:
        driver.execute_script("arguments[0].click();", plate_input)
    except Exception:
        plate_input.click()

    # Limpa via JS (mais robusto que CTRL+A quando o campo está em estado angular bloqueado)
    try:
        driver.execute_script(
            "arguments[0].value = '';"
            " arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
            " arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
            plate_input
        )
    except Exception:
        pass
    try:
        plate_input.send_keys(Keys.CONTROL, 'a')
        plate_input.send_keys(Keys.DELETE)
    except Exception:
        pass  # JS clear já limpou o campo acima

    # Digita a placa via send_keys (fallback: JS já definiu o valor acima se falhar)
    try:
        plate_input.send_keys(placa_txt)
    except Exception:
        pass  # JS dispatch abaixo garante o valor correto mesmo sem send_keys

    # Dispara eventos para o framework JS
    try:
        driver.execute_script(
            "const e=arguments[0]; e.value=arguments[1];"
            " e.dispatchEvent(new Event('input',{bubbles:true}));"
            " e.dispatchEvent(new Event('change',{bubbles:true}));",
            plate_input, placa_txt
        )
    except Exception:
        pass

    # Passo 1: TAB dispara o lookup do chassis no backend (botão Pesquisar fica disabled até então)
    try:
        plate_input.send_keys(Keys.TAB)
    except Exception:
        # Fallback: ActionChains com TAB
        from selenium.webdriver.common.action_chains import ActionChains
        try:
            ActionChains(driver).move_to_element(plate_input).send_keys(Keys.TAB).perform()
        except Exception:
            pass
    # Aguarda o botão Pesquisar ser habilitado (backend preenche chassi em background)
    try:
        WebDriverWait(driver, TIMEOUT_MODAL).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, 'button.btn-pesquisar-veiculos:not([disabled])')
            or d.find_elements(By.CSS_SELECTOR, 'a.btn-pesquisar-cotacao')
        )
    except TimeoutException:
        pass  # tenta clicar mesmo assim

    # Passo 2: clica no botão Pesquisar Veiculos (agora deve estar habilitado)
    # Usa JS click para ignorar qualquer overlay/span que intercepta o clique normal
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

    # Sempre tenta selecionar o veículo da "Lista de Veículos" (modal abre após Pesquisar)
    _maybe_select_first_vehicle_in_modal(driver, timeout=20)
    # Não usa sleep fixo — _vehicle_info_populated abaixo aguarda o formulário atualizar

    # Aguarda o campo veiculo/modelo ser preenchido (indica seleção bem-sucedida)
    def _vehicle_info_populated(drv):
        # Verifica se o SELECT de modelo tem um valor selecionado
        modelo_els = drv.find_elements(By.CSS_SELECTOR, 'select[name*=".modelo."]')
        if modelo_els:
            try:
                text = modelo_els[0].find_element(By.CSS_SELECTOR, 'option:checked').text.strip()
                if text and text.lower() not in ('selecione', ''):
                    return True
            except Exception:
                pass
        # Fallback: chassi preenchido
        chassi_els = drv.find_elements(By.CSS_SELECTOR, 'input[name*="chassi"]')
        return bool(chassi_els and (chassi_els[0].get_attribute('value') or '').strip())

    try:
        wait.until(_vehicle_info_populated)
    except TimeoutException:
        pass  # retorna o que estiver preenchido

    # Lê campos usando name=* (mais robusto que labels)
    def _by_name(pattern: str) -> Optional[str]:
        els = driver.find_elements(By.CSS_SELECTOR, f'input[name*="{pattern}"]')
        return (els[0].get_attribute('value') or '').strip() or None if els else None

    def _by_select_text(pattern: str) -> Optional[str]:
        """Lê o texto da opção selecionada de um SELECT cujo name contém o padrão."""
        els = driver.find_elements(By.CSS_SELECTOR, f'select[name*="{pattern}"]')
        for el in els:
            try:
                text = el.find_element(By.CSS_SELECTOR, 'option:checked').text.strip()
                if text and text.lower() not in ('selecione', '-selecione-', '', 'selecionar'):
                    return text
            except Exception:
                pass
        return None

    dados = {
        'placa':                 _by_name('.placa.')          or _by_name('placa')     or placa,
        'chassi':                _by_name('.chassi.')         or _by_name('chassi'),
        'ano_modelo':            _by_select_text('anoModelo') or _read_value(driver, 'Ano modelo'),
        'veiculo':               _by_select_text('.modelo.')  or _by_select_text('modelo') or _read_value(driver, 'Veículo'),
        'valor_base_do_veiculo': _by_name('valorBase')        or _read_value(driver, 'Valor base'),
        'codigo_fipe':           _by_name('codFIPE')          or _by_name('FIPE') or _read_value(driver, 'Código FIPE'),
    }

    # Volta ao contexto principal
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

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
            return result
    except Exception:
        pass

    # 2) Volta ao topo e tenta o source da página externa
    try:
        driver.switch_to.default_content()
        result = _search_html(driver.page_source)
        if result:
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
