"""
Debug: testa fluxo completo — placa → Pesquisar → modal → lê campos.
Uso: python debug_inspect_iframe.py [PLACA]
"""
import re
import sys
import time
from pathlib import Path

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException

from tokio_automation import build_driver, login
from config import nova_cotacao_url, get_urls


def snap(driver, outdir, name):
    p = outdir / f'{name}.png'
    driver.save_screenshot(str(p))
    print(f'  [shot] {p}')


def main():
    placa = sys.argv[1] if len(sys.argv) > 1 else 'JKM8143'
    driver = build_driver(headless=False)
    outdir = Path('output'); outdir.mkdir(exist_ok=True)
    _, portal_url = get_urls()
    portal_base = portal_url.rstrip('/')
    wait = WebDriverWait(driver, 40)

    try:
        login(driver)
        print('[1] Login OK')

        driver.get(portal_base + '/group/portal-corretor')
        time.sleep(1)
        driver.get(nova_cotacao_url())

        wait.until(EC.presence_of_element_located((By.TAG_NAME, 'iframe')))
        iframes = driver.find_elements(By.TAG_NAME, 'iframe')
        driver.switch_to.frame(iframes[0])
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input.placa')))
        print('[2] Dentro do iframe, input.placa OK')
        snap(driver, outdir, 'A_before_placa')

        # Digita a placa
        el = driver.find_element(By.CSS_SELECTOR, 'input.placa')
        el.click(); el.send_keys(Keys.CONTROL, 'a'); el.send_keys(Keys.DELETE)
        el.send_keys(placa.upper())
        driver.execute_script(
            "const e=arguments[0]; e.value=arguments[1];"
            " e.dispatchEvent(new Event('input',{bubbles:true}));"
            " e.dispatchEvent(new Event('change',{bubbles:true}));",
            el, placa.upper()
        )
        print(f'[3] Placa "{placa.upper()}" digitada')

        # Lista todos os botões e links antes do Pesquisar
        btns = driver.find_elements(By.XPATH, "//button | //a[contains(@class,'btn')]")
        print(f'[4] Botões/links com class btn: {len(btns)}')
        for b in btns[:15]:
            cls = b.get_attribute('class') or ''
            txt = b.text.strip()[:40]
            print(f'    tag={b.tag_name} class="{cls[:80]}" text="{txt}"')

        # Passo 1: TAB para disparar lookup de chassi (botão fica disabled até o chassi aparecer)
        el.send_keys(Keys.TAB)
        print('[4b] TAB enviado — aguardando lookup do chassi...')
        time.sleep(3)

        # Lista todos os botões e links antes do Pesquisar
        btns_all = driver.find_elements(By.XPATH, "//button | //a[contains(@class,'btn')]")
        print(f'[4] Botões/links com class btn: {len(btns_all)}')
        for b in btns_all[:15]:
            cls = b.get_attribute('class') or ''
            txt = b.text.strip()[:40]
            dis = b.get_attribute('disabled') or ''
            print(f'    tag={b.tag_name} class="{cls[:80]}" disabled={dis!r} text="{txt}"')

        # Passo 2: clica btn-pesquisar-veiculos (agora deve estar habilitado)
        pesquisar = driver.find_elements(By.CSS_SELECTOR, 'button.btn-pesquisar-veiculos')
        print(f'[5] button.btn-pesquisar-veiculos: {len(pesquisar)}')
        if not pesquisar:
            pesquisar = driver.find_elements(By.CSS_SELECTOR, 'a.btn-pesquisar-cotacao')
            print(f'[5b] a.btn-pesquisar-cotacao: {len(pesquisar)}')
        if pesquisar:
            # JS click para ignorar overlays
            driver.execute_script("arguments[0].click();", pesquisar[0])
            print('[6] Clicou Pesquisar (JS click)')
        else:
            print('[6] Pesquisar não encontrado')

        time.sleep(3)
        snap(driver, outdir, 'B_after_pesquisar')

        # Verifica se apareceu modal
        modal = driver.find_elements(By.XPATH, "//*[contains(.,'Lista de Ve')]")
        print(f'[7] Modal "Lista de Veículos": {len(modal)}')

        # Lista todos os elementos com "Lista de Ve"
        for m in modal[:3]:
            print(f'    tag={m.tag_name} class={m.get_attribute("class")} text={m.text[:60]}')

        # Tenta selecionar primeira linha da tabela
        if modal:
            rows = driver.find_elements(By.XPATH,
                "//div[contains(@class,'modal') and contains(@style,'block')]//table//tbody//tr |"
                "//div[contains(@class,'modal-body')]//table//tbody//tr |"
                "//table//tbody//tr"
            )
            print(f'[8] Linhas na tabela: {len(rows)}')
            # Mostra as primeiras linhas
            for r in rows[:4]:
                print(f'    row text: {r.text[:100]}')
            if rows:
                # Pula linhas vazias, clica a primeira com conteúdo
                target_row = next((r for r in rows if r.text.strip()), rows[0])
                driver.execute_script("arguments[0].click();", target_row)
                print(f'[9] Linha clicada (JS click): "{target_row.text.strip()[:80]}')
                time.sleep(3)
                snap(driver, outdir, 'C_after_row_click')

        # Lê todos inputs preenchidos
        print('[10] Inputs preenchidos:')
        all_inputs = driver.find_elements(By.TAG_NAME, 'input')
        for i in all_inputs:
            n = i.get_attribute('name') or ''
            v = i.get_attribute('value') or ''
            if v and v not in ('0', '00', '0,0', '0,00'):
                print(f'    name={n[:80]}  value={v[:60]}')

        # Lê todos os SELECT com valor selecionado
        print('[11] SELECTs com valor:')
        all_selects = driver.find_elements(By.TAG_NAME, 'select')
        for s in all_selects:
            n = s.get_attribute('name') or ''
            v = s.get_attribute('value') or ''
            try:
                opt_text = s.find_element(By.CSS_SELECTOR, 'option:checked').text
            except Exception:
                opt_text = ''
            if v or opt_text:
                print(f'    name={n[:80]}  value={v[:40]}  text={opt_text[:40]}')

        # Procura especificamente por anoModelo e veiculo
        print('[12] Busca por anoModelo e veiculo:')
        for pattern in ['anoModelo', 'veiculo', 'Veiculo', 'ano', 'descricao', 'modelo', 'FIPE', 'codFIPE']:
            els_i = driver.find_elements(By.CSS_SELECTOR, f'input[name*="{pattern}"]')
            els_s = driver.find_elements(By.CSS_SELECTOR, f'select[name*="{pattern}"]')
            for el in els_i + els_s:
                n = el.get_attribute('name') or ''
                v = el.get_attribute('value') or ''
                print(f'    pattern={pattern} tag={el.tag_name} name={n[:80]}  value={v[:60]}')

    finally:
        driver.quit()
        print('[fim]')


