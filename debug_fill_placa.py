import sys
import time
from pathlib import Path

from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

from tokio_automation import build_driver, login
from config import nova_cotacao_url


JS_SNIPPET = r"""
(function(placa){
  function findPlacaIn(doc){
    const sels=[
      "input[placeholder*='placa' i]",
      "input[name*='placa' i]",
      "input[id*='placa' i]",
      "input[formcontrolname*='placa' i]",
      "input[aria-label*='placa' i]"
    ];
    for(const s of sels){ const el = doc.querySelector(s); if(el) return {el, doc}; }
    const lbl=[...doc.querySelectorAll('label')].find(l=>/placa/i.test((l.textContent||'').trim()));
    if(lbl){
      const near = (lbl.nextElementSibling && (lbl.nextElementSibling.querySelector?.('input')||lbl.nextElementSibling)) ||
                   (lbl.parentElement && lbl.parentElement.querySelector && lbl.parentElement.querySelector('input'));
      if(near && near.tagName==='INPUT') return {el:near, doc};
    }
    return null;
  }
  function clickPesquisarNear(doc, input){
    const root = input.closest('div') || doc;
    const btn = root.querySelector("button:has(*:contains('Pesquisar')), button:contains('Pesquisar'), a.btn-pesquisar-cotacao, i.fa-search, i[class*='search']");
    if(btn){
      const clickable = btn.closest('button,[role=button]') || btn.closest('a') || btn;
      clickable.click?.();
      return true;
    }
    return false;
  }
  function typeAndTrigger(input, val){
    input.focus();
    input.select?.();
    input.value='';
    input.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'deleteContent'}));
    for(const ch of (val||'').toUpperCase()){
      input.value += ch;
      input.dispatchEvent(new InputEvent('input',{bubbles:true,data:ch,inputType:'insertText'}));
    }
    input.dispatchEvent(new Event('change',{bubbles:true}));
    input.dispatchEvent(new Event('blur',{bubbles:true}));
  }

  // 1) principal
  let hit = findPlacaIn(document);
  if(hit){ typeAndTrigger(hit.el, placa); clickPesquisarNear(document, hit.el); return {context:'top', ok:true}; }
  // 2) iframes
  const iframes=[...document.querySelectorAll('iframe')];
  for(let i=0;i<iframes.length;i++){
    try{
      const doc = iframes[i].contentDocument || iframes[i].contentWindow?.document; if(!doc) continue;
      hit = findPlacaIn(doc);
      if(hit){ typeAndTrigger(hit.el, placa); clickPesquisarNear(doc, hit.el); return {context:'iframe['+i+']', ok:true}; }
    }catch(e){}
  }
  return {ok:false};
})(arguments[0]);
"""


def main():
    placa = sys.argv[1] if len(sys.argv) > 1 else 'JKM8143'
    headless = False  # força janela para depuração visual

    driver = build_driver(headless=headless)
    outdir = Path('output'); outdir.mkdir(exist_ok=True)
    try:
        login(driver)
        # navega direto para Nova Cotação, sem aguardar seletor estrito
        driver.get(nova_cotacao_url())
        # dá tempo de renderizar
        time.sleep(3)

        # injeta JS em cada contexto para digitar/clicar
        result = driver.execute_script(JS_SNIPPET, placa)
        print('[debug] JS result:', result)

        time.sleep(2)
        shot = outdir / f'debug_after_fill_{placa}.png'
        driver.save_screenshot(str(shot))
        print('[debug] Screenshot salvo em:', shot)

        # aguarda por alguma mudança (por exemplo, veículo/chassi preenchidos)
        time.sleep(3)
        shot2 = outdir / f'debug_after_wait_{placa}.png'
        driver.save_screenshot(str(shot2))
        print('[debug] Screenshot salvo em:', shot2)

    finally:
        driver.quit()


if __name__ == '__main__':
    main()
