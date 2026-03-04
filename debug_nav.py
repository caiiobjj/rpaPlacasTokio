"""
Debug: testa a navegação passo a passo.
"""
import time
import traceback
from selenium.webdriver.common.by import By
from tokio_automation import build_driver, login
from config import nova_cotacao_url, get_urls

driver = build_driver(headless=False)
try:
    login(driver)
    print('login OK, url=', driver.current_url)

    _, portal_url = get_urls()
    base = portal_url.rstrip('/')

    # Step 1: navigate to portal base (establish SSO session)
    driver.get(base + '/group/portal-corretor')
    time.sleep(2)
    print('step1 URL=', driver.current_url)

    # Step 2: navigate to nova cotacao
    target = nova_cotacao_url()
    driver.get(target)
    time.sleep(5)
    print('step2 URL=', driver.current_url)

    # Step 3: find iframes
    iframes = driver.find_elements(By.TAG_NAME, 'iframe')
    print('iframes found:', len(iframes))
    for i, fr in enumerate(iframes[:5]):
        src = fr.get_attribute('src') or '(no src)'
        print(f'  iframe[{i}] src={src[:100]}')

    # Step 4: check if CotadorAutoService iframe has input.placa
    if iframes:
        driver.switch_to.frame(iframes[0])
        placa = driver.find_elements(By.CSS_SELECTOR, 'input.placa')
        print('input.placa in iframe[0]:', len(placa))
        driver.switch_to.default_content()

    # Step 5: if not found, try forced reload with query param
    if not iframes or len(driver.find_elements(By.TAG_NAME, 'iframe')) == 0:
        print('No iframes - trying forced reload...')
        ts = str(int(time.time()))
        driver.get(f"{base}/group/portal-corretor?_r={ts}#/nova-cotacao")
        time.sleep(5)
        print('forced URL=', driver.current_url)
        iframes2 = driver.find_elements(By.TAG_NAME, 'iframe')
        print('iframes after forced reload:', len(iframes2))

except Exception:
    traceback.print_exc()
finally:
    driver.quit()
    print('Done.')
