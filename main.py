"""
Web Scraper para Portal Tokio Marine
Autentica e coleta dados do portal protegido
"""

import os
import sys
import json
import logging
from typing import Optional, Dict, List
from datetime import datetime
from pathlib import Path

# Importações de terceiros
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import csv

# Configuração de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('scraper.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class TokioMarineScraper:
    """Classe responsável pelo scraping do portal Tokio Marine"""
    
    def __init__(self, username: str, password: str):
        """
        Inicializa o scraper com credenciais
        
        Args:
            username: Usuário para autenticação
            password: Senha para autenticação
        """
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.timeout = int(os.getenv('TIMEOUT', 30))
        self.base_url = os.getenv('LOGIN_URL', 'https://ssoportais3.tokiomarine.com.br/openam/XUI/')
        self.portal_url = os.getenv('PORTAL_URL', 'http://portalparceiros.tokiomarine.com.br/')
        self.is_authenticated = False
        
        # User-Agent para parecer um navegador legítimo
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        logger.info("Scraper inicializado")
    
    def _get_auth_tree(self) -> Optional[Dict]:
        """
        Obtém a árvore de autenticação do ForgeRock
        
        Returns:
            Dicionário com a configuração de autenticação ou None
        """
        try:
            response = self.session.get(
                f"{self.base_url.split('?')[0]}json/realms/root/authenticate",
                timeout=self.session.timeout,
                params={'realm': 'TOKIOLFR'}
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.warning(f"Erro ao obter árvore de auth: {str(e)}")
            return None

    def login(self) -> bool:
        """
        Realiza o login no portal com ForgeRock OpenAM
        
        Returns:
            True se login foi bem-sucedido, False caso contrário
        """
        try:
            logger.info("Tentando fazer login com usuário informado (mascarado)")
            
            # Passo 1: Fazer GET inicial para pegar cookies e sessão
            response = self.session.get(self.base_url, timeout=self.timeout)
            response.raise_for_status()
            logger.info(f"Status do GET inicial: {response.status_code}")
            
            # Passo 2: Tentar obter árvore de autenticação (ForgeRock)
            auth_tree = self._get_auth_tree()
            
            # Passo 3: POST com callbacks (método padrão OpenAM)
            # callback_0 = username, callback_1 = password
            payload = {
                'callback_0': self.username,
                'callback_1': self.password,
            }
            
            # Tentar login via POST direto
            response = self.session.post(
                self.base_url,
                data=payload,
                timeout=self.timeout,
                allow_redirects=True
            )
            response.raise_for_status()
            logger.info(f"Status do POST de login: {response.status_code}")
            
            # Verificar sucesso do login
            success_indicators = [
                'portalparceiros.tokiomarine.com.br' in response.url or response.url,
                'error' not in response.text.lower() or response.status_code == 200
            ]
            
            # Se foi redirecionado para o portal, login foi bem-sucedido
            if 'portalparceiros' in response.url.lower():
                self.is_authenticated = True
                logger.info("✓ Login realizado com sucesso!")
                logger.info(f"URL atual: {response.url}")
                return True
            
            # Se ainda está na página de login sem erro aparente
            if response.status_code == 200 and 'login' in response.text.lower():
                logger.warning("Status OK mas ainda em página de login")
                logger.warning("Usuário/senha podem estar incorretos")
                return False
            
            logger.info(f"Status final: {response.status_code}")
            self.is_authenticated = True
            logger.info("✓ Login aparentemente bem-sucedido (status 200)")
            return True
                
        except requests.exceptions.Timeout:
            logger.error("Timeout na requisição de login")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"Erro na requisição de login: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"Erro inesperado durante login: {str(e)}")
            return False
    
    def get_page(self, url: str) -> Optional[BeautifulSoup]:
        """
        Faz requisição GET para uma página
        
        Args:
            url: URL da página a acessar
            
        Returns:
            BeautifulSoup com conteúdo da página ou None em caso de erro
        """
        if not self.is_authenticated:
            logger.error("Não autenticado. Execute login() primeiro.")
            return None
        
        try:
            response = self.session.get(url, timeout=self.session.timeout)
            response.raise_for_status()
            return BeautifulSoup(response.content, 'html.parser')
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Erro ao acessar {url}: {str(e)}")
            return None
    
    def extract_tables(self, soup: BeautifulSoup) -> List[Dict]:
        """
        Extrai todas as tabelas da página
        
        Args:
            soup: BeautifulSoup com conteúdo da página
            
        Returns:
            Lista de DataFrames, uma para cada tabela
        """
        tables: List[Dict] = []
        for table in soup.find_all('table'):
            try:
                headers = [th.get_text(strip=True) for th in table.find_all('th')]
                rows = []
                for tr in table.find_all('tr'):
                    cells = [td.get_text(strip=True) for td in tr.find_all(['td','th'])]
                    if not cells:
                        continue
                    if headers and len(cells) == len(headers):
                        rows.append(dict(zip(headers, cells)))
                    else:
                        row = {f'col_{i+1}': v for i, v in enumerate(cells)}
                        rows.append(row)
                if rows:
                    tables.append({'headers': headers, 'rows': rows})
                    logger.info(f"Tabela extraída com {len(rows)} linhas")
            except Exception as e:
                logger.warning(f"Erro ao extrair tabela: {str(e)}")
        return tables
    
    def extract_text_content(self, soup: BeautifulSoup) -> Dict[str, str]:
        """
        Extrai conteúdo textual relevante da página
        
        Args:
            soup: BeautifulSoup com conteúdo da página
            
        Returns:
            Dicionário com conteúdo extraído
        """
        content = {
            'title': soup.title.string if soup.title else 'N/A',
            'text': soup.get_text(separator='\n', strip=True)[:1000],  # Primeiros 1000 caracteres
            'links': [a.get('href') for a in soup.find_all('a') if a.get('href')],
        }
        return content
    
    def save_to_json(self, data: dict, filename: str = 'dados_extraidos.json') -> str:
        """
        Salva dados em arquivo JSON
        
        Args:
            data: Dicionário com dados
            filename: Nome do arquivo
            
        Returns:
            Caminho do arquivo salvo
        """
        output_dir = Path('output')
        output_dir.mkdir(exist_ok=True)
        
        filepath = output_dir / filename
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Dados salvos em: {filepath}")
        return str(filepath)
    
    def save_table_to_csv(self, table: Dict, filename: str = 'dados_extraidos.csv') -> str:
        """
        Salva DataFrame em arquivo CSV
        
        Args:
            df: DataFrame a salvar
            filename: Nome do arquivo
            
        Returns:
            Caminho do arquivo salvo
        """
        output_dir = Path('output')
        output_dir.mkdir(exist_ok=True)
        
        filepath = output_dir / filename
        rows = table.get('rows', [])
        if not rows:
            logger.warning("Tabela sem linhas para salvar")
            return str(filepath)
        fieldnames = list(rows[0].keys())
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        
        logger.info(f"CSV salvo em: {filepath}")
        return str(filepath)
    
    def close(self):
        """Fecha a sessão"""
        self.session.close()
        logger.info("Sessão encerrada")


def main():
    """Função principal"""
    
    # Carregar variáveis de ambiente
    load_dotenv()
    
    username = os.getenv('USERNAME')
    password = os.getenv('PASSWORD')
    
    if not username or not password:
        logger.error("Credenciais não encontradas em .env")
        logger.error("Por favor, copie .env.example para .env e preencha as credenciais")
        sys.exit(1)
    
    # Criar instância do scraper
    scraper = TokioMarineScraper(username, password)
    
    try:
        # Fazer login
        if not scraper.login():
            logger.error("Falha na autenticação")
            sys.exit(1)
        
        # Acessar portal
        logger.info("Acessando portal...")
        soup = scraper.get_page(scraper.portal_url)
        
        if soup:
            # Extrair dados
            tables = scraper.extract_tables(soup)
            text_data = scraper.extract_text_content(soup)
            
            # Salvar resultados
            scraper.save_to_json(text_data, f'portal_dados_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json')
            
            if tables:
                for i, table in enumerate(tables):
                    scraper.save_table_to_csv(table, f'tabela_{i}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv')
            
            logger.info("✓ Scraping concluído com sucesso!")
        else:
            logger.error("Falha ao acessar portal")
    
    finally:
        scraper.close()


if __name__ == '__main__':
    main()
