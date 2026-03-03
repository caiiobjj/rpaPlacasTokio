import os
import json
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


def ensure_output():
    out = Path('output')
    out.mkdir(exist_ok=True)
    return out


def save_text(data: dict, outdir: Path, name: str):
    path = outdir / name
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return str(path)


def main():
    load_dotenv()

    username = os.getenv('USERNAME')
    password = os.getenv('PASSWORD')
    login_url = os.getenv('LOGIN_URL', 'https://ssoportais3.tokiomarine.com.br/openam/XUI/?realm=TOKIOLFR')
    portal_url = os.getenv('PORTAL_URL', 'http://portalparceiros.tokiomarine.com.br/')
    headless = os.getenv('HEADLESS', 'true').lower() in ('1', 'true', 'yes')

    if not username or not password:
        raise SystemExit('Preencha USERNAME e PASSWORD no .env')

    outdir = ensure_output()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        # Navega para a tela de login
        page.goto(login_url, wait_until='domcontentloaded', timeout=45000)

        # Preenche campos de login (ids detectados: idToken1 e idToken2)
        page.fill('#idToken1', username)
        page.fill('#idToken2', password)

        # Clica no botão Entrar
        page.click('#loginButton_0')

        # Aguarda redirecionamento para o portal
        try:
            page.wait_for_url('**portalparceiros.tokiomarine.com.br**', timeout=60000)
        except PlaywrightTimeout:
            # Tenta carregar o portal diretamente se já autenticado via SSO
            page.goto(portal_url, wait_until='load', timeout=60000)

        # Salva screenshot e HTML da página atual
        shot_path = outdir / f'portal_{timestamp}.png'
        page.screenshot(path=str(shot_path), full_page=True)

        html = page.content()
        soup = BeautifulSoup(html, 'html.parser')

        info = {
            'url': page.url,
            'title': soup.title.string if soup.title else '',
            'links': [a.get('href') for a in soup.find_all('a') if a.get('href')][:200],
            'tables': len(soup.find_all('table')),
        }

        json_path = save_text(info, outdir, f'portal_info_{timestamp}.json')

        # Opcional: salva HTML bruto
        html_path = outdir / f'portal_raw_{timestamp}.html'
        html_path.write_text(html, encoding='utf-8')

        # Persiste estado de autenticação para reuso
        state_path = outdir / 'auth_state.json'
        context.storage_state(path=str(state_path))

        print('Login concluído')
        print(f'- URL: {info["url"]}')
        print(f'- Título: {info["title"]}')
        print(f'- Screenshot: {shot_path}')
        print(f'- Info JSON: {json_path}')
        print(f'- HTML salvo: {html_path}')
        print(f'- Estado: {state_path}')

        browser.close()


if __name__ == '__main__':
    main()