if __name__ == '__main__':
    main()

import re
import sys
import time
from pathlib import Path

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from tokio_automation import build_driver, login
from config import nova_cotacao_url, get_urls


def snap(driver, outdir, name):
    p = outdir / f'{name}.png'
    driver.save_screenshot(str(p))
    print(f'  [screenshot] {p}')


def list_clickables(driver, limit=20):
    """Lista botões e links com texto visível."""
    els = driver.find_elements(By.XPATH,
        "//button[normalize-space(.)] | //a[normalize-space(.)] | //span[@role='button' and normalize-space(.)]"
    )
    seen = set()
    for el in els:
        txt = (el.text or '').strip()
        if txt and txt not in seen:
            seen.add(txt)
            print(f'    [{el.tag_name}] "{txt[:80]}"')
        if len(seen) >= limit:
            break


def main():
    placa = sys.argv[1] if len(sys.argv) > 1 else 'JKM8143'
    driver = build_driver(headless=False)
    outdir = Path('output'); outdir.mkdir(exist_ok=True)
    _, portal_url = get_urls()
    portal_base = portal_url.rstrip('/')
    wait = WebDriverWait(driver, 30)

    try:
        # ── 1. Login ───────────────────────────────────────────────────────
        login(driver)
        print(f'[1] Login OK  URL={driver.current_url}')

        # ── 2. Portal base ────────────────────────────────────────────────
        driver.get(portal_base + '/group/portal-corretor')
        try:
            wait.until(EC.url_contains('portalparceiros'))
        except TimeoutException:
            pass
        time.sleep(3)
        print(f'[2] Portal URL={driver.current_url}')
        snap(driver, outdir, '01_portal_home')
        print('[2] Elementos clicáveis na página inicial:')
        list_clickables(driver)

        # ── 3. Navega para #/nova-cotacao ─────────────────────────────────
        driver.get(nova_cotacao_url())
        time.sleep(3)
        print(f'[3] Nova cotação URL={driver.current_url}  title="{driver.title}"')
        snap(driver, outdir, '02_nova_cotacao')
        print('[3] Elementos clicáveis na tela de nova cotação:')
        list_clickables(driver)

        # ── 4. Checa iframes já presentes ─────────────────────────────────
        iframes = driver.find_elements(By.TAG_NAME, 'iframe')
        print(f'[4] iframes presentes: {len(iframes)}')
        for i, fr in enumerate(iframes):
            print(f'    iframe[{i}] src={str(fr.get_attribute("src") or "")[:120]}')

        if not iframes:
            # Tenta clicar em "Novo" / "Nova" / "Iniciar" / "Cotação"
            print('[5] Sem iframe – tentando clicar em botão para iniciar cotação...')
            candidates = driver.find_elements(By.XPATH,
                "//button[contains(normalize-space(),'Novo') or contains(normalize-space(),'Nova') or "
                "contains(normalize-space(),'Iniciar') or contains(normalize-space(),'Cotação') or "
                "contains(normalize-space(),'Cotacao')] | "
                "//a[contains(normalize-space(),'Novo') or contains(normalize-space(),'Nova') or "
                "contains(normalize-space(),'Iniciar') or contains(normalize-space(),'Cotação')]"
            )
            print(f'    candidatos ao clique: {len(candidates)}')
            for c in candidates:
                print(f'      "{c.text.strip()[:60]}"')
            if candidates:
                candidates[0].click()
                time.sleep(4)
                snap(driver, outdir, '03_after_click_novo')
                iframes = driver.find_elements(By.TAG_NAME, 'iframe')
                print(f'[6] iframes após clique: {len(iframes)}')

        # ── 5. Salva HTML para inspeção manual ────────────────────────────
        with open(str(outdir / 'page_source.html'), 'w', encoding='utf-8') as f:
            f.write(driver.page_source)
        print('[7] page_source.html salvo em output/')

        # ── 6. Se tiver iframe, entra e inspeciona ────────────────────────
        iframes = driver.find_elements(By.TAG_NAME, 'iframe')
        if not iframes:
            print('[8] Ainda sem iframe – verifique os screenshots e page_source.html')
            return

        driver.switch_to.frame(iframes[0])
        print(f'[8] Dentro de iframe[0]')

        # Lista inputs
        inputs = driver.find_elements(By.TAG_NAME, 'input')
        print(f'[9] Inputs no iframe: {len(inputs)}')
        for i, inp in enumerate(inputs[:15]):
            attrs = {a: inp.get_attribute(a) for a in ['id', 'name', 'class', 'placeholder', 'type']}
            print(f'    input[{i}]: {attrs}')

        # input.placa
        placa_els = driver.find_elements(By.CSS_SELECTOR, 'input.placa')
        print(f'[10] input.placa: {len(placa_els)} encontrado(s)')

        # calc_id via source
        html = driver.page_source
        m = re.search(r'mapCotacoes(\d+)', html)
        if m:
            print(f'[11] calc_id = {m.group(1)}')
        else:
            print('[11] calc_id NÃO encontrado no source do iframe')

        snap(driver, outdir, '04_inside_iframe')
        with open(str(outdir / 'iframe_source.html'), 'w', encoding='utf-8') as f:
            f.write(html)
        print('[12] iframe_source.html salvo')

        # ── 7. Preenche e pesquisa ─────────────────────────────────────────
        if placa_els:
            el = placa_els[0]
            el.click()
            el.send_keys(Keys.CONTROL, 'a')
            el.send_keys(Keys.DELETE)
            el.send_keys(placa.upper())
            driver.execute_script(
                "const e=arguments[0]; e.value=arguments[1];"
                " e.dispatchEvent(new Event('input',{bubbles:true}));"
                " e.dispatchEvent(new Event('change',{bubbles:true}));",
                el, placa.upper()
            )
            print(f'[13] Placa "{placa.upper()}" digitada')

            btns = driver.find_elements(By.CSS_SELECTOR, 'a.btn-pesquisar-cotacao')
            if btns:
                btns[0].click()
                print('[14] Pesquisar clicado')
            else:
                el.send_keys(Keys.TAB)
                print('[14] TAB enviado (não achou btn-pesquisar-cotacao)')

            time.sleep(5)
            snap(driver, outdir, '05_after_pesquisar')

            # Lê inputs de resultado
            print('[15] Inputs após pesquisa:')
            result_inputs = driver.find_elements(By.TAG_NAME, 'input')
            for ri in result_inputs[:20]:
                nm = ri.get_attribute('name') or ''
                val = ri.get_attribute('value') or ''
                if val:
                    print(f'    name={nm[:60]}  value={val[:60]}')

    finally:
        driver.quit()
        print('[fim] Chrome fechado.')


if __name__ == '__main__':
    main()

