#!/usr/bin/env python3
"""
Script de teste para validar login no Portal Tokio Marine
Útil para debugar problemas de autenticação
"""

import os
import sys
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

# Carregar .env
load_dotenv()

USERNAME = os.getenv('USERNAME', 'seu_usuario')
PASSWORD = os.getenv('PASSWORD', 'sua_senha')
BASE_URL = 'https://ssoportais3.tokiomarine.com.br/openam/XUI/'
TIMEOUT = 30

print("=" * 60)
print("TESTE DE AUTENTICAÇÃO - PORTAL TOKIO MARINE")
print("=" * 60)
print(f"Usuário: {USERNAME}")
print(f"URL: {BASE_URL}")
print()

# Criar sessão
session = requests.Session()
session.timeout = TIMEOUT
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
})

try:
    # Teste 1: GET inicial
    print("[1/4] Fazendo GET inicial...")
    resp = session.get(BASE_URL, timeout=TIMEOUT)
    print(f"✓ Status: {resp.status_code}")
    print(f"✓ Cookies: {len(session.cookies)}")
    print(f"✓ URL: {resp.url}")
    print()
    
    # Teste 2: Analisar HTML
    print("[2/4] Analisando estrutura HTML...")
    soup = BeautifulSoup(resp.content, 'html.parser')
    forms = soup.find_all('form')
    print(f"✓ Formulários encontrados: {len(forms)}")
    
    if forms:
        form = forms[0]
        inputs = form.find_all('input')
        print(f"✓ Campos de input: {len(inputs)}")
        for inp in inputs:
            print(f"  - {inp.get('name')}: {inp.get('type')} (id: {inp.get('id')})")
    print()
    
    # Teste 3: POST com credenciais
    print("[3/4] Tentando fazer POST com credenciais...")
    payload = {
        'callback_0': USERNAME,
        'callback_1': PASSWORD,
    }
    
    resp = session.post(BASE_URL, data=payload, timeout=TIMEOUT, allow_redirects=True)
    print(f"✓ Status: {resp.status_code}")
    print(f"✓ URL final: {resp.url}")
    print(f"✓ Cookies após login: {len(session.cookies)}")
    print()
    
    # Teste 4: Verificar autenticação
    print("[4/4] Verificando resultado...")
    
    if 'portalparceiros' in resp.url.lower():
        print("✓ SUCESSO! Redirecionado para o portal")
        print(f"✓ URL: {resp.url}")
    elif resp.status_code == 200:
        print("⚠ Status 200 mas ainda em página de login")
        
        # Procurar por mensagem de erro
        if 'erro' in resp.text.lower() or 'error' in resp.text.lower():
            print("✗ Encontrada mensagem de erro na página")
            
            # Tentar extrair mensagem
            error_elem = soup.find(class_=lambda x: x and 'error' in x.lower())
            if error_elem:
                print(f"  Erro: {error_elem.get_text()}")
        else:
            print("→ Verificar se credenciais estão corretas")
    else:
        print(f"✗ Status inesperado: {resp.status_code}")
    
    print()
    print("=" * 60)
    print("RESUMO:")
    print(f"Cookie: {dict(session.cookies)}")
    print(f"Headers: {dict(session.headers)}")
    print("=" * 60)
    
except requests.exceptions.Timeout:
    print("✗ TIMEOUT - Servidor não respondeu")
except requests.exceptions.RequestException as e:
    print(f"✗ ERRO DE CONEXÃO: {str(e)}")
except Exception as e:
    print(f"✗ ERRO: {str(e)}")
    import traceback
    traceback.print_exc()
