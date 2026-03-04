"""Test the full automation flow."""
import traceback
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from tokio_automation import build_driver, login, go_to_nova_cotacao, query_plate
from config import nova_cotacao_url, get_urls

driver = build_driver(headless=False)
try:
    login(driver)
    print('login OK, url=', driver.current_url)

    # Manual go_to_nova_cotacao for debugging
    _, portal_url = get_urls()
    base = portal_url.rstrip('/')
    target = nova_cotacao_url()

    print('step1: navigating to portal-corretor...')
    driver.get(base + '/group/portal-corretor')
    time.sleep(2)
    print('step1 done, url=', driver.current_url)

    print('step2: navigating to nova-cotacao...')
    driver.get(target)
    print('step2 GET called, waiting...')
    time.sleep(3)
    print('step2 done, url=', driver.current_url)

    iframes = driver.find_elements(By.TAG_NAME, 'iframe')
    print('iframes found:', len(iframes))
    for i, fr in enumerate(iframes[:5]):
        src = fr.get_attribute('src') or '(no src)'
        print(f'  iframe[{i}] src={src[:80]}')

    if not iframes:
        print('ERROR: no iframes! Checking page title...')
        print('title=', driver.title)
        print('url=', driver.current_url)
    else:
        print('step3: entering iframe[0]...')
        driver.switch_to.frame(iframes[0])
        print('in iframe, checking input.placa...')
        placa_els = driver.find_elements(By.CSS_SELECTOR, 'input.placa')
        print('input.placa elements:', len(placa_els))
        driver.switch_to.default_content()

        if placa_els:
            print('step4: calling query_plate directly (already navigated)...')
            # We're at default_content, iframe[0] has input.placa
            dados = query_plate(driver, 'JKM8143')
            print('SUCESSO!')
            for k, v in dados.items():
                print(f'  {k}: {v}')
        else:
            print('input.placa NOT found in iframe[0]')

except Exception:
    traceback.print_exc()
finally:
    driver.quit()
    print('Done.')
