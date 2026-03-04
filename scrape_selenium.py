import os
import json
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

from config import get_credentials, get_urls, get_headless

def ensure_output():
    out = Path('output')
    out.mkdir(exist_ok=True)
    return out


def main():
    load_dotenv()

    username, password = get_credentials()
    login_url, portal_url = get_urls()
    headless = get_headless()

    if not username or not password:
        raise SystemExit('Preencha USERNAME e PASSWORD no .env')

    outdir = ensure_output()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1600,1200')
    options.add_argument('--disable-dev-shm-usage')

    driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)

    try:
        wait = WebDriverWait(driver, 60)
        print('[1/4] Abrindo página de login...')
        driver.get(login_url)

        # Preenche login
        print('[2/4] Preenchendo credenciais...')
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '#idToken1')))
        driver.find_element(By.CSS_SELECTOR, '#idToken1').clear()
        driver.find_element(By.CSS_SELECTOR, '#idToken1').send_keys(username)

        driver.find_element(By.CSS_SELECTOR, '#idToken2').clear()
        driver.find_element(By.CSS_SELECTOR, '#idToken2').send_keys(password)

        print('[3/4] Enviando formulário...')
        driver.find_element(By.CSS_SELECTOR, '#loginButton_0').click()

        # Aguarda redirecionamento para o portal
        print('[4/4] Aguardando redirecionamento para o portal...')
        try:
            wait.until(EC.url_contains('portalparceiros.tokiomarine.com.br'))
        except TimeoutException:
            # Tenta ir direto ao portal (SSO pode já estar válido)
            driver.get(portal_url)
            try:
                wait.until(EC.url_contains('portalparceiros.tokiomarine.com.br'))
            except TimeoutException:
                # Se ainda não redirecionou, salva artefatos de diagnóstico
                err_shot = outdir / f'login_error_{timestamp}.png'
                driver.save_screenshot(str(err_shot))
                html_err = outdir / f'login_error_{timestamp}.html'
                html_err.write_text(driver.page_source, encoding='utf-8')
                # Tenta identificar mensagens de erro na página
                soup_err = BeautifulSoup(driver.page_source, 'html5lib')
                msg = soup_err.get_text(" ", strip=True)[:400]
                print('⚠ Não foi possível confirmar o redirecionamento ao portal.')
                print(f'- Página atual: {driver.current_url}')
                print(f'- Screenshot de erro: {err_shot}')
                print(f'- HTML de erro: {html_err}')
                print(f'- Primeiros 400 chars do texto da página: {msg}')
                # Encerrar cedo para o usuário analisar
                return

        # Salva screenshot e HTML
        shot_path = outdir / f'portal_{timestamp}.png'
        driver.save_screenshot(str(shot_path))

        html = driver.page_source
        soup = BeautifulSoup(html, 'html5lib')
        info = {
            'url': driver.current_url,
            'title': soup.title.string if soup.title else '',
            'links': [a.get('href') for a in soup.find_all('a') if a.get('href')][:200],
            'tables': len(soup.find_all('table')),
        }

        json_path = outdir / f'portal_info_{timestamp}.json'
        json_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')

        html_path = outdir / f'portal_raw_{timestamp}.html'
        html_path.write_text(html, encoding='utf-8')

        print('Login concluído')
        print(f'- URL: {info["url"]}')
        print(f'- Título: {info["title"]}')
        print(f'- Screenshot: {shot_path}')
        print(f'- Info JSON: {json_path}')
        print(f'- HTML salvo: {html_path}')

    finally:
        driver.quit()


if __name__ == '__main__':
    main()
