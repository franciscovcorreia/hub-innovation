"""
database_and_scraper.py (versão 2)

Recolha, filtragem e armazenamento de notícias para o Hub de Inovação e
Comunicações Emergentes (Portugal).

Princípios de funcionamento
- Sem APIs de inteligência artificial: a seleção e a classificação usam
  apenas regras e palavras-chave em português.
- Apenas notícias de acesso livre. Artigos com paywall, página de subscrição,
  ligação inexistente ou sem acesso ficam marcados como "bloqueado" e nunca
  são apresentados (nem o respetivo link).
- Apenas notícias com contexto português (configurável em EXIGIR_CONTEXTO_PT).
- Sem duplicados: o URL (sem parâmetros de rastreio) é único na base de dados.
- O título guardado é o título publicado pela fonte, sem alterações.

Dependências: feedparser, requests, beautifulsoup4, lxml

Utilização
    python database_and_scraper.py                   atualiza a base de dados
    python database_and_scraper.py --diagnostico     testa as fontes (não grava)
    python database_and_scraper.py --verificar URL   testa o acesso a um artigo
    python database_and_scraper.py --init            apaga e recria a base de dados
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from zoneinfo import ZoneInfo

    FUSO_LISBOA = ZoneInfo("Europe/Lisbon")
except Exception:  # sem base de dados de fusos horários (ex.: Windows sem tzdata)
    FUSO_LISBOA = timezone.utc

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuração geral
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).resolve().with_name("noticias_hub.db")

EXIGIR_CONTEXTO_PT = True          # só notícias com referências a Portugal
VERIFICAR_ACESSO = True            # abre cada notícia nova para detetar paywall
INCLUIR_NAO_VERIFICADAS = False    # mostrar notícias cujo acesso não foi possível confirmar
MAX_VERIFICACOES_POR_EXECUCAO = 40
MAX_REVERIFICACOES = 15            # artigos por verificar a repetir em cada atualização
MAX_IMAGENS_POR_EXECUCAO = 30      # notícias sem imagem a tentar em cada atualização
MAX_DESCRICOES_POR_EXECUCAO = 20  # notícias sem resumo a completar por atualização
MAX_FALHAS_DE_REDE = 3            # depois de tantas falhas de rede, deixa de se repetir o pedido à página
RETENCAO_DIAS = int(os.environ.get("HUB_RETENCAO_DIAS", "365"))  # notícias mais antigas são apagadas na atualização
TIMEOUT = (6, 15)                  # ligação, leitura (segundos)
PAUSA_ENTRE_FONTES = 1.0
MAX_BYTES_PAGINA = 600_000

CABECALHOS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 HubComunicacoesEmergentes/2.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.6",
}


def _criar_sessao() -> requests.Session:
    sessao = requests.Session()
    sessao.headers.update(CABECALHOS)
    repeticoes = Retry(
        total=2,
        backoff_factor=0.6,
        status_forcelist=(502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adaptador = HTTPAdapter(max_retries=repeticoes, pool_connections=8, pool_maxsize=16)
    sessao.mount("https://", adaptador)
    sessao.mount("http://", adaptador)
    return sessao


SESSAO = _criar_sessao()

# ---------------------------------------------------------------------------
# Fontes
# ---------------------------------------------------------------------------
# feeds:   endereços RSS/Atom conhecidos (todos os que funcionarem são usados).
# paginas: páginas HTML usadas para descobrir feeds e, se não houver feed,
#          para extrair as notícias diretamente da listagem.
# sempre_pt: dispensa o teste de contexto português (fonte nacional oficial).
# padrao_link: expressão regular que identifica ligações de notícias na listagem.

CATEGORIA_REGULACAO = "Regulação"

FONTES: list[dict[str, Any]] = [
    {
        "nome": "TEK SAPO",
        "feeds": ["https://tek.sapo.pt/rss"],
        "paginas": ["https://tek.sapo.pt/"],
    },
    {
        "nome": "Pplware",
        "feeds": ["https://pplware.sapo.pt/feed/"],
        "paginas": ["https://pplware.sapo.pt/"],
    },
    {
        "nome": "ECO",
        "feeds": ["https://eco.sapo.pt/feed/"],
        "paginas": ["https://eco.sapo.pt/"],
    },
    {
        "nome": "Jornal de Negócios",
        "feeds": [],
        "paginas": [
            "https://www.jornaldenegocios.pt/empresas/tecnologias",
            "https://www.jornaldenegocios.pt/rss",
            "https://www.jornaldenegocios.pt/",
        ],
    },
    {
        "nome": "Público",
        "feeds": ["https://feeds.feedburner.com/PublicoRSS"],
        "paginas": [
            "https://www.publico.pt/tecnologia",
            "https://www.publico.pt/rss",
        ],
    },
    {
        "nome": "Dinheiro Vivo",
        "feeds": ["https://www.dinheirovivo.pt/feed"],
        "paginas": ["https://www.dinheirovivo.pt/"],
    },
    {
        "nome": "Observador",
        "feeds": ["https://observador.pt/feed"],
        "paginas": ["https://observador.pt/"],
    },
    {
        "nome": "IT Security",
        "feeds": [],
        "paginas": ["https://www.itsecurity.pt/", "https://www.itsecurity.pt/news/"],
        "padrao_link": r"itsecurity\.pt/news/[^/?#]+/[^/?#]+",
        "sempre_pt": True,
    },
    {
        "nome": "FCT",
        "feeds": ["https://www.fct.pt/media/noticias/feed/"],
        "paginas": ["https://www.fct.pt/media/noticias/"],
        "sempre_pt": True,
    },
    {
        "nome": "ANI",
        "feeds": [],
        "paginas": ["https://www.ani.pt/pt/noticias/"],
        "sempre_pt": True,
    },
    {
        "nome": "Portal do Governo",
        "feeds": [],
        "paginas": ["https://portugal.gov.pt/gc25/comunicacao/noticias"],
        "sempre_pt": True,
    },
]

# ---------------------------------------------------------------------------
# Política de utilização de conteúdo por fonte (pré-visualização)
# ---------------------------------------------------------------------------
# uso:  "resumo"  -> só o resumo/descrição que o próprio feed da fonte publica (comportamento por omissão)
#       "excerto" -> excerto do artigo, até limite_caracteres (só com licença/autorização verificada)
#       "integral"-> artigo completo (só com licença/autorização verificada)
# Uma fonte só passa de "resumo" para "excerto"/"integral" se licenca_verificada=True, verificado_em estiver
# preenchido e fundamento descrever a base legal (autorização escrita, licença CC, etc.). Caso contrário o código
# volta a "resumo" e regista um aviso. NENHUMA fonte está autorizada neste momento: as condições de utilização
# não foram verificadas e todas aguardam revisão humana (ver revisao_humana).
# Nunca se contornam paywalls, logins ou bloqueios técnicos: artigos com acesso "bloqueado" não têm conteúdo.
USOS_PERMITIDOS = ("resumo", "excerto", "integral")
MAX_CONTEUDOS_POR_EXECUCAO = 30
LIMITE_CONTEUDO_PADRAO = 4000
LIMITE_MAXIMO_INTEGRAL = 30_000  # tecto de caracteres por artigo mesmo com licença «integral» (mantém a base pequena)

POLITICA_PADRAO: dict[str, Any] = {
    "dominio": "",
    "uso": "resumo",
    "licenca_verificada": False,
    "limite_caracteres": 0,
    "verificado_em": None,
    "fundamento": "Sem autorização ou licença verificada: apenas o resumo que a própria fonte publica no feed, com título, fonte e ligação.",
    "limitacoes": "Reprodução do artigo (integral ou em excerto) não autorizada até verificação.",
    "revisao_humana": True,
    "observacoes": "Verificar condições de utilização, direitos reservados e eventual licença de reutilização.",
}

POLITICA_FONTES: dict[str, dict[str, Any]] = {
    "TEK SAPO": {"dominio": "tek.sapo.pt", "observacoes": "Imprensa (art. 188.º-A CDADC / art. 15.º Dir. 2019/790): verificar termos do SAPO/Altice."},
    "Pplware": {"dominio": "pplware.sapo.pt", "observacoes": "Verificar termos do blogue/SAPO."},
    "ECO": {"dominio": "eco.sapo.pt", "observacoes": "Imprensa: verificar termos e direitos reservados."},
    "Jornal de Negócios": {"dominio": "jornaldenegocios.pt", "observacoes": "Imprensa com conteúdos pagos: verificar termos."},
    "Público": {"dominio": "publico.pt", "observacoes": "Imprensa com paywall: verificar termos."},
    "Dinheiro Vivo": {"dominio": "dinheirovivo.pt", "observacoes": "Imprensa: verificar termos."},
    "Observador": {"dominio": "observador.pt", "observacoes": "Imprensa com área reservada: verificar termos."},
    "IT Security": {"dominio": "itsecurity.pt", "observacoes": "Sem feed RSS: recolha por página HTML; verificar termos e robots."},
    "FCT": {"dominio": "fct.pt", "observacoes": "Entidade pública: verificar se há licença de reutilização de informação do setor público."},
    "ANI": {"dominio": "ani.pt", "observacoes": "Entidade pública: verificar licença de reutilização."},
    "Portal do Governo": {"dominio": "portugal.gov.pt", "observacoes": "Verificar condições de utilização e licença de reutilização de conteúdos."},
}


def politica_fonte(nome: str) -> dict[str, Any]:
    """Política efetiva da fonte. Sem fundamento verificável, o uso volta sempre a «resumo»."""
    politica = {**POLITICA_PADRAO, **POLITICA_FONTES.get(nome, {})}
    if politica["uso"] not in USOS_PERMITIDOS:
        politica["uso"] = "resumo"
    if politica["uso"] != "resumo" and not (
        politica["licenca_verificada"] and politica["verificado_em"] and str(politica["fundamento"]).strip()
        and politica["fundamento"] != POLITICA_PADRAO["fundamento"]
    ):
        logger.warning("Fonte %s: uso %r sem licença verificada; a usar apenas o resumo.", nome, politica["uso"])
        politica["uso"] = "resumo"
    return politica

# ---------------------------------------------------------------------------
# Tópicos e palavras-chave (em português, sem acentos e em minúsculas)
# ---------------------------------------------------------------------------
# O texto é comparado sem acentos e sem distinção de maiúsculas.
# Termos com o prefixo "=" distinguem maiúsculas de minúsculas e são comparados
# com o texto original (evita confundir "IA" com o verbo "ia").
# Os termos só são reconhecidos como palavras inteiras.

TOPICOS: dict[str, list[str]] = {
    "Telecomunicações": [
        "telecomunicacoes", "telecomunicacao", "telecom", "telecoms",
        "operador de telecomunicacoes", "operadores de telecomunicacoes",
        "operadora de telecomunicacoes", "operadoras de telecomunicacoes",
        "operadores moveis", "operadora movel", "operadoras moveis", "operador movel",
        "operador virtual", "mvno", "banda larga", "ftth",
        "roaming", "rede movel", "redes moveis", "rede fixa", "redes fixas",
        "internet fixa", "internet movel", "servico movel", "servicos moveis",
        "comunicacoes moveis", "comunicacao movel", "mobile communications",
        "operador alternativo", "operadores alternativos", "portabilidade do numero",
        "portabilidade de numero", "portabilidade de numeros", "tarifa social de internet",
        "cobertura movel", "falha de rede", "falhas de rede", "fusao de operadores",
        "gpon", "xgs-pon", "esim", "e-sim", "tdt", "televisao digital terrestre",
    ],
    "5G e 6G": [
        "5g", "6g", "5g advanced", "5g-advanced", "5g sa", "redes 5g", "rede 5g",
        "open ran", "o-ran", "ai-ran", "network slicing", "massive mimo",
        "ondas milimetricas", "mmwave", "3gpp", "imt-2030", "imt 2030",
        "redes privadas 5g", "rede privada 5g", "redes privadas moveis", "5g standalone",
        "cobertura 5g", "antenas 5g", "cellnex", "vran", "redes nao terrestres",
        "non-terrestrial networks", "700 mhz", "3,6 ghz", "26 ghz",
    ],
    "Inteligência Artificial": [
        "inteligencia artificial", "=IA", "=AI", "ia generativa", "ai generativa",
        "aprendizagem automatica", "machine learning", "aprendizagem profunda",
        "deep learning", "modelo de linguagem", "modelos de linguagem",
        "grandes modelos de linguagem", "llm", "llms", "chatgpt", "openai",
        "anthropic", "redes neuronais", "regulamento da ia", "ai act",
        "gigafabrica de ia", "gigafabricas de ia", "fabrica de ia", "fabricas de ia",
        "ia soberana", "regulamento europeu da ia", "ai factory", "ai factories",
        "modelo de ia", "modelos de ia", "deepseek", "mistral ai",
        "microsoft copilot", "github copilot", "google gemini", "supercomputador",
        "supercomputadores", "computacao de alto desempenho", "=HPC", "eurohpc",
        "deucalion", "chips act",
    ],
    "Agentes de IA": [
        "agentes de ia", "agente de ia", "agentes de ai", "agente de ai",
        "agentes de inteligencia artificial", "agente de inteligencia artificial",
        "agentes autonomos", "agente autonomo", "ia agentica", "ai agentica",
        "agentic", "agentic ai", "ai agents", "ai agent", "agentes inteligentes",
        "agentes autonomos de ia", "model context protocol", "sistemas multiagente",
        "orquestracao de agentes", "assistentes de ia", "assistente de ia", 
    ],
    "Carros Conectados": [
        "carros conectados", "carro conectado", "veiculos conectados", "veiculo conectado",
        "automoveis conectados", "automovel conectado", 
        "conectividade automovel", "conectividade veicular",
        "v2x", "c-v2x", "c-its", "ecall", "mobilidade conectada",
        "internet dos veiculos", "veiculo definido por software", "software-defined vehicle",
        "software defined vehicle", 
        "sistemas de transporte inteligentes",
        "sistemas inteligentes de transporte", "=LiDAR", "=LIDAR",
    ],
    "Carros Autónomos": [
        "carros autonomos", "carro autonomo", "veiculos autonomos", "veiculo autonomo",
        "automoveis autonomos", "automovel autonomo", "conducao autonoma",
        "conducao automatizada", "veiculos automatizados", "robotaxi", "robotaxis",
        "taxis autonomos", "autocarros autonomos", "camioes autonomos", "waymo",
        "full self-driving", "full self driving", "veiculos sem condutor", "veiculo sem condutor",
        "carros sem condutor", "carro sem condutor", "autocarro sem condutor",
        "autocarros sem condutor", "robo-taxi", "robo-taxis", "navetes autonomas",
        "shuttle autonomo", "shuttles autonomos", "zoox", "apollo go",
        "=LiDAR", "=LIDAR", "sensores lidar", "sensor lidar", "tecnologia lidar",
    ],
    "Cloud": [
        "cloud", "computacao em nuvem", "servicos de nuvem", "nuvem publica",
        "nuvem privada", "nuvem soberana", "multicloud", "multi-cloud",
        "hiperescala", "hiperescalar", "hyperscaler", "hyperscalers",
        "=AWS", "google cloud", "oracle cloud", "saas", "iaas", "paas",
        "armazenamento em nuvem", "computacao na nuvem", "migracao para a nuvem",
        "nuvem hibrida", "data act",
        "ovhcloud", "alibaba cloud",
        "neocloud", "neo cloud", "neo-cloud", "nuvem de gpu", "gpu cloud", "gpuaas",
        "gpu-as-a-service", "gpu as a service",
    ],
    "Data Centers": [
        "data center", "data centers", "data centre", "data centres", "datacenter",
        "datacenters", "centro de dados", "centros de dados",
        "centro de processamento de dados", "campus de dados", "start campus",
        "equinix", "digital realty", "colocation", "ponto de troca de trafego",
        "pontos de troca de trafego", "ixp", "ixps",
        "fabrica de ia", "fabricas de ia", "fabrica de inteligencia artificial",
        "fabricas de inteligencia artificial", "fabrica de ai", "fabricas de ai",
        "gigafabrica de ia", "gigafabricas de ia", "gigafabrica de inteligencia artificial",
        "ai factory", "ai factories", "ai gigafactory", "ai gigafactories",
        "centro de dados de ia", "centros de dados de ia", "data center de ia", "data centers de ia",
        "ai data center", "ai data centre", "campus de ia",
    ],
    "Quântica": [
        "computacao quantica", "computador quantico", "computadores quanticos",
        "quantum", "qubit", "qubits", "tecnologia quantica", "processador quantico",
        "processadores quanticos", "sistemas quanticos", "supremacia quantica",
        "vantagem quantica", "ibm quantum",
        "criptografia quantica", "comunicacoes quanticas",
        "comunicacao quantica", "internet quantica", "distribuicao quantica de chaves",
        "qkd", "tecnologias quanticas", "sensores quanticos",
        "quantica", "quantico", 
        "hardware quantico", "rede quantica",
        # Sensorização, computação e comunicação quânticas
        "sensorizacao quantica", "deteccao quantica", "metrologia quantica", "magnetometro quantico",
        "magnetometros quanticos", "giroscopio quantico", "giroscopios quanticos", "relogio quantico",
        "relogios quanticos", "navegacao quantica", "imagem quantica", "radar quantico",
        "annealing quantico", "recozimento quantico", "porta quantica", "portas quanticas", "emaranhamento quantico",
        "repetidor quantico", "repetidores quanticos", "teletransporte quantico", "satelite quantico", "satelites quanticos", "redes quanticas", "computacao quantica fotonica",
    ],
    "Cabos Submarinos": [
        "cabo submarino", "cabos submarinos", "cabo de fibra submarino",
        "cabos de fibra submarinos", "cabo de fibra optica submarino",
        "cabos de fibra optica submarinos", "cabo subaquatico", "cabos subaquaticos",
        "cabo transatlantico", "cabos transatlanticos", "cabo intercontinental",
        "ellalink", "equiano", "2africa", "estacao de amarracao",
        "estacoes de amarracao", "landing station", "infraestruturas submarinas",
        "ponto de amarracao", "pontos de amarracao", "hub de cabos submarinos",
        "ligacao submarina", "ligacoes submarinas", "sabotagem de cabos",
    ],
    "Redes e Comunicações Eletrónicas": [
        "comunicacoes eletronicas", "comunicacoes electronicas", "redes de comunicacoes",
        "rede de comunicacoes", "servicos de comunicacoes", "servico universal",
        "regulador das comunicacoes", "regulador das telecomunicacoes",
        "autoridade nacional de comunicacoes", "anacom", "berec", "cept", "etsi",
        "espetro radioeletrico", "espectro radioeletrico", "espetro radioelectrico",
        "espectro radioelectrico", "gestao do espetro", "gestao do espectro",
        "atribuicao de espetro", "atribuicao de espectro", "licencas de espetro",
        "licencas de espectro", "bandas de frequencias", "radiofrequencia", "radiofrequencias",
        "leilao de espetro", "leilao do espetro", "leilao de espectro", "leilao do espectro",
        "codigo europeu das comunicacoes eletronicas",
        "digital networks act", "neutralidade da rede", "infraestruturas de telecomunicacoes",
        "infraestrutura de telecomunicacoes", "gigabit infrastructure act",
        "redes de muito alta capacidade", "partilha de infraestruturas", 
        "estacoes base", "operador de rede", "operadores de rede", "obrigacoes de cobertura",
        "infraestruturas aptas",
        "qualidade de servico das comunicacoes",
        # Direitos de utilização de espectro e frequências (DUER)
        "duer", "direito de utilizacao de espetro", "direito de utilizacao de espectro",
        "direitos de utilizacao de espetro", "direitos de utilizacao de espectro",
        "direito de utilizacao de frequencias", "direitos de utilizacao de frequencias",
        "direito de utilizacao de radiofrequencias", "direitos de utilizacao de radiofrequencias",
        "direito de utilizacao do espetro", "direito de utilizacao do espectro",
        "direitos de utilizacao do espetro", "direitos de utilizacao do espectro",
        "titulo de utilizacao de frequencias", "titulos de utilizacao de frequencias",
        "licenca de utilizacao de frequencias", "licencas de utilizacao de frequencias",
        "autorizacao de utilizacao de frequencias", 
        "espetro de frequencias", "espectro de frequencias",
        "espetro de radiofrequencias", "espectro de radiofrequencias", "banda de frequencias",
        "concurso de espetro", "concurso de espectro", "renovacao de espetro", "renovacao de espectro",
        # Segurança das redes e serviços
        "seguranca das redes", "seguranca de redes", "seguranca da rede",
        "seguranca das comunicacoes", "seguranca das comunicacoes eletronicas",
        "seguranca das redes e servicos de comunicacoes eletronicas",
        "seguranca das redes e dos servicos de comunicacoes eletronicas",
        "seguranca e integridade das redes", "integridade das redes", "resiliencia das redes",
        "resiliencia de redes", "resiliencia das comunicacoes", "seguranca das redes 5g", "seguranca 5g",
        "fornecedores de alto risco", "nis2", "nis 2", "diretiva nis", "diretiva nis2",
        "ciberseguranca das redes", "ciberseguranca nas redes", "ciberseguranca das comunicacoes",
    ],
    "Drones": [
        "drone", "drones", "=UAS", "rpas", "uav", "uavs",
        "veiculos aereos nao tripulados", "veiculo aereo nao tripulado",
        "aeronave nao tripulada", "aeronaves nao tripuladas", "u-space",
        "anti-drone", "antidrone", "antidrones", "entregas por drone", "evtol", "evtols", "taxi aereo eletrico", "taxis aereos eletricos",
        "mobilidade aerea avancada", "sistemas aereos nao tripulados", "sistema aereo nao tripulado",
        "sistemas de aeronaves nao tripuladas", "=sUAS", "=BVLOS", "=LiDAR", "=LIDAR",
    ],
    "Outras Tecnologias Emergentes": [
        
        
        
        "constelacao de satelites", "constelacoes de satelites",
        "internet por satelite", "comunicacoes por satelite", "starlink", "kuiper",
        "iris2", "direct-to-cell", "direct-to-device", "wi-fi 7", "wifi 7",
        "lorawan", "nb-iot", "lte-m", "lpwan", "wi-fi 6e", "wi-fi 6", "wifi 6",
        "internet via satelite", "banda larga por satelite", "satelites de comunicacoes",
        "oneweb", "eutelsat", "amazon leo", 
        
        
        "chave movel digital", "eidas", "carteira digital europeia",
        
        
    ],
}

# Equivalentes em inglês dos termos acima (singular e plural são tratados automaticamente).
TERMOS_EM_INGLES: dict[str, list[str]] = {
    "Telecomunicações": [
        "telecommunications", "telco", "mobile operator", "mobile network", "fixed network",
        "broadband", 
        "virtual operator", "fixed internet", "mobile internet", "mobile service",
        "number portability", "network outage", "digital terrestrial television",
        "operator merger",
    ],
    "5G e 6G": [
        "5g network", "private 5g", "5g coverage", "5g antenna", "5g standalone",
        "millimeter wave", "millimetre wave", "private mobile network",
    ],
    "Inteligência Artificial": [
        "artificial intelligence", "generative ai", "generative artificial intelligence",
        "large language model", "neural network", "ai model", "ai regulation",
        "sovereign ai", "european ai act", "ai gigafactory", "supercomputer",
        "high-performance computing", "high performance computing", 
    ],
    "Agentes de IA": [
        "autonomous agent", "ai assistant", "intelligent agent", "multi-agent system",
        "agent orchestration", "ai copilot",
    ],
    "Carros Conectados": [
        "connected car", "connected vehicle", "vehicle connectivity", "connected mobility",
        "internet of vehicles", "intelligent transport system",
        "intelligent transportation system",
    ],
    "Carros Autónomos": [
        "autonomous car", "autonomous vehicle", "self-driving", "self driving",
        "driverless", "autonomous driving", "automated driving", "autonomous taxi",
        "autonomous bus", "autonomous truck", "autonomous shuttle",
    ],
    "Cloud": ["sovereign cloud"],
    "Data Centers": ["datacentre", "internet exchange point", "internet exchange"],
    "Quântica": [
        "quantum computing", "quantum computer", "quantum technology", "quantum cryptography",
        "quantum communication", "quantum internet", "quantum key distribution",
        "quantum sensor", "quantum sensing", "quantum metrology", "quantum magnetometer",
        "quantum gyroscope", "quantum clock", "quantum navigation", "quantum imaging", "quantum radar",
        "quantum annealing", "quantum gate", "quantum entanglement", "quantum repeater", "quantum teleportation",
        "quantum network", "quantum satellite", "quantum processor",
        "quantum supremacy", "quantum advantage",
    ],
    "Cabos Submarinos": [
        "submarine cable", "undersea cable", "subsea cable", "transatlantic cable",
        "cable landing", "cable sabotage", "subsea infrastructure", "undersea infrastructure",
    ],
    "Redes e Comunicações Eletrónicas": [
        "electronic communications", 
        "universal service", "communications regulator", "telecom regulator",
        "radio spectrum", "spectrum auction", "spectrum allocation", "spectrum licence",
        "spectrum license", "spectrum management", "very high capacity network",
        "infrastructure sharing", "european electronic communications code",
        "net neutrality", "base station", "telecom tower", 
        "network operator", "coverage obligation",
    ],
    "Drones": [
        "unmanned aerial vehicle", "unmanned aircraft", "drone delivery", "air taxi",
        "urban air mobility", "advanced air mobility",
    ],
    "Outras Tecnologias Emergentes": [
        
        "satellite constellation", "satellite internet",
        "satellite communications", "satellite broadband", 
        
        "low-power wide-area",
    ],
}
for _nome, _extra in TERMOS_EM_INGLES.items():
    TOPICOS[_nome].extend(t for t in _extra if t not in TOPICOS[_nome])

# Desligamento de redes antigas (2G, 3G, cobre): pertence a Telecomunicações e a Redes e Comunicações Eletrónicas.
# "phase-out" ou "switch-off" isolados existem noutros setores; por isso só contam junto a "rede" ou a uma tecnologia.
TERMOS_DESLIGAMENTO = [
    "2g", "3g", "desligamento da rede", "desligamento das redes", "desligamento de redes",
    "desativacao da rede", "desativacao das redes", "desativacao de redes",
    "encerramento da rede", "encerramento das redes", "encerramento de redes",
    "descontinuacao da rede", "descontinuacao das redes", "redes legadas", "rede legada",
    "rede de cobre", "redes de cobre", "desligamento do cobre", "desativacao do cobre", "fim do cobre",
    "phase-out da rede", "phase-out das redes", "phase-out de redes", "phase out da rede",
    "phaseout da rede", "switch-off da rede", "switch-off das redes", "switch-off de redes",
    "switch-off do cobre", "switch off da rede", "switchoff da rede", "sunset da rede",
    "sunset das redes", "shutdown da rede", "shutdown das redes",
    "network switch-off", "network switch off", "network shutdown", "network sunset",
    "network phase-out", "legacy network", "copper network", "copper switch-off",
    "copper retirement", "legacy network retirement",
]
for _nome in ("Telecomunicações", "Redes e Comunicações Eletrónicas"):
    TOPICOS[_nome].extend(t for t in TERMOS_DESLIGAMENTO if t not in TOPICOS[_nome])

# Categoria "Espaço": só temas do setor espacial (satélites, lançadores, estações espaciais, licenças de lançamento).
# Só frases específicas; "espaço" ou "espacial" isolados são ambíguos (espaço público, análise espacial).
TOPICOS["Espaço"] = [
    "setor espacial", "sector espacial", "industria espacial", "economia espacial", "mercado espacial",
    "agencia espacial", "agencia espacial europeia", "agencia espacial portuguesa", "portugal space",
    "=ESA", "=NASA", "spacex", "blue origin", "rocket lab", "arianespace", "ariane 6", "ariane 5",
    "vega-c", "starship", "falcon 9", "programa artemis", "missao artemis", "isar aerospace",
    "estacao espacial", "estacoes espaciais", "estacao espacial internacional", "nave espacial",
    "naves espaciais", "sonda espacial", "sondas espaciais", "telescopio espacial",
    "foguetao", "foguetoes", "lancador espacial", "lancadores espaciais",
    "microlancador", "microlancadores", "veiculo lancador", "veiculos lancadores", "foguete espacial",
    "lancamento de satelite", "lancamento de satelites",
    "lancamento espacial", "lancamentos espaciais", "lancamento orbital", "lancamentos orbitais",
    "estacao de lancamento", "estacoes de lancamento", "centro de lancamento", "base de lancamento de foguetes",
    "plataforma de lancamento de foguetes", "plataforma de lancamento de satelites", "licenca de lancamento espacial",
    "licencas de lancamento espacial", "licenca de lancamento orbital", "autorizacao de lancamento espacial",
    "operador de lancamento espacial", "servicos de lancamento espacial", "servicos de lancamento de satelites", 
     
    
     
    "atlantic spaceport", "azores space", "centro espacial", "base espacial",
    "microssatelite", "microssatelites", "nanossatelite", "nanossatelites",
    "minissatelite", "minissatelites", "cubesat", "cubesats",
    "constelacao de satelites", "constelacoes de satelites", 
    "sistema galileo", "egnos", "programa copernicus",
    "observacao da terra", "starlink", "oneweb", "iris2", "amazon leo", "kuiper",
    "orbita terrestre baixa",
    "orbita baixa", "orbitas terrestres",
    "astronauta", "astronautas", "missao espacial", "missoes espaciais", "exploracao espacial",
    "voo espacial", "voos espaciais", "viagem espacial", "viagens espaciais", "turismo espacial",
    "detritos espaciais", "lixo espacial", "sustentabilidade espacial", "trafego espacial",
    "gestao do trafego espacial", "seguranca espacial", "programa espacial", "programas espaciais",
    "politica espacial", "estrategia espacial", "lei do espaco", "regime juridico da atividade espacial",
    "atividade espacial", "atividades espaciais", "operador espacial", "operadores espaciais",
    "licenciamento espacial", "licenca espacial", "licencas espaciais", "operacoes espaciais",
    "tecnologia espacial", "tecnologias espaciais", "empresa espacial", "empresas espaciais",
    "start-up espacial", "startup espacial", "startups espaciais", "defesa espacial",
    # Inglês
    "space industry", "space sector", "space economy", "space market", "space agency",
    "european space agency", "space station", "space mission", "space missions", "space exploration",
    "space launch", "space launches", "launch site", "launch sites", "launch vehicle", "launch vehicles",
    "launch service", "launch services", "launch licence", "launch license", "launch licences",
    "launch licenses", "launch authorisation", "launch authorization", "launch operator",
    "rocket launch", "rocket launches", "orbital launch", "reusable rocket", "reusable rockets",
    "space port", "small satellite", "small satellites", "smallsat",
    "satellite constellation", "satellite launch", "earth observation",
    "low earth orbit",
    "spacecraft", "spaceflight", "space flight", "astronaut",
    "astronauts", "space tourism", "space debris", "space junk", "space traffic", "space law",
    "space policy", "space tech", "space technology", "space program", "space programme",
    "space company", "space companies", "space startup", "space start-up", "lunar mission",
    "moon mission", "mars mission", "space telescope", "space probe", "space security",
]

# Termos acrescentados depois da primeira versão (as listas acima ficam como estão).
def _acrescentar(topico: str, termos: list[str]) -> None:
    TOPICOS[topico].extend(t for t in termos if t not in TOPICOS[topico])


_acrescentar("Telecomunicações", [
    # Operadores e mercado
    "vodafone", "altice", "digi portugal", "=MEO", "=NOS", "=DIGI", "operador de rede movel",
    "mercado das comunicacoes", "mercado de telecomunicacoes", "setor das telecomunicacoes",
    "sector das telecomunicacoes", "setor das comunicacoes", "pacotes convergentes", "tarifarios moveis",
    "telecom operator", "telecom market", "telecom sector", "mobile carrier",
    # Rede e acesso
    "satcom", "backhaul", "fronthaul", "rede de fibra", "redes de fibra", "cabo coaxial",
    "torre de telecomunicacoes", "torres de telecomunicacoes", 
    
    
    "siresp",
    "ericsson", "nokia", "huawei", "zte", "equipamentos de telecomunicacoes",
    # Fraude em comunicações
    "caller id spoofing",
    # Espectro e bandas
    "800 mhz", "900 mhz", "1800 mhz", "2100 mhz", "2600 mhz", "3,5 ghz", "3.5 ghz", "6 ghz", "banda dos 6 ghz",
    # Outras tecnologias emergentes
    
])
_acrescentar("5G e 6G", [
    "radio access network", "rede de acesso radio", "redes de acesso radio", "small cells", "5g core", "core 5g",
    "banda dos 3,5 ghz", "leilao 5g",
])
_acrescentar("Inteligência Artificial", [
    "visao computacional", "computer vision", "foundation model", "foundation models",
    "modelo de fundacao", "modelos de fundacao", "nvidia", "grok",
])
_acrescentar("Agentes de IA", ["=MCP", "agentes de codigo", "coding agent", "coding agents"])
_acrescentar("Carros Conectados", ["=ADAS", "v2v", "v2i", "sistemas avancados de assistencia a conducao"])
_acrescentar("Carros Autónomos", ["tesla fsd", "nivel 4 de autonomia", "level 4 autonomy"])
_acrescentar("Cabos Submarinos", [
    "cabo marinho", "cabos marinhos", "cabo oceanico", "cabos oceanicos",
    "cabo submerso", "cabos submersos", "amarracao de cabos", "cabo do atlantico", "medusa cable",
])
_acrescentar("Drones", [
    "aeronave pilotada remotamente", "aeronaves pilotadas remotamente", "sistemas de aeronaves pilotadas remotamente",
    "contra-drone", "contra-drones", "counter-drone", "counter-uas", "=C-UAS", "mobilidade aerea urbana",
    "remotely piloted aircraft", "remotely piloted aircraft system", "tekever",
])
_acrescentar("Espaço", [
    "neuraspace", "lusospace", "missao lunar", "missoes lunares", "alunagem", "missao a marte",
    "forca espacial", "space force", "spacetech", "space-tech", "newspace", "asteroide", "asteroides",
    "lunar lander", "moon landing", "mars rover", 
])

# ---------------------------------------------------------------------------
# Categoria «Carros»: junta «Carros Conectados» e «Carros Autónomos»
# ---------------------------------------------------------------------------
# Termos FORTES identificam por si só tecnologia automóvel.
# Termos FRACOS (sensores, software, visão) só contam se o texto também falar de automóveis.
# MARCAS só contam se o texto falar de automóveis E de condução autónoma, conectividade ou assistência à condução.
CARROS_FORTES_NOVOS = [
    # Autónomos
    "carros autonomos", "carro autonomo", "veiculos autonomos", "veiculo autonomo", "automoveis autonomos",
    "conducao autonoma", "conducao automatizada", "conducao assistida", "sistemas de conducao autonoma",
    "robotaxi", "robotaxis", "robotaxi technology", "taxis autonomos", "carros sem condutor", "veiculos sem condutor",
    "camaras de conducao autonoma", "cameras de conducao autonoma", "full self-driving", "full self driving",
    "autonomous cars", "autonomous car", "autonomous vehicles", "autonomous vehicle", "self-driving cars",
    "self-driving car", "self-driving vehicles", "self-driving vehicle", "self-driving trucks", "driverless cars",
    "driverless car", "autonomous driving", "automated driving", "tesla fsd", "tesla autopilot",
    "waymo", "zoox", "apollo go", "mobileye", "nvidia drive", "drive orin", "drive thor", "pony.ai", "weride",
    "autonomous taxi", "autonomous bus", "autonomous truck", "autonomous shuttle",
    # Conectados e inteligentes
    "carros conectados", "carro conectado", "veiculos conectados", "veiculo conectado", "automoveis conectados",
    "carros inteligentes", "carro inteligente", "automoveis inteligentes", "veiculos inteligentes",
    "conectividade automovel", "conectividade veicular", "mobilidade conectada", "mobilidade autonoma",
    "internet dos veiculos", "veiculo definido por software", "veiculos definidos por software",
    "sistemas operativos automoveis", "sistema operativo automovel", "sistemas avancados de assistencia a conducao",
    "connected cars", "connected car", "connected vehicles", "connected vehicle", "smart cars", "smart car",
    "software-defined vehicle", "software-defined vehicles", "software defined vehicle",
    "vehicle-to-everything", "vehicle-to-vehicle", "vehicle-to-infrastructure", "car operating system",
    "car operating systems", "vehicle operating system", "advanced driver assistance", "advanced driver-assistance",
    "=V2X", "=V2V", "=V2I", "=C-V2X", "=ADAS", "c-its", "ecall",
    # Sensores automóveis (já específicos)
    "radar automovel", "radares automoveis", "automotive radar", "automotive lidar", "lidar automovel",
    "4d imaging radar", "radar de imagem 4d",
]
# Sensores e câmaras específicos do automóvel: contam sempre (o próprio termo já indica contexto automóvel).
CARROS_FORTES_NOVOS += [
    "fsd supervisionado", "fsd supervised", "fsd (supervised)", "tesla autopilot", "radar de 4d", "radares 4d",
    "radar de imagem 4d", "sensores para automoveis", "sensores para carros", "sensores para veiculos",
    "sensors for cars", "sensors for vehicles", "radar para automoveis", "radar for cars", "lidar para automoveis",
    "lidar para carros", "lidar for cars", "camaras para automoveis", "cameras for cars", "cameras for vehicles",
]
CARROS_FORTES_NOVOS += [
    "camiao autonomo", "camioes autonomos", "autocarro autonomo", "taxi autonomo", "camiao sem condutor",
    "camioes sem condutor", "autonomous trucks", "autonomous buses", "autonomous taxis", "driverless truck",
    "driverless trucks", "driverless car", "driverless cars", "self-driving car", "self-driving cars",
    "self-driving bus", "self-driving taxi", "autonomous cars", "autonomous driving system",

    "radar automovel", "radares automoveis", "radar automotivo", "automotive radar", "lidar automovel",
    "lidar automotivo", "automotive lidar", "camara automovel", "camaras automoveis", "automotive camera",
    "automotive cameras", "vehicle camera", "vehicle cameras", "camaras de visao automovel", "camera automotiva",
    "camaras automotivas", "sensores automotivos", "sensor automotivo", "automotive sensors", "automotive sensor",
]
CARROS_FRACOS = [
    # Sensores genéricos: só contam se o texto tiver também contexto automóvel (carro, veículo, condutor...)
    "camara de bordo", "camaras de bordo", "camera de bordo", "onboard camera", "onboard cameras",
    "camaras de visao", "camera de visao", "camera-based", "radar de longo alcance", "long-range radar",
    "radar de curto alcance", "short-range radar", "radar de ondas milimetricas", "millimeter-wave radar",
    "sensores de radar", "sensor de radar", "radar sensor", "radar sensors", "surround view",
    "=LiDAR", "=LIDAR", "lidar sensor", "lidar sensors", "sensor lidar", "sensores lidar", "tecnologia lidar",
    "radar 4d", "4d radar", "computer vision", "visao computacional", "sensor fusion", "fusao de sensores",
    "ultrasonic sensors", "sensores ultrassonicos", "perception system", "perception systems",
    "sistemas de percecao", "object detection", "deteccao de objetos", "autonomous navigation", "navegacao autonoma",
    "ota updates", "ota update", "atualizacoes ota", "atualizacoes over-the-air", "over-the-air updates",
    "=FSD", "=SDV", "level 2", "level 3", "level 4", "level 5", "nivel 2", "nivel 3", "nivel 4", "nivel 5",
    "level 4 autonomy", "nivel 4 de autonomia",
]
CARROS_MARCAS = [
    "tesla", "google", "mercedes-benz", "mercedes", "bmw", "volvo", "hyundai", "kia", "volkswagen", "=VW",
    "stellantis", "renault", "toyota", "honda", "nissan", "ford", "general motors", "porsche", "audi", "byd",
    "xpeng", "polestar", "uber", "baidu", "cruise",
]
_CTX_AUTOMOVEL = re.compile(
    r"(?<![a-z0-9])(carro|carros|automovel|automoveis|veiculo|veiculos|viatura|viaturas|car|cars|vehicle|vehicles|"
    r"automotive|condutor|condutores|conducao|motorista|autocarro|autocarros|"
    r"camiao|camioes|truck|trucks|taxi|taxis|robotaxi|robotaxis)(?![a-z0-9])"
)
# Veto: se o texto fala de veículos não rodoviários e não tem nenhuma palavra rodoviária explícita, não é «Carros».
_NAO_RODOVIARIO = re.compile(
    r"(?<![a-z0-9])(aereo|aereos|aerial|aerotransportad[oa]s?|drone|drones|uav|uas|subaquatic[oa]s?|submarin[oa]s?|underwater|"
    r"navio|navios|ship|ships|maritim[oa]s?|naval|navais|superficie|comboio|comboios|metro|train|trains|ferroviari[oa]s?|"
    r"agv|agvs|armazem|armazens|warehouse|warehouses|logistic[oa]s?|agricol[oa]s?|tractor|tractores|trator|tratores|"
    r"aspirador|aspiradores|robo|robos|robot|robots|veiculo de lancamento|launch vehicle|espacial|spacecraft|satelite|satelites)(?![a-z0-9])"
)
_RODOVIARIO = re.compile(
    r"(?<![a-z0-9])(carro|carros|automovel|automoveis|car|cars|automotive|rodoviari[oa]s?|estrada|estradas|road|roads|"
    r"camiao|camioes|truck|trucks|autocarro|autocarros|taxi|taxis|robotaxi|robotaxis|robo-taxi|robo-taxis|condutor|condutores|"
    r"motorista|passageiros|viatura|viaturas|adas|v2x|c-its|waymo|zoox)(?![a-z0-9])"
)
# Frases em que «veículo» não é um carro (investimento, lançamento espacial, etc.): retiradas antes de testar o contexto.
_VEICULO_NAO_CARRO = re.compile(
    r"(veiculo de investimento|veiculos de investimento|investment vehicle|special purpose vehicle|veiculo de proposito especial|"
    r"veiculo de lancamento|launch vehicle|veiculo aereo|veiculos aereos|unmanned aerial vehicle|veiculo subaquatico|"
    r"veiculos subaquaticos|underwater vehicle|veiculo de superficie)"
)
_CTX_TECNOLOGIA_CARRO = re.compile(
    r"(?<![a-z0-9])(autonom(?!i)|self-driving|self driving|driverless|autopilot|full self|adas|assistencia a conducao|"
    r"driver assist|driver-assist|v2x|v2v|v2i|conectad|connected|robotaxi|robo-taxi|"
    r"software-defined|definido por software|conducao assistida|conducao automatizada|automated driving|"
    r"over-the-air|carro inteligente|smart car|fsd)"
)
# Segundo sinal para termos fracos (sensores, câmaras, «nível 3»...): tem de haver algo de condução/segurança rodoviária perto.
_CTX_CONDUCAO = re.compile(
    r"(?<![a-z0-9])(autonom(?!i)|assist|adas|travag|brak|colis|collision|self-driving|self driving|driverless|autopilot|"
    r"piloto automatico|sinais de transito|traffic sign|conducao|faixa de rodagem|lane|ultrapass|estacionamento|parking|"
    r"peao|peoes|pedestrian|obstaculo|obstacle|over-the-air|conectad|connected|v2x|v2v|v2i|"
    r"nivel [2-5]|level [2-5]|automotive|automovel|automoveis|autocarro|robotaxi)"
)

_merged = []
for _n in ("Carros Conectados", "Carros Autónomos"):
    _merged.extend(TOPICOS[_n])
_merged.extend(CARROS_FORTES_NOVOS)
_fracos_norm = {t.lower() for t in CARROS_FRACOS}
_marcas_norm = {t.lower() for t in CARROS_MARCAS}
_carros_fortes: list[str] = []
for _t in _merged:
    if _t.lower() in _fracos_norm or _t.lower() in _marcas_norm or _t in _carros_fortes:
        continue
    _carros_fortes.append(_t)

_novo_dicionario: dict[str, list[str]] = {}
for _nome, _termos in TOPICOS.items():
    if _nome == "Carros Autónomos":
        continue
    _novo_dicionario["Carros" if _nome == "Carros Conectados" else _nome] = _carros_fortes if _nome == "Carros Conectados" else _termos
TOPICOS.clear()
TOPICOS.update(_novo_dicionario)

# Fusão: "Outras - Telecomunicações" reúne os termos de "Telecomunicações", "Redes e Comunicações Eletrónicas"
# e "Outras Tecnologias Emergentes". As listas acima ficam como estão; só os nomes passam a ser um.
CATEGORIA_OUTRAS_TELECOM = "Outras - Telecomunicações"
_NOMES_FUNDIDOS = ("Telecomunicações", "Redes e Comunicações Eletrónicas", "Outras Tecnologias Emergentes")
NOMES_RENOMEADOS = {nome: CATEGORIA_OUTRAS_TELECOM for nome in _NOMES_FUNDIDOS}
NOMES_RENOMEADOS.update({"Carros Conectados": "Carros", "Carros Autónomos": "Carros"})
NOMES_RENOMEADOS["Computação Quântica"] = "Quântica"
TOPICOS[CATEGORIA_OUTRAS_TELECOM] = []
for _nome in _NOMES_FUNDIDOS:
    TOPICOS[CATEGORIA_OUTRAS_TELECOM].extend(
        t for t in TOPICOS.pop(_nome) if t not in TOPICOS[CATEGORIA_OUTRAS_TELECOM]
    )

# Um tópico que implica outro (os cabos submarinos são infraestrutura de comunicações eletrónicas).
TOPICOS_IMPLICADOS: dict[str, list[str]] = {
    "Cabos Submarinos": [CATEGORIA_OUTRAS_TELECOM],
}

# Em caso de pontuação igual, vence o tópico mais específico (primeiro da lista).
PRIORIDADE_TOPICOS = [
    "Agentes de IA", "Cabos Submarinos", "Espaço", "Carros",
    "Quântica", "Data Centers", "Cloud", "Drones", "Inteligência Artificial",
    "5G e 6G", CATEGORIA_OUTRAS_TELECOM,
]

# Ordem de apresentação nos filtros: "Outras - Telecomunicações" é sempre a última, com "Espaço" logo antes.
_FIM_DA_ORDEM = ["Espaço", CATEGORIA_OUTRAS_TELECOM]
ORDEM_CATEGORIAS = [n for n in TOPICOS if n not in _FIM_DA_ORDEM] + _FIM_DA_ORDEM

# Títulos de produto, promoções e entretenimento são ignorados, exceto se o
# próprio título tiver um termo de tópico.
RUIDO_TITULO = re.compile(
    r"(?<![a-z0-9])(review|analise|unboxing|promocao|promocoes|desconto|descontos|"
    r"oferta|ofertas|black friday|cupao|cupoes|smartphone|smartphones|telemovel|"
    r"telemoveis|tablet|portatil|auscultadores|videojogo|videojogos|gaming|"
    r"playstation|xbox|nintendo|netflix|disney|futebol|benfica|sporting)(?![a-z0-9])"
)

# Referências que indicam acontecimentos em Portugal (sem acentos, minúsculas).
# Foram retirados termos que existem noutros contextos: "governo" (qualquer governo),
# "porto" (porto de mar, vinho), "faro" (farol, em espanhol e italiano), "madeira"
# (o material), "vodafone" e "altice" (operadores de outros países) e "web summit"
# (realiza-se também fora de Portugal).
TERMOS_CONTEXTO_PT = [
    # País, língua e instituições
    "portugal", "portugues", "portuguesa", "portugueses", "portuguesas", "portuguese",
    "lusitano", "republica portuguesa", "assembleia da republica", "diario da republica",
    "conselho de ministros", "autoridade da concorrencia", "portugal 2030",
    "plano de recuperacao e resiliencia",
    # Reguladores, agências e entidades públicas
    "anacom", "cncs", "centro nacional de ciberseguranca", "cnpd",
    "comissao nacional de protecao de dados", "fundacao para a ciencia e a tecnologia",
    "agencia nacional de inovacao", "agencia para a modernizacao administrativa",
    "iapmei", "instituto de telecomunicacoes", "fccn", "siresp",
    # Operadores e empresas nacionais
    "dstelecom", "altice labs", "outsystems", "feedzai", "talkdesk", "farfetch",
    "sword health", "unbabel", "priberam", "critical software", "codacy", "glintt",
    # Universidades, investigação e infraestruturas
    "inesc", "iscte", "instituto superior tecnico", "universidade do porto",
    "universidade de lisboa", "universidade de coimbra", "universidade do minho",
    "laboratorio iberico internacional de nanotecnologia", "deucalion",
    "start campus", "ellalink",
    # Localidades (inclui pontos de amarração de cabos submarinos)
    "lisboa", "lisbon", "oporto", "braga", "coimbra", "aveiro", "algarve", "evora",
    "setubal", "leiria", "viseu", "guimaraes", "covilha", "acores", "azores", "funchal",
    "ponta delgada", "sines", "sesimbra", "seixal", "carcavelos", "cascais", "oeiras",
    "sintra", "almada", "amadora", "matosinhos", "peniche", "santarem", "braganca",
    "viana do castelo", "castelo branco", "portalegre", "vila real", "beja",
    "alentejo", "minho", "douro",
]
# Termos que só valem com esta grafia exata (evita confundir "nos", "meo", "porto",
# "faro" ou "madeira" com palavras comuns).
TERMOS_CONTEXTO_PT_SENSIVEIS = [
    "NOS", "MEO", "Digi", "DIGI", "IPMA", "ANEPC", "CTT", "FCT", "AdC",
    "Porto", "Faro", "Madeira",
]

# Etiquetas do feed que indicam conteúdo reservado.
ETIQUETAS_BLOQUEIO = {
    "premium", "eco premium", "exclusivo", "exclusivo assinantes", "assinantes",
    "subscritores", "conteudo pago", "conteudo premium",
}

# Frases de bloqueio (sem acentos, minúsculas).
# Nível 1: procuradas apenas dentro do artigo (<article> ou <main>).
FRASES_BLOQUEIO_ARTIGO = [
    "exclusivo para assinantes", "exclusivo para subscritores", "exclusivo assinantes",
    "exclusivo subscritores", "reservado a assinantes", "reservado a subscritores",
    "reservado para assinantes", "conteudo premium", "artigo premium",
    "acesso exclusivo a assinantes", "acesso reservado a assinantes",
    "artigo bloqueado", "conteudo bloqueado", "subscribers only",
]
# Nível 2: convites explícitos à subscrição, procurados em toda a página útil.
FRASES_BLOQUEIO_PAGINA = [
    "subscreva para continuar", "assine para continuar", "subscreva para ler",
    "assine para ler", "para continuar a ler este artigo", "este artigo e exclusivo",
    "este conteudo e exclusivo", "desbloqueie este artigo", "continue a ler com uma subscricao",
    "torne-se assinante para", "torne-se subscritor para",
]
META_BLOQUEIO = {
    "article:content_tier": {"locked", "premium", "paid", "subscriber", "subscribers"},
    "content_tier": {"locked", "premium", "paid", "subscriber", "subscribers"},
    "premium": {"true", "1", "yes", "sim"},
    "paywall": {"true", "1", "yes", "sim"},
}
URL_DE_BLOQUEIO = re.compile(
    r"(login|signin|sign-in|entrar|subscrev|subscri[cç]|assine|assinatura|paywall|registo|registar)",
    re.I,
)

# ---------------------------------------------------------------------------
# Utilitários de texto
# ---------------------------------------------------------------------------


def sem_acentos(texto: str) -> str:
    """Remove acentos e cedilhas (a comparação de palavras-chave ignora-os)."""
    decomposto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def limpar_texto(texto: Any) -> str:
    """Converte HTML e entidades em texto simples numa única linha."""
    if not texto:
        return ""
    texto = str(texto)
    if "<" in texto and ">" in texto:
        texto = BeautifulSoup(texto, "lxml").get_text(" ")
    texto = html.unescape(texto)
    return re.sub(r"\s+", " ", texto).strip()


def limpar_url_imagem(url: Any, base: str) -> Optional[str]:
    """URL absoluto de uma imagem, ou None se não for utilizável (vazio, data:, SVG ou GIF)."""
    url = html.unescape(str(url or "")).strip()
    if not url or url.startswith("data:"):
        return None
    url = urljoin(base, url.split()[0])
    partes = urlsplit(url)
    if partes.scheme not in ("http", "https") or re.search(r"\.(svg|gif)$", partes.path, re.I):
        return None
    return url


_META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
_ATRIBUTO_TAG = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_CHAVES_IMAGEM = ("og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src")


_CHAVES_DESCRICAO = ("og:description", "twitter:description", "description")
_DESCRICOES_PAGINA: dict[str, str] = {}  # link pedido -> descrição pública da página (preenchido em verificar_acesso)


def extrair_descricao_pagina(pagina_html: str) -> str:
    """Descrição pública que a própria página declara para partilha (og:description / meta description)."""
    encontradas: dict[str, str] = {}
    for tag in _META_TAG.findall(pagina_html[:400_000]):
        atributos = {m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
                     for m in _ATRIBUTO_TAG.finditer(tag)}
        chave = (atributos.get("property") or atributos.get("name") or "").lower()
        if chave in _CHAVES_DESCRICAO and chave not in encontradas:
            encontradas[chave] = atributos.get("content") or ""
    for chave in _CHAVES_DESCRICAO:
        texto = limpar_texto(encontradas.get(chave))
        if len(texto) >= 40:
            return texto[:1200]
    return ""


def extrair_imagem_pagina(pagina_html: str, base: str) -> Optional[str]:
    """Imagem de partilha do artigo (og:image ou twitter:image), se a página a indicar."""
    encontradas: dict[str, str] = {}
    for tag in _META_TAG.findall(pagina_html[:400_000]):
        atributos = {m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
                     for m in _ATRIBUTO_TAG.finditer(tag)}
        chave = (atributos.get("property") or atributos.get("name") or "").lower()
        if chave in _CHAVES_IMAGEM and chave not in encontradas:
            encontradas[chave] = atributos.get("content") or ""
    for chave in _CHAVES_IMAGEM:
        url = limpar_url_imagem(encontradas.get(chave), base)
        if url:
            return url
    return _imagem_alternativa(pagina_html, base)


def _imagem_alternativa(pagina_html: str, base: str) -> Optional[str]:
    """Quando a página não tem og:image: <link rel="image_src">, imagem dos dados estruturados ou primeira imagem do artigo."""
    try:
        sopa = BeautifulSoup(pagina_html[:400_000], "lxml")
    except Exception:
        return None
    for tag in sopa.find_all("link", rel=True, href=True):
        if "image_src" in [r.lower() for r in tag.get("rel", [])]:
            url = limpar_url_imagem(tag["href"], base)
            if url:
                return url
    for script in sopa.find_all("script", type="application/ld+json"):
        try:
            dados = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        pilha = [dados]
        while pilha:
            atual = pilha.pop()
            if isinstance(atual, list):
                pilha.extend(atual)
            elif isinstance(atual, dict):
                imagem = atual.get("image") or atual.get("thumbnailUrl")
                if isinstance(imagem, list) and imagem:
                    imagem = imagem[0]
                if isinstance(imagem, dict):
                    imagem = imagem.get("url")
                url = limpar_url_imagem(imagem, base) if isinstance(imagem, str) else None
                if url:
                    return url
                pilha.extend(v for v in atual.values() if isinstance(v, (list, dict)))
    return _imagem_do_contentor(sopa.find("article") or sopa.find("main"), base)


def _imagem_do_feed(entrada: Any, base: str) -> Optional[str]:
    """Imagem de uma entrada RSS (media:content, media:thumbnail, enclosure ou <img> no texto)."""
    for chave in ("media_content", "media_thumbnail"):
        for media in entrada.get(chave) or []:
            tipo = (media.get("type") or media.get("medium") or "image").lower()
            url = limpar_url_imagem(media.get("url"), base) if "image" in tipo else None
            if url:
                return url
    for ligacao in entrada.get("links") or []:
        if ligacao.get("rel") == "enclosure" and (ligacao.get("type") or "").startswith("image"):
            url = limpar_url_imagem(ligacao.get("href"), base)
            if url:
                return url
    conteudo = (entrada.get("content") or [{}])[0].get("value", "") + " " + (entrada.get("summary") or "")
    encontrado = re.search(r"""<img[^>]+src\s*=\s*["']([^"']+)""", conteudo, re.I)
    return limpar_url_imagem(encontrado.group(1), base) if encontrado else None


def _imagem_do_contentor(contentor: Any, base: str) -> Optional[str]:
    """Primeira imagem utilizável dentro do bloco HTML de uma notícia."""
    if contentor is None:
        return None
    for img in contentor.find_all("img", limit=3):
        for atributo in ("data-src", "data-lazy-src", "data-original", "src"):
            url = limpar_url_imagem(img.get(atributo), base)
            if url:
                return url
        srcset = img.get("data-srcset") or img.get("srcset")
        if srcset:
            url = limpar_url_imagem(srcset.split(",")[-1], base)
            if url:
                return url
    return None


def _palavra_flexivel(palavra: str) -> str:
    """Aceita singular e plural (cabo/cabos, rede/redes, movel/moveis)."""
    if len(palavra) < 4 or any(c.isdigit() for c in palavra):
        return re.escape(palavra)
    if palavra.endswith("el"):
        return re.escape(palavra[:-2]) + r"(?:el|eis)"
    if palavra.endswith("al"):
        return re.escape(palavra[:-2]) + r"(?:al|ais)"
    if palavra.endswith("m"):
        return re.escape(palavra[:-1]) + r"(?:m|ns)"
    return re.escape(palavra) + r"(?:s|es)?"


def _padrao_de_termos(termos: list[str]) -> Optional[re.Pattern]:
    if not termos:
        return None
    partes = [
        r"\s+".join(_palavra_flexivel(p) for p in t.split(" "))
        for t in sorted(termos, key=len, reverse=True)
    ]
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(partes) + r")(?![A-Za-z0-9²³])")


def _compilar_topico(termos: list[str]) -> tuple[Optional[re.Pattern], Optional[re.Pattern]]:
    insensiveis = [t for t in termos if not t.startswith("=")]
    sensiveis = [t[1:] for t in termos if t.startswith("=")]
    return _padrao_de_termos(insensiveis), _padrao_de_termos(sensiveis)


_TOPICOS_COMPILADOS = {nome: _compilar_topico(termos) for nome, termos in TOPICOS.items()}
_CARROS_FRACOS_PAD = _compilar_topico(CARROS_FRACOS)
_CARROS_MARCAS_PAD = _compilar_topico(CARROS_MARCAS)
_PT_INSENSIVEL = _padrao_de_termos(TERMOS_CONTEXTO_PT)
_PT_SENSIVEL = _padrao_de_termos(TERMOS_CONTEXTO_PT_SENSIVEIS)


# Criptografia pós-quântica (PQC) não pertence a "Quântica": no título exclui a categoria; no resumo, as expressões são ignoradas.
_PQC = re.compile(
    r"(?<![a-z0-9])(pqc|pos[- ]?quantic[oa]s?|post[- ]?quantum|quantum[- ](safe|resistant|proof)|"
    r"resistentes? a (computacao|computadores) quantic[oa]s?|criptografia quantico-segura)(?![a-z0-9])"
)


def _contagem(padrao: Optional[re.Pattern], texto: str) -> int:
    return len(padrao.findall(texto)) if padrao else 0


# «5G e 6G» é sobre redes. Lançamentos de telemóveis que só mencionam «5G» nas especificações não entram,
# salvo se o texto também falar de operadoras, cobertura, espetro, antenas, ANACOM...
_PRODUTO_CONSUMO = re.compile(
    r"(?<![a-z0-9])(smartphones?|telemoveis|telemovel|mah|amoled|oled|camara frontal|camara traseira|snapdragon|dimensity|"
    r"iphone|galaxy [a-z]?\d+|redmi|realme|oppo|xiaomi|honor|pro max|chega a portugal com)(?![a-z0-9])"
)
_REDE_5G = re.compile(
    r"(?<![a-z0-9])(operadoras?|cobertura|espetro|espectro|antenas?|anacom|frequencias?|municipios?|leilao|infraestruturas?|"
    r"cellnex|ericsson|nokia|huawei|open ran|spectrum|operators?|coverage|rollout|standalone|torres?|telecomunicacoes|dgeg)(?![a-z0-9])"
)

_JANELA_CONTEXTO = 120  # caracteres de cada lado do termo onde tem de aparecer uma palavra automóvel


def _carros_contexto(titulo: str, resumo: Optional[str]) -> tuple[bool, int, int]:
    """
    Para a categoria Carros. Devolve (veto, n_no_titulo, n_no_resumo) dos termos fracos e marcas que têm
    contexto automóvel perto (janela de _JANELA_CONTEXTO caracteres). Termos fracos: sensores, lidar, «level 4»...
    Marcas (Tesla, Ford...) precisam também de contexto de condução autónoma/assistida/conectada.
    """
    t_asc = sem_acentos(titulo).lower()
    asc = t_asc + " . " + sem_acentos(resumo or "").lower()
    asc = _VEICULO_NAO_CARRO.sub(lambda m: " " * len(m.group(0)), asc)
    if _NAO_RODOVIARIO.search(asc) and not _RODOVIARIO.search(asc):
        return True, 0, 0
    original = titulo + " . " + (resumo or "")
    mesmo_tamanho = len(original) == len(asc)
    corte = len(titulo)
    n_t = n_r = 0
    for (ins, sens), exige_tecnologia in ((_CARROS_FRACOS_PAD, False), (_CARROS_MARCAS_PAD, True)):
        for padrao, texto in ((ins, asc), (sens, original if mesmo_tamanho else "")):
            if not padrao or not texto:
                continue
            for m in padrao.finditer(texto):
                trecho = asc[max(0, m.start() - _JANELA_CONTEXTO): m.end() + _JANELA_CONTEXTO]
                if not _CTX_AUTOMOVEL.search(trecho):
                    continue
                if exige_tecnologia and not _CTX_TECNOLOGIA_CARRO.search(trecho):
                    continue
                if not exige_tecnologia and not _CTX_CONDUCAO.search(trecho):
                    continue
                if m.start() < corte:
                    n_t += 1
                else:
                    n_r += 1
    return False, n_t, n_r


# Blocos de notícias (direto, resumos diários/semanais) não são artigos sobre um tema: não entram.
_BLOCO_DE_NOTICIAS = re.compile(
    r"^\s*\d{1,2}\s*h\.\s"                                   # «1h. …», «21h. …»: atualizações do dia
    r"|outras?\s+(?:\d+\s+|duas\s+|tres\s+|quatro\s+|cinco\s+|seis\s+)?coisas?\b"  # «… e 4 outras coisas que precisa de saber»
    r"|para\s+comecar\s+o\s+dia|semana\s+dia\s+a\s+dia|resumo\s+do\s+dia|\bao\s+minuto\b|\bem\s+direto\b",
    re.I,
)
# Nomes de empresas: só contam para IA quando aparecem no título; citadas de passagem no resumo não chegam.
_EMPRESAS_IA = re.compile(r"(?<![a-z0-9])(?:anthropic|nvidia|openai|deepseek|grok|mistral ai)(?![a-z0-9])")
# Resumos de jornais/newsletters juntam várias notícias ("Ainda neste jornal, …"): o que vem depois não é sobre este artigo.
_RESUMO_DIGEST = re.compile(r"\s+Ainda\s+(?:neste|nesta)\s+\w+.*$", re.I | re.S)


def classificar(titulo: str, resumo: Optional[str]) -> tuple[list[str], bool]:
    """
    Devolve (tópicos por ordem de relevância, algum_termo_no_título).
    Um termo no título pesa mais do que no resumo.
    """
    resumo = _RESUMO_DIGEST.sub("", resumo or "")
    if _BLOCO_DE_NOTICIAS.search(sem_acentos(titulo).lower()):
        return [], False
    titulo_ascii, resumo_ascii = sem_acentos(titulo).lower(), sem_acentos(resumo or "").lower()
    pontos: dict[str, int] = {}
    no_titulo = False
    pqc_no_titulo = bool(_PQC.search(titulo_ascii))
    resumo_sem_pqc = _PQC.sub(" ", resumo_ascii)
    for nome, (ins, sens) in _TOPICOS_COMPILADOS.items():
        if nome == "Quântica" and pqc_no_titulo:
            continue
        r = resumo_sem_pqc if nome == "Quântica" else resumo_ascii
        if nome == "Inteligência Artificial":
            r = _EMPRESAS_IA.sub(" ", r)
        n_titulo = _contagem(ins, titulo_ascii) + _contagem(sens, titulo)
        n_resumo = _contagem(ins, r) + _contagem(sens, resumo or "")
        if nome == "5G e 6G":
            _t5 = titulo_ascii + " . " + resumo_ascii
            if _PRODUTO_CONSUMO.search(_t5) and not _REDE_5G.search(_t5):
                continue  # lançamento de telemóvel, não de rede
        if nome == "Carros":
            # Termos fracos e marcas só contam com contexto automóvel PERTO do termo (ver _carros_contexto).
            veto, ct, cr = _carros_contexto(titulo, resumo)
            if veto:
                continue  # drones, navios, comboios, robôs de armazém, satélites... não são «Carros»
            n_titulo += ct
            n_resumo += cr
        if nome == "Drones" and n_titulo == 0 and n_resumo < 2:
            continue  # «drones» citado uma só vez, de passagem, no resumo
        if n_titulo:
            no_titulo = True
        total = 3 * min(n_titulo, 2) + min(n_resumo, 3)
        if total:
            pontos[nome] = total
    if any(v >= 3 for v in pontos.values()):
        for nome in [n for n, v in pontos.items() if v == 1]:
            del pontos[nome]  # uma só menção solta no resumo não chega para uma categoria secundária
    prioridade = {nome: i for i, nome in enumerate(PRIORIDADE_TOPICOS)}
    ordenados = sorted(pontos, key=lambda n: (-pontos[n], prioridade.get(n, 99)))
    for nome in list(ordenados):
        for implicado in TOPICOS_IMPLICADOS.get(nome, []):
            if implicado not in ordenados:
                ordenados.append(implicado)
    return ordenados, no_titulo


def tem_contexto_pt(titulo: str, resumo: Optional[str]) -> bool:
    original = f"{titulo} {resumo or ''}"
    texto = sem_acentos(original).lower()
    return bool((_PT_INSENSIVEL and _PT_INSENSIVEL.search(texto)) or (_PT_SENSIVEL and _PT_SENSIVEL.search(original)))


def contem_keyword(titulo: str, resumo: Optional[str] = None) -> bool:
    """Compatibilidade com a versão 1: True se o texto tiver algum tópico."""
    return bool(classificar(titulo, resumo)[0])


# ---------------------------------------------------------------------------
# URLs e datas
# ---------------------------------------------------------------------------

_PARAMETROS_RASTREIO = re.compile(r"^(utm_|fbclid|gclid|mc_|igshid|cmpid|xtor|at_|ref$|ref_)", re.I)


def normalizar_url(url: str) -> str:
    """Remove fragmentos e parâmetros de rastreio, para que o mesmo artigo tenha um só URL."""
    partes = urlsplit((url or "").strip())
    consulta = [(k, v) for k, v in parse_qsl(partes.query, keep_blank_values=True)
                if not _PARAMETROS_RASTREIO.match(k)]
    return urlunsplit((partes.scheme.lower(), partes.netloc.lower(), partes.path, urlencode(consulta), ""))


def agora_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalizar_data(valor: Any) -> Optional[str]:
    """Converte struct_time ou texto ISO/RFC 822 em ISO 8601 UTC. Devolve None se inválida."""
    if not valor:
        return None
    dt: Optional[datetime] = None
    if hasattr(valor, "tm_year"):
        try:
            dt = datetime(*valor[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
    else:
        texto = str(valor).strip()
        try:
            dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
        except ValueError:
            dt = None
        if dt is None:
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(texto)
            except (TypeError, ValueError):
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=FUSO_LISBOA)
    dt = dt.astimezone(timezone.utc)
    if dt > datetime.now(timezone.utc) + timedelta(days=2):
        return None
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Base de dados
# ---------------------------------------------------------------------------


def ligar(db_path: Path = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=20)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    con.execute("PRAGMA busy_timeout=20000")
    con.create_function(
        "sa", 1, lambda s: sem_acentos(s).lower() if s else "", deterministic=True
    )
    return con


def criar_base_dados(db_path: Path = DB_PATH) -> None:
    """Cria a base de dados e migra bases criadas pela versão 1."""
    with closing(ligar(db_path)) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS noticias (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo           TEXT NOT NULL,
                link             TEXT NOT NULL UNIQUE,
                fonte            TEXT NOT NULL,
                data_publicacao  TEXT,
                categoria        TEXT,
                resumo           TEXT,
                topicos          TEXT,
                acesso           TEXT NOT NULL DEFAULT 'nao_verificado',
                criado_em        TEXT,
                imagem           TEXT
            );
            CREATE TABLE IF NOT EXISTS metadados (
                chave TEXT PRIMARY KEY,
                valor TEXT
            );
            """
        )
        colunas = {linha["name"] for linha in con.execute("PRAGMA table_info(noticias)")}
        if "topicos" not in colunas:
            con.execute("ALTER TABLE noticias ADD COLUMN topicos TEXT")
        if "acesso" not in colunas:
            con.execute("ALTER TABLE noticias ADD COLUMN acesso TEXT NOT NULL DEFAULT 'nao_verificado'")
        if "criado_em" not in colunas:
            con.execute("ALTER TABLE noticias ADD COLUMN criado_em TEXT")
        if "imagem" not in colunas:
            con.execute("ALTER TABLE noticias ADD COLUMN imagem TEXT")
        if "descricao_tentada" not in colunas:  # 1 = já se tentou obter a descrição pública da página
            con.execute("ALTER TABLE noticias ADD COLUMN descricao_tentada INTEGER NOT NULL DEFAULT 0")
        if "duplicada" not in colunas:  # 1 = mesmo título que uma notícia de fonte mais relevante (fica oculta)
            con.execute("ALTER TABLE noticias ADD COLUMN duplicada INTEGER NOT NULL DEFAULT 0")
        if "falhas_rede" not in colunas:  # quantas vezes a página não respondeu (evita repetir pedidos sem fim)
            con.execute("ALTER TABLE noticias ADD COLUMN falhas_rede INTEGER NOT NULL DEFAULT 0")
        for coluna in ("conteudo", "conteudo_tipo", "conteudo_origem", "autor"):
            if coluna not in colunas:
                con.execute(f"ALTER TABLE noticias ADD COLUMN {coluna} TEXT")
        if "link_hash" in colunas:  # coluna inútil da versão 1
            try:
                con.execute("ALTER TABLE noticias DROP COLUMN link_hash")
            except sqlite3.DatabaseError:
                pass
        con.execute(
            "UPDATE noticias SET criado_em = strftime('%Y-%m-%dT%H:%M:%S+00:00', COALESCE(criado_em, 'now')) "
            "WHERE criado_em IS NULL OR criado_em NOT LIKE '%T%'"
        )
        con.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_noticias_fonte  ON noticias (fonte);
            CREATE INDEX IF NOT EXISTS idx_noticias_data   ON noticias (data_publicacao DESC);
            CREATE INDEX IF NOT EXISTS idx_noticias_acesso ON noticias (acesso);
            """
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS metadados (chave TEXT PRIMARY KEY, valor TEXT)"
        )
        try:
            migrar_carros(con, db_path)
            _aplicar_renomeacoes(con)
            reclassificar_existentes(con)
            desduplicar_titulos(con)
        except Exception:
            logger.exception("falha ao reclassificar as notícias guardadas")
        limpas = 0
        try:
            limpas = preencher_conteudo_base(con)
        except Exception:
            logger.exception("falha ao limpar o conteúdo duplicado")
        con.commit()
    if limpas:
        try:
            compactar_base_dados(db_path)
            logger.info("base de dados compactada: %d resumos duplicados removidos", limpas)
        except Exception:
            logger.exception("falha ao compactar a base de dados")


def _aplicar_renomeacoes(con: sqlite3.Connection) -> None:
    """Atualiza nomes antigos de categorias (ex.: «Computação Quântica» -> «Quântica») e tira PQC de «Quântica»."""
    for linha in con.execute("SELECT id, titulo, topicos, categoria FROM noticias").fetchall():
        antigos = [t for t in (linha["topicos"] or "").split("|") if t]
        novos = list(dict.fromkeys(NOMES_RENOMEADOS.get(t, t) for t in antigos))
        if _PQC.search(sem_acentos(linha["titulo"] or "").lower()):
            novos = [t for t in novos if t != "Quântica"]
        if novos and novos != antigos:
            categoria = NOMES_RENOMEADOS.get(linha["categoria"], linha["categoria"])
            if categoria not in novos:
                categoria = novos[0]
            con.execute(
                "UPDATE noticias SET topicos = ?, categoria = ? WHERE id = ?",
                ("|" + "|".join(novos) + "|", categoria, linha["id"]),
            )


def _instantaneo(con: sqlite3.Connection) -> dict[int, tuple]:
    return {
        l["id"]: (l["topicos"] or "", l["categoria"] or "", l["titulo"])
        for l in con.execute("SELECT id, titulo, topicos, categoria FROM noticias")
    }


def migrar_carros(con: sqlite3.Connection, db_path: Optional[Path] = None) -> Optional[dict]:
    """
    Migração única e idempotente: «Carros Conectados» e «Carros Autónomos» -> «Carros».
    Faz cópia de segurança, reclassifica, verifica que nada se perdeu e guarda o relatório nos metadados.
    Se a verificação falhar, desfaz tudo (rollback) e deixa a base como estava.
    """
    if con.execute("SELECT 1 FROM metadados WHERE chave = 'migracao_carros_v1'").fetchone():
        return None
    antigas = ("Carros Conectados", "Carros Autónomos")
    total_antes = con.execute("SELECT COUNT(*) FROM noticias").fetchone()[0]
    links_antes = con.execute("SELECT COUNT(DISTINCT link) FROM noticias").fetchone()[0]
    antes = _instantaneo(con)
    por_categoria_antiga = {
        n: sum(1 for t, _, _ in antes.values() if f"|{n}|" in t or f"|{n}|" in f"|{t.strip('|')}|") for n in antigas
    }
    ids_antigos = {i for i, (t, c, _) in antes.items() if any(n in t.split("|") or n == c for n in antigas)}

    copia = None
    if total_antes and db_path and Path(db_path).exists():
        copia = Path(db_path).with_name(f"{Path(db_path).stem}.backup-{datetime.now():%Y%m%d-%H%M%S}.db")
        con.commit()
        destino = sqlite3.connect(copia)
        try:
            con.backup(destino)
        finally:
            destino.close()

    _aplicar_renomeacoes(con)
    alteradas = reclassificar_existentes(con, forcar=True)
    depois = _instantaneo(con)

    problemas = []
    if con.execute("SELECT COUNT(*) FROM noticias").fetchone()[0] != total_antes:
        problemas.append("o número de notícias mudou")
    if con.execute("SELECT COUNT(DISTINCT link) FROM noticias").fetchone()[0] != links_antes:
        problemas.append("o número de ligações únicas mudou")
    if any(f"|{n}|" in t or c == n for t, c, _ in depois.values() for n in antigas):
        problemas.append("ficaram registos com as categorias antigas")
    if set(antes) != set(depois):
        problemas.append("os identificadores mudaram")
    if problemas:
        con.rollback()
        logger.error("Migração Carros desfeita: %s", "; ".join(problemas))
        return {"erro": problemas, "copia_seguranca": str(copia) if copia else None}

    tem_carros = {i for i, (t, c, _) in depois.items() if "|Carros|" in t or c == "Carros"}
    relatorio = {
        "data": agora_utc(),
        "copia_seguranca": str(copia) if copia else None,
        "total_noticias": total_antes,
        "antes_por_categoria_antiga": por_categoria_antiga,
        "registos_nas_categorias_antigas": len(ids_antigos),
        "transferidas_para_carros": len(ids_antigos & tem_carros),
        "adicionais_reclassificadas_para_carros": len(tem_carros - ids_antigos),
        "sem_alteracao": sum(1 for i in antes if antes[i][:2] == depois[i][:2]),
        "reclassificadas_no_total": alteradas,
        "ocultas_apos_migracao": sum(1 for t, _, _ in depois.values() if not t),
        "duvidosas": [
            {"id": i, "titulo": antes[i][2][:90], "tópicos_atuais": depois[i][0].strip("|") or "(oculta)"}
            for i in sorted(ids_antigos - tem_carros)
        ],
        "adicionais_titulos": [depois[i][2][:90] for i in sorted(tem_carros - ids_antigos)][:20],
    }
    definir_meta(con, "relatorio_migracao_carros", relatorio)
    definir_meta(con, "migracao_carros_v1", agora_utc())
    logger.info("Migração Carros concluída: %s", {k: v for k, v in relatorio.items() if not isinstance(v, (list, dict))})
    return relatorio


def _versao_classificacao() -> str:
    """Muda sempre que as palavras-chave mudam."""
    import hashlib

    base = json.dumps([TOPICOS, TOPICOS_IMPLICADOS, "sem-tema-oculta-v2", "carros-v4", "ruido-v1", "blocos-v1", _BLOCO_DE_NOTICIAS.pattern, _EMPRESAS_IA.pattern, _PRODUTO_CONSUMO.pattern, _REDE_5G.pattern, CARROS_FRACOS, CARROS_MARCAS,
                       _NAO_RODOVIARIO.pattern, _RODOVIARIO.pattern, _VEICULO_NAO_CARRO.pattern, str(_JANELA_CONTEXTO), _CTX_CONDUCAO.pattern,
                       _CTX_AUTOMOVEL.pattern, _CTX_TECNOLOGIA_CARRO.pattern], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


# Em títulos exatamente iguais mantém-se a notícia da fonte com número mais baixo (mais relevo em Portugal).
PRIORIDADE_FONTES = {
    "Público": 1, "ECO": 2, "Jornal de Negócios": 3, "Observador": 4,
    "Portal do Governo": 5, "FCT": 6, "ANI": 7,
    "TEK SAPO": 8, "Pplware": 9, "IT Security": 10, "Dinheiro Vivo": 11,
}
# Fontes que podem coexistir com título igual (todas as credíveis ficam visíveis entre si)
_FONTES_CREDÍVEIS = {"Público", "ECO", "Jornal de Negócios", "Observador", "Portal do Governo", "FCT", "ANI"}


def _titulo_normalizado(titulo: str) -> str:
    """Título sem acentos, maiúsculas nem pontuação: só os títulos com as mesmas palavras são iguais."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", sem_acentos(titulo or "").lower()).split())


def desduplicar_titulos(con: sqlite3.Connection) -> int:
    """
    Regras de deduplicação por grupo de título normalizado:
    - Se o grupo contém apenas fontes credíveis → todas ficam visíveis (podem ter texto diferente).
    - Se o grupo contém pelo menos uma fonte credível → as fontes secundárias ficam ocultas.
    - Se o grupo contém apenas fontes secundárias → mantém só a de maior prioridade (menor número),
      em empate a mais antiga.
    Recalcula tudo a cada execução, por isso é seguro repetir. Devolve quantas ficaram ocultas.
    """
    grupos: dict[str, list[sqlite3.Row]] = {}
    for linha in con.execute("SELECT id, titulo, fonte, duplicada FROM noticias WHERE acesso != 'bloqueado' AND COALESCE(topicos, '') <> ''"):
        chave = _titulo_normalizado(linha["titulo"])
        if chave:
            grupos.setdefault(chave, []).append(linha)
    ocultar: set[int] = set()
    for linhas in grupos.values():
        if len(linhas) <= 1:
            continue
        tem_credivel = any(l["fonte"] in _FONTES_CREDÍVEIS for l in linhas)
        if tem_credivel:
            # Fontes secundárias ficam ocultas; credíveis coexistem todas
            for l in linhas:
                if l["fonte"] not in _FONTES_CREDÍVEIS:
                    ocultar.add(l["id"])
        else:
            # Só fontes secundárias: mantém a de maior prioridade (menor nº), em empate a mais antiga
            linhas.sort(key=lambda l: (PRIORIDADE_FONTES.get(l["fonte"], 50), l["id"]))
            ocultar.update(l["id"] for l in linhas[1:])
    atuais = {l["id"] for l in con.execute("SELECT id FROM noticias WHERE duplicada = 1")}
    for i in atuais - ocultar:
        con.execute("UPDATE noticias SET duplicada = 0 WHERE id = ?", (i,))
    for i in ocultar - atuais:
        con.execute("UPDATE noticias SET duplicada = 1 WHERE id = ?", (i,))
    return len(ocultar)


def reclassificar_existentes(con: sqlite3.Connection, forcar: bool = False) -> int:
    """
    Volta a classificar as notícias guardadas com as palavras-chave atuais (só se estas mudaram).
    Notícias sem nenhum tópico ficam ocultas. Devolve quantas mudaram.
    """
    versao = _versao_classificacao()
    atual = con.execute("SELECT valor FROM metadados WHERE chave = 'versao_classificacao'").fetchone()
    if atual and json.loads(atual["valor"]) == versao and not forcar:
        return 0
    alteradas = 0
    for linha in con.execute("SELECT id, titulo, resumo, topicos, categoria FROM noticias").fetchall():
        topicos, _ = classificar(linha["titulo"], linha["resumo"])
        if not topicos:
            # Sem tópico: a notícia fica guardada mas oculta (não aparece no site nem nos filtros).
            con.execute("UPDATE noticias SET topicos = '', categoria = '' WHERE id = ?", (linha["id"],))
            alteradas += 1
            continue
        marcados = "|" + "|".join(topicos) + "|"
        if marcados != linha["topicos"] or topicos[0] != linha["categoria"]:
            con.execute("UPDATE noticias SET topicos = ?, categoria = ? WHERE id = ?", (marcados, topicos[0], linha["id"]))
            alteradas += 1
    definir_meta(con, "versao_classificacao", versao)
    return alteradas


def definir_meta(con: sqlite3.Connection, chave: str, valor: Any) -> None:
    con.execute(
        "INSERT INTO metadados (chave, valor) VALUES (?, ?) "
        "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
        (chave, json.dumps(valor, ensure_ascii=False)),
    )


def obter_meta(chave: str, db_path: Path = DB_PATH, padrao: Any = None) -> Any:
    try:
        with closing(ligar(db_path)) as con:
            linha = con.execute("SELECT valor FROM metadados WHERE chave = ?", (chave,)).fetchone()
        return json.loads(linha["valor"]) if linha else padrao
    except (sqlite3.DatabaseError, ValueError):
        return padrao


def preencher_conteudo_base(con: sqlite3.Connection) -> int:
    """
    Sem pedidos HTTP. Versões anteriores copiavam o resumo para a coluna `conteudo`, guardando o mesmo texto duas vezes
    (a base e o histórico do Git cresciam mais do que o necessário). Aqui apaga-se essa cópia: a aplicação já usa o
    resumo quando `conteudo` está vazio. Só texto autorizado (excerto/integral) fica em `conteudo`.
    """
    cursor = con.execute(
        "UPDATE noticias SET conteudo = NULL, conteudo_tipo = NULL, conteudo_origem = NULL "
        "WHERE conteudo_tipo = 'resumo' AND conteudo IS NOT NULL AND conteudo = resumo"
    )
    return cursor.rowcount


def apagar_noticias_antigas(con: sqlite3.Connection, dias: Optional[int] = None) -> int:
    """
    Apaga notícias com mais de `dias` dias (por defeito RETENCAO_DIAS), pela data de publicação ou, na falta dela,
    pela data de recolha. Só remove linhas da base de dados; o histórico do Git não é reescrito.
    """
    dias = RETENCAO_DIAS if dias is None else dias
    limite = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat(timespec="seconds")
    return con.execute(
        "DELETE FROM noticias WHERE COALESCE(data_publicacao, criado_em) < ?", (limite,)
    ).rowcount


def compactar_base_dados(db_path: Path = DB_PATH) -> None:
    """VACUUM: devolve ao ficheiro o espaço libertado (apagar linhas não reduz o tamanho do .db sem isto)."""
    ligacao = sqlite3.connect(db_path, isolation_level=None)
    try:
        ligacao.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        ligacao.execute("VACUUM")
    finally:
        ligacao.close()


_PARAGRAFO_RUIDO = re.compile(
    r"(subscri|assin(e|ar)|cookies?|newsletter|partilh|publicidade|leia (também|tambem)|veja (também|tambem)|"
    r"todos os direitos|copyright|siga-nos|siga o)",
    re.I,
)


def extrair_texto_artigo(pagina_html: str, limite: int) -> str:
    """Parágrafos do corpo do artigo (texto simples, sem HTML), cortados em fim de parágrafo até limite caracteres."""
    sopa = BeautifulSoup(pagina_html, "lxml")
    for tag in sopa(["script", "style", "noscript", "aside", "nav", "footer", "form", "iframe", "figure"]):
        tag.decompose()
    raiz = sopa.find("article") or sopa.find("main") or sopa.body or sopa
    partes: list[str] = []
    total = 0
    for p in raiz.find_all("p"):
        texto = limpar_texto(p.get_text(" "))
        if len(texto) < 60 or _PARAGRAFO_RUIDO.search(texto):
            continue
        if total + len(texto) > limite:
            break
        partes.append(texto)
        total += len(texto)
    return "\n\n".join(partes)


def obter_conteudo_autorizado(link: str, politica: dict[str, Any]) -> Optional[str]:
    """Só corre para fontes com licença verificada. Nunca contorna paywalls: se houver sinal de restrição, devolve None."""
    if politica["uso"] == "resumo":
        return None
    limite = int(politica.get("limite_caracteres") or LIMITE_CONTEUDO_PADRAO)
    if politica["uso"] == "integral":
        limite = min(max(limite, LIMITE_CONTEUDO_PADRAO), LIMITE_MAXIMO_INTEGRAL)
    try:
        resposta = SESSAO.get(link, timeout=TIMEOUT, allow_redirects=True)
        resposta.raise_for_status()
    except requests.RequestException:
        return None
    if "html" not in resposta.headers.get("Content-Type", "").lower():
        return None
    texto = resposta.text
    if detectar_paywall(texto):
        return None
    return extrair_texto_artigo(texto, limite) or None


def preencher_conteudo_autorizado(con: sqlite3.Connection) -> int:
    """Durante a atualização (nunca no hover): completa o conteúdo das fontes cuja política o permite."""
    feitas = 0
    linhas = con.execute(
        "SELECT id, link, fonte FROM noticias WHERE acesso = 'livre' AND COALESCE(conteudo_tipo, 'resumo') = 'resumo' "
        "AND COALESCE(conteudo_origem, 'feed') = 'feed' ORDER BY COALESCE(data_publicacao, criado_em) DESC"
    ).fetchall()
    for linha in linhas:
        if feitas >= MAX_CONTEUDOS_POR_EXECUCAO:
            break
        politica = politica_fonte(linha["fonte"])
        if politica["uso"] == "resumo":
            continue
        texto = obter_conteudo_autorizado(linha["link"], politica)
        if texto:
            con.execute(
                "UPDATE noticias SET conteudo = ?, conteudo_tipo = ?, conteudo_origem = ? WHERE id = ?",
                (texto, politica["uso"], f"artigo ({politica['dominio'] or linha['fonte']})", linha["id"]),
            )
            feitas += 1
        time.sleep(PAUSA_ENTRE_FONTES)
    return feitas


def inserir_noticia(
    con: sqlite3.Connection,
    titulo: str,
    link: str,
    fonte: str,
    data_publicacao: Optional[str],
    categoria: str,
    resumo: Optional[str],
    topicos: Optional[list[str]] = None,
    acesso: str = "nao_verificado",
    imagem: Optional[str] = None,
    autor: Optional[str] = None,
) -> bool:
    """Insere a notícia. Devolve False se o URL já existir."""
    marcados = "|" + "|".join(topicos or [categoria]) + "|"
    cursor = con.execute(
        """
        INSERT OR IGNORE INTO noticias
            (titulo, link, fonte, data_publicacao, categoria, resumo, topicos, acesso, criado_em, imagem,
             conteudo, conteudo_tipo, conteudo_origem, autor)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (titulo, link, fonte, data_publicacao, categoria, resumo, marcados, acesso, agora_utc(), imagem,
         None, None, None, autor or None),  # conteudo só guarda texto autorizado; sem ele a app usa o resumo
    )
    return cursor.rowcount == 1


# ---------------------------------------------------------------------------
# Recolha: RSS, descoberta de feeds e extração de páginas HTML
# ---------------------------------------------------------------------------


def _resumo_erro(exc: BaseException) -> str:
    texto = f"{type(exc).__name__}: {exc}"
    return texto if len(texto) <= 140 else texto[:137] + "..."


def ler_feed(url: str) -> list[dict]:
    """Lê um feed RSS/Atom. Levanta exceção se o pedido falhar ou o conteúdo não for um feed."""
    resposta = SESSAO.get(url, timeout=TIMEOUT)
    resposta.raise_for_status()
    feed = feedparser.parse(resposta.content)
    if not feed.entries:
        raise ValueError("a resposta não contém entradas de feed")
    artigos = []
    for entrada in feed.entries:
        titulo = limpar_texto(entrada.get("title"))
        link = (entrada.get("link") or "").strip()
        if not titulo or not link:
            continue
        resumo = limpar_texto(entrada.get("summary") or "")
        if not resumo and entrada.get("content"):
            resumo = limpar_texto(entrada["content"][0].get("value", ""))
        etiquetas = [sem_acentos(limpar_texto(t.get("term"))).lower() for t in entrada.get("tags", []) if t.get("term")]
        artigos.append({
            "titulo": titulo,
            "link": normalizar_url(urljoin(url, link)),
            "data_publicacao": normalizar_data(entrada.get("published_parsed") or entrada.get("updated_parsed")),
            "resumo": resumo[:1200],
            "etiquetas": etiquetas,
            "imagem": _imagem_do_feed(entrada, url),
            "autor": limpar_texto(entrada.get("author"))[:120],
        })
    return artigos


def descobrir_feeds(pagina_url: str) -> list[str]:
    """Procura endereços de feeds numa página (<link rel="alternate"> e ligações para RSS)."""
    resposta = SESSAO.get(pagina_url, timeout=TIMEOUT)
    resposta.raise_for_status()
    sopa = BeautifulSoup(resposta.content, "lxml")
    encontrados: list[str] = []
    for tag in sopa.find_all("link", href=True):
        tipo = (tag.get("type") or "").lower()
        if "rss" in tipo or "atom" in tipo:
            encontrados.append(urljoin(resposta.url, tag["href"]))
    for ancora in sopa.find_all("a", href=True):
        href = ancora["href"]
        if re.search(r"(rss|\.xml|/feed)", href, re.I) and "sitemap" not in href.lower():
            encontrados.append(urljoin(resposta.url, href))
    unicos: list[str] = []
    for url in encontrados:
        if url not in unicos and "comments" not in url.lower() and url.startswith("http"):
            unicos.append(url)
    return unicos[:10]


def extrair_da_pagina(pagina_url: str, fonte: dict) -> list[dict]:
    """Extrai notícias da listagem HTML de uma página (alternativa quando não há feed)."""
    resposta = SESSAO.get(pagina_url, timeout=TIMEOUT)
    resposta.raise_for_status()
    sopa = BeautifulSoup(resposta.content, "lxml")
    base = urlsplit(resposta.url)
    dominio = ".".join(base.netloc.lower().split(".")[-2:])
    padrao_link = re.compile(fonte["padrao_link"]) if fonte.get("padrao_link") else None
    artigos: list[dict] = []
    vistos: set[str] = set()

    def registar(ancora, titulo: str, contentor) -> None:
        href = (ancora.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return
        link = normalizar_url(urljoin(resposta.url, href))
        partes = urlsplit(link)
        if partes.scheme not in ("http", "https") or not partes.netloc.endswith(dominio):
            return
        if padrao_link and not padrao_link.search(link):
            return
        titulo = limpar_texto(titulo)
        if len(titulo) < 25 or link in vistos:
            return
        vistos.add(link)
        resumo, data = "", None
        if contentor is not None:
            paragrafo = contentor.find("p")
            resumo = limpar_texto(paragrafo.get_text(" ")) if paragrafo else ""
            tempo = contentor.find("time")
            if tempo:
                data = normalizar_data(tempo.get("datetime") or tempo.get_text(strip=True))
        artigos.append({
            "titulo": titulo, "link": link, "data_publicacao": data,
            "resumo": resumo[:1200], "etiquetas": [],
            "imagem": _imagem_do_contentor(contentor, resposta.url),
        })

    if padrao_link:
        for ancora in sopa.find_all("a", href=True):
            registar(ancora, ancora.get_text(" "), ancora.find_parent(["li", "div", "tr"]))
    for artigo in sopa.find_all("article")[:80]:
        cabecalho = artigo.find(["h1", "h2", "h3", "h4"])
        ancora = (cabecalho.find("a", href=True) if cabecalho else None) or artigo.find("a", href=True)
        if ancora:
            registar(ancora, (cabecalho or ancora).get_text(" "), artigo)
    if not artigos:
        for cabecalho in sopa.find_all(["h2", "h3"])[:120]:
            ancora = cabecalho.find("a", href=True) or cabecalho.find_parent("a", href=True)
            if ancora:
                registar(ancora, cabecalho.get_text(" "), cabecalho.parent)
    return artigos


def recolher_fonte(fonte: dict) -> tuple[list[dict], dict]:
    """
    Recolhe os artigos de uma fonte. Ordem: feeds conhecidos, feeds descobertos
    nas páginas e, por fim, extração da listagem HTML.
    Devolve (artigos, estado) onde estado resume o que funcionou ou falhou.
    """
    nome = fonte["nome"]
    estado: dict[str, Any] = {"metodo": None, "avisos": []}
    artigos: list[dict] = []

    def acrescentar(novos: list[dict]) -> None:
        existentes = {a["link"] for a in artigos}
        artigos.extend(a for a in novos if a["link"] not in existentes)

    for url in fonte.get("feeds", []):
        try:
            acrescentar(ler_feed(url))
            estado["metodo"] = "rss"
        except Exception as exc:
            estado["avisos"].append(f"{url}: {_resumo_erro(exc)}")
            logger.warning("[%s] feed indisponível (%s)", nome, _resumo_erro(exc))

    if not artigos:
        for pagina in fonte.get("paginas", []):
            try:
                candidatos = descobrir_feeds(pagina)
            except Exception as exc:
                estado["avisos"].append(f"{pagina}: {_resumo_erro(exc)}")
                continue
            for url in candidatos:
                try:
                    acrescentar(ler_feed(url))
                    estado["metodo"] = "rss descoberto"
                except Exception:
                    continue
            if artigos:
                break

    if not artigos:
        for pagina in fonte.get("paginas", []):
            try:
                acrescentar(extrair_da_pagina(pagina, fonte))
                estado["metodo"] = "html"
            except Exception as exc:
                estado["avisos"].append(f"{pagina}: {_resumo_erro(exc)}")
            if len(artigos) >= 15:
                break

    logger.info("[%s] %d artigos recolhidos (%s)", nome, len(artigos), estado["metodo"] or "sem método")
    return artigos, estado


# ---------------------------------------------------------------------------
# Filtragem
# ---------------------------------------------------------------------------


def avaliar_artigo(artigo: dict, fonte: dict) -> tuple[Optional[dict], str]:
    """
    Aplica as regras de seleção. Devolve (artigo enriquecido, "") se aceite
    ou (None, motivo) se rejeitado.
    """
    titulo, resumo = artigo["titulo"], artigo.get("resumo", "")
    if not artigo["link"].startswith(("http://", "https://")):
        return None, "ligação inválida"

    topicos, no_titulo = classificar(titulo, resumo)
    if not topicos:
        return None, "sem tópico"
    if RUIDO_TITULO.search(sem_acentos(titulo).lower()):
        # Um termo de produto no título só é aceite se houver um tópico específico
        # no título ("5G" sozinho é típico de análises de telemóveis).
        if not no_titulo or topicos[0] == "5G e 6G":
            return None, "produto ou entretenimento"
    if EXIGIR_CONTEXTO_PT and not fonte.get("sempre_pt") and not tem_contexto_pt(titulo, resumo):
        return None, "sem contexto português"

    aceite = dict(artigo, categoria=topicos[0], topicos=topicos)
    if any(e in ETIQUETAS_BLOQUEIO for e in artigo.get("etiquetas", [])):
        aceite["acesso"] = "bloqueado"
        aceite["motivo_acesso"] = "etiqueta do feed indica conteúdo reservado"
    return aceite, ""


# ---------------------------------------------------------------------------
# Verificação de acesso (paywall, login, ligação inválida)
# ---------------------------------------------------------------------------


def _json_tem_acesso_restrito(dados: Any) -> bool:
    if isinstance(dados, dict):
        for chave, valor in dados.items():
            if str(chave).lower() == "isaccessibleforfree" and str(valor).strip().lower() == "false":
                return True
            if _json_tem_acesso_restrito(valor):
                return True
    elif isinstance(dados, list):
        return any(_json_tem_acesso_restrito(item) for item in dados)
    return False


def detectar_paywall(pagina_html: str) -> Optional[str]:
    """Devolve o motivo se o HTML indicar conteúdo reservado a assinantes; caso contrário None."""
    sopa = BeautifulSoup(pagina_html, "lxml")

    for script in sopa.find_all("script", type="application/ld+json"):
        try:
            dados = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        if _json_tem_acesso_restrito(dados):
            return "dados estruturados indicam acesso restrito"

    for meta in sopa.find_all("meta"):
        chave = (meta.get("property") or meta.get("name") or "").strip().lower()
        valor = (meta.get("content") or "").strip().lower()
        if valor in META_BLOQUEIO.get(chave, ()):
            return f"meta {chave}={valor}"

    for tag in sopa(["script", "style", "noscript", "nav", "footer", "header", "aside"]):
        tag.decompose()
    corpo = sopa.body or sopa
    zona_artigo = sopa.find("article") or sopa.find("main") or corpo
    texto_artigo = re.sub(r"\s+", " ", sem_acentos(zona_artigo.get_text(" ")).lower())
    texto_pagina = re.sub(r"\s+", " ", sem_acentos(corpo.get_text(" ")).lower())
    for frase in FRASES_BLOQUEIO_ARTIGO:
        if frase in texto_artigo:
            return f"texto da página: «{frase}»"
    for frase in FRASES_BLOQUEIO_PAGINA:
        if frase in texto_pagina:
            return f"texto da página: «{frase}»"
    return None


def verificar_acesso(url: str) -> tuple[str, str, Optional[str]]:
    """
    Abre o artigo e devolve (estado, motivo, imagem).
    Estados: "livre", "bloqueado" ou "nao_verificado" (sem resposta fiável).
    imagem é o og:image do artigo, quando existe.
    """
    try:
        resposta = SESSAO.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
    except requests.RequestException as exc:
        return "nao_verificado", _resumo_erro(exc), None
    try:
        codigo = resposta.status_code
        if codigo in (401, 402, 404, 410):
            return "bloqueado", f"HTTP {codigo}", None
        if codigo >= 400:
            return "nao_verificado", f"HTTP {codigo}", None
        if URL_DE_BLOQUEIO.search(urlsplit(resposta.url).path) and not URL_DE_BLOQUEIO.search(urlsplit(url).path):
            return "bloqueado", "redirecionado para página de login ou subscrição", None
        if "html" not in resposta.headers.get("Content-Type", "").lower():
            return "livre", "ficheiro não HTML", None
        corpo = b""
        for bloco in resposta.iter_content(65_536):
            corpo += bloco
            if len(corpo) >= MAX_BYTES_PAGINA:
                break
        codificacao = resposta.encoding if resposta.encoding and resposta.encoding.lower() != "iso-8859-1" else "utf-8"
        texto = corpo.decode(codificacao, errors="replace")
    except requests.RequestException as exc:
        return "nao_verificado", _resumo_erro(exc), None
    finally:
        resposta.close()
    imagem = extrair_imagem_pagina(texto, resposta.url)
    _DESCRICOES_PAGINA[url] = extrair_descricao_pagina(texto)
    motivo = detectar_paywall(texto)
    if motivo:
        return "bloqueado", motivo, imagem
    return "livre", "acesso livre", imagem


def verificar_lote(links: list[str], max_threads: int = 6) -> dict[str, tuple[str, str, Optional[str]]]:
    """Verifica vários links em paralelo (poucos fios, para não sobrecarregar as fontes)."""
    if not links:
        return {}
    with ThreadPoolExecutor(max_workers=max_threads) as pool:
        return dict(zip(links, pool.map(verificar_acesso, links)))


def reverificar_pendentes(
    con: sqlite3.Connection, limite: int = MAX_REVERIFICACOES, antes_de: Optional[str] = None
) -> int:
    """
    Tenta confirmar o acesso a notícias que ficaram por verificar (apenas as
    gravadas antes de antes_de, se indicado). Devolve quantas mudaram de estado.
    """
    linhas = con.execute(
        "SELECT id, link FROM noticias WHERE acesso = 'nao_verificado' AND criado_em < ? "
        "AND COALESCE(falhas_rede, 0) < ? ORDER BY criado_em DESC LIMIT ?",
        (antes_de or "9999", MAX_FALHAS_DE_REDE, limite),
    ).fetchall()
    resultados = verificar_lote([l["link"] for l in linhas])
    alteradas = 0
    for linha in linhas:
        estado, _, imagem = resultados[linha["link"]]
        if estado != "nao_verificado":
            con.execute("UPDATE noticias SET acesso = ?, imagem = COALESCE(?, imagem) WHERE id = ?",
                        (estado, imagem, linha["id"]))
            alteradas += 1
        else:
            con.execute("UPDATE noticias SET falhas_rede = COALESCE(falhas_rede, 0) + 1 WHERE id = ?", (linha["id"],))
    return alteradas


def preencher_imagens_em_falta(con: sqlite3.Connection, limite: int = MAX_IMAGENS_POR_EXECUCAO) -> int:
    """
    Notícias guardadas antes de a imagem ser recolhida (ou para além do limite de verificações)
    ficam sem imagem. Abre o artigo e guarda o og:image. imagem = '' marca "já tentado, sem imagem",
    para não repetir o pedido; falhas de rede deixam o campo vazio (NULL) e voltam a ser tentadas.
    Devolve quantas notícias ganharam imagem.
    """
    linhas = con.execute(
        "SELECT id, link, acesso, COALESCE(falhas_rede, 0) AS falhas FROM noticias "
        "WHERE imagem IS NULL AND acesso != 'bloqueado' AND COALESCE(falhas_rede, 0) < ? "
        "ORDER BY COALESCE(data_publicacao, criado_em) DESC LIMIT ?",
        (MAX_FALHAS_DE_REDE, limite),
    ).fetchall()
    resultados = verificar_lote([l["link"] for l in linhas])
    preenchidas = 0
    for linha in linhas:
        estado, _, imagem = resultados[linha["link"]]
        if estado == "nao_verificado":
            # falha de rede: tenta-se noutra atualização, mas só até MAX_FALHAS_DE_REDE vezes
            if linha["falhas"] + 1 >= MAX_FALHAS_DE_REDE:
                con.execute("UPDATE noticias SET imagem = '', falhas_rede = ? WHERE id = ?", (linha["falhas"] + 1, linha["id"]))
            else:
                con.execute("UPDATE noticias SET falhas_rede = ? WHERE id = ?", (linha["falhas"] + 1, linha["id"]))
            continue
        if estado == "bloqueado":
            con.execute("UPDATE noticias SET acesso = 'bloqueado' WHERE id = ?", (linha["id"],))
            continue
        con.execute("UPDATE noticias SET imagem = ?, acesso = 'livre' WHERE id = ?", (imagem or "", linha["id"]))
        preenchidas += 1 if imagem else 0
    return preenchidas


def preencher_descricoes_em_falta(con: sqlite3.Connection, limite: int = MAX_DESCRICOES_POR_EXECUCAO) -> int:
    """
    Notícias sem resumo no feed: abre o artigo e guarda a descrição pública que a página declara
    (og:description / meta description). Não guarda texto do corpo do artigo. Cada notícia é tentada uma só vez.
    """
    linhas = con.execute(
        "SELECT id, link FROM noticias WHERE TRIM(COALESCE(resumo, '')) = '' AND acesso != 'bloqueado' "
        "AND descricao_tentada = 0 ORDER BY COALESCE(data_publicacao, criado_em) DESC LIMIT ?", (limite,),
    ).fetchall()
    if not linhas:
        return 0
    resultados = verificar_lote([l["link"] for l in linhas])
    preenchidas = 0
    for linha in linhas:
        estado = resultados[linha["link"]][0]
        if estado == "nao_verificado":
            continue  # falha de rede: volta a tentar-se na atualização seguinte
        texto = _DESCRICOES_PAGINA.get(linha["link"], "") if estado == "livre" else ""
        con.execute("UPDATE noticias SET resumo = ?, descricao_tentada = 1 WHERE id = ?", (texto, linha["id"]))
        if estado == "bloqueado":
            con.execute("UPDATE noticias SET acesso = 'bloqueado' WHERE id = ?", (linha["id"],))
        preenchidas += 1 if texto else 0
    return preenchidas


def preencher_imagens_pendentes(db_path: Path = DB_PATH, limite: int = MAX_IMAGENS_POR_EXECUCAO) -> int:
    """Versão autónoma de preencher_imagens_em_falta (abre a sua própria ligação); usada em segundo plano pela app."""
    with closing(ligar(db_path)) as con:
        total = preencher_imagens_em_falta(con, limite)
        con.commit()
    return total


# ---------------------------------------------------------------------------
# Atualização da base de dados
# ---------------------------------------------------------------------------


def actualizar_base_dados(db_path: Path = DB_PATH, verificar: Optional[bool] = None) -> dict:
    """
    Recolhe, filtra, verifica o acesso e grava as notícias novas.
    Nunca levanta exceção por falhas de rede: o resultado de cada fonte fica em
    stats["fontes"][nome]["estado"]. Devolve um dicionário de estatísticas.
    """
    verificar = VERIFICAR_ACESSO if verificar is None else verificar
    inicio = time.time()
    inicio_iso = agora_utc()
    criar_base_dados(db_path)

    stats: dict[str, Any] = {
        "total_recolhidos": 0, "total_filtrados": 0, "total_duplicados": 0,
        "total_inseridos": 0, "total_bloqueadas": 0, "total_nao_verificadas": 0,
        "reverificadas": 0, "erros": 0, "fontes": {},
    }

    with closing(ligar(db_path)) as con:
        existentes = {l["link"] for l in con.execute("SELECT link FROM noticias")}
        titulos = {(l["fonte"], l["titulo"]) for l in con.execute("SELECT fonte, titulo FROM noticias")}
        por_inserir: list[tuple[dict, str]] = []

        for indice, fonte in enumerate(FONTES):
            nome = fonte["nome"]
            resumo = {"recolhidos": 0, "filtrados": 0, "novos": 0, "rejeitados": {}, "estado": "", "avisos": []}
            stats["fontes"][nome] = resumo
            try:
                artigos, estado = recolher_fonte(fonte)
            except Exception as exc:  # defesa final: uma fonte nunca derruba as outras
                logger.exception("[%s] erro inesperado", nome)
                resumo["estado"] = f"erro: {_resumo_erro(exc)}"
                stats["erros"] += 1
                continue

            resumo["avisos"] = estado["avisos"][:4]
            resumo["recolhidos"] = len(artigos)
            stats["total_recolhidos"] += len(artigos)
            if not artigos:
                resumo["estado"] = "sem resultados"
                stats["erros"] += 1
            else:
                resumo["estado"] = f"ok ({estado['metodo']})"

            for artigo in artigos:
                aceite, motivo = avaliar_artigo(artigo, fonte)
                if aceite is None:
                    resumo["rejeitados"][motivo] = resumo["rejeitados"].get(motivo, 0) + 1
                    continue
                resumo["filtrados"] += 1
                stats["total_filtrados"] += 1
                if aceite["link"] in existentes or (nome, aceite["titulo"]) in titulos:
                    stats["total_duplicados"] += 1
                    continue
                existentes.add(aceite["link"])
                titulos.add((nome, aceite["titulo"]))
                por_inserir.append((aceite, nome))
            if indice < len(FONTES) - 1:
                time.sleep(PAUSA_ENTRE_FONTES)

        # Verificar o acesso às notícias novas (as mais recentes primeiro).
        por_inserir.sort(key=lambda par: par[0].get("data_publicacao") or "", reverse=True)
        a_verificar = [a["link"] for a, _ in por_inserir if "acesso" not in a]
        a_verificar = a_verificar[:MAX_VERIFICACOES_POR_EXECUCAO] if verificar else []
        resultados = verificar_lote(a_verificar)

        for aceite, nome in por_inserir:
            estado_verificado, _, imagem_artigo = resultados.get(aceite["link"], ("nao_verificado", "", None))
            acesso = aceite.get("acesso", estado_verificado)
            texto_resumo = aceite.get("resumo", "")
            if not texto_resumo.strip() and acesso != "bloqueado":
                texto_resumo = _DESCRICOES_PAGINA.get(aceite["link"], "")
            inserida = inserir_noticia(
                con, aceite["titulo"], aceite["link"], nome, aceite.get("data_publicacao"),
                aceite["categoria"], texto_resumo, aceite["topicos"], acesso,
                imagem_artigo or aceite.get("imagem"), aceite.get("autor"),
            )
            if inserida:
                stats["total_inseridos"] += 1
                stats["fontes"][nome]["novos"] += 1
                if acesso == "bloqueado":
                    stats["total_bloqueadas"] += 1
                elif acesso == "nao_verificado":
                    stats["total_nao_verificadas"] += 1
        con.commit()

        if verificar:
            try:
                stats["reverificadas"] = reverificar_pendentes(con, antes_de=inicio_iso)
            except Exception:
                logger.exception("falha ao reverificar notícias pendentes")

        try:
            stats["imagens_preenchidas"] = preencher_imagens_em_falta(con)
        except Exception:
            logger.exception("falha ao preencher imagens em falta")
        try:
            stats["descricoes_preenchidas"] = preencher_descricoes_em_falta(con) if verificar else 0
        except Exception:
            logger.exception("falha ao preencher descrições em falta")
        try:
            stats["reclassificadas"] = reclassificar_existentes(con)
            stats["duplicadas"] = desduplicar_titulos(con)
        except Exception:
            logger.exception("falha ao reclassificar as notícias guardadas")
        try:
            preencher_conteudo_base(con)
            stats["conteudos_autorizados"] = preencher_conteudo_autorizado(con)
        except Exception:
            logger.exception("falha ao preencher o conteúdo de pré-visualização")

        stats["apagadas_antigas"] = apagar_noticias_antigas(con)

        stats["duracao"] = round(time.time() - inicio, 1)
        definir_meta(con, "ultima_actualizacao", agora_utc())
        definir_meta(con, "ultimo_resumo", stats)
        con.commit()
    if stats.get("apagadas_antigas"):
        try:
            compactar_base_dados(db_path)  # DELETE não reduz o ficheiro sem VACUUM
        except Exception:
            logger.exception("falha ao compactar a base de dados")
    return stats


atualizar_base_dados = actualizar_base_dados  # grafia do Acordo Ortográfico


# ---------------------------------------------------------------------------
# Consultas (usadas pela aplicação Streamlit)
# ---------------------------------------------------------------------------

ORDENACOES = {
    "recentes": "data_ordem DESC, id DESC",
    "antigas": "data_ordem ASC, id ASC",
    "fonte": "fonte COLLATE NOCASE ASC, data_ordem DESC, id DESC",
}


def _estados_visiveis(incluir_nao_verificadas: bool) -> tuple[str, ...]:
    return ("livre", "nao_verificado") if incluir_nao_verificadas else ("livre",)


def _como_iso(valor: Any) -> Optional[str]:
    if valor is None:
        return None
    if isinstance(valor, datetime):
        if valor.tzinfo is None:
            valor = valor.replace(tzinfo=timezone.utc)
        return valor.astimezone(timezone.utc).isoformat(timespec="seconds")
    return str(valor)


def listar_noticias(
    db_path: Path = DB_PATH,
    limite: int = 50,
    fonte: Optional[str] = None,
    categoria: Optional[str] = None,
    keyword: Optional[str] = None,
    *,
    fontes: Optional[list[str]] = None,
    categorias: Optional[list[str]] = None,
    desde: Any = None,
    ordem: str = "recentes",
    incluir_nao_verificadas: bool = INCLUIR_NAO_VERIFICADAS,
    offset: int = 0,
) -> list[dict]:
    """
    Devolve notícias já filtradas. Notícias bloqueadas nunca são devolvidas.
    ordem: "recentes", "antigas" ou "fonte".
    """
    estados = _estados_visiveis(incluir_nao_verificadas)
    condicoes = [f"acesso IN ({','.join('?' * len(estados))})", "COALESCE(topicos, '') <> ''", "duplicada = 0"]
    parametros: list[Any] = list(estados)

    lista_fontes = list(fontes or ([fonte] if fonte else []))
    if lista_fontes:
        condicoes.append(f"fonte IN ({','.join('?' * len(lista_fontes))})")
        parametros.extend(lista_fontes)

    lista_categorias = list(categorias or ([categoria] if categoria else []))
    if lista_categorias:
        partes = []
        for cat in lista_categorias:
            partes.append("(topicos LIKE ? OR categoria = ?)")
            parametros.extend([f"%|{cat}|%", cat])
        condicoes.append("(" + " OR ".join(partes) + ")")

    if keyword and keyword.strip():
        for palavra in sem_acentos(keyword).lower().split():
            condicoes.append("(sa(titulo) LIKE ? ESCAPE '\\' OR sa(resumo) LIKE ? ESCAPE '\\')")
            palavra = palavra.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parametros.extend([f"%{palavra}%", f"%{palavra}%"])

    desde_iso = _como_iso(desde)
    if desde_iso:
        condicoes.append("COALESCE(data_publicacao, criado_em) >= ?")
        parametros.append(desde_iso)

    ordenacao = ORDENACOES.get(ordem, ORDENACOES["recentes"])
    consulta = f"""
        SELECT id, titulo, link, fonte, data_publicacao, categoria, resumo, topicos, acesso,
               criado_em, imagem, conteudo, conteudo_tipo, conteudo_origem, autor,
               COALESCE(data_publicacao, criado_em) AS data_ordem
        FROM noticias
        WHERE {' AND '.join(condicoes)}
        ORDER BY {ordenacao}
        LIMIT ? OFFSET ?
    """
    parametros.extend([int(limite), int(offset)])
    with closing(ligar(db_path)) as con:
        return [dict(linha) for linha in con.execute(consulta, parametros)]


def contar_por_fonte(db_path: Path = DB_PATH, incluir_nao_verificadas: bool = INCLUIR_NAO_VERIFICADAS) -> list[dict]:
    """Número de notícias visíveis por fonte, da maior para a menor."""
    estados = _estados_visiveis(incluir_nao_verificadas)
    with closing(ligar(db_path)) as con:
        linhas = con.execute(
            f"SELECT fonte, COUNT(*) AS total FROM noticias WHERE acesso IN ({','.join('?' * len(estados))}) AND COALESCE(topicos, '') <> '' AND duplicada = 0 "
            "GROUP BY fonte ORDER BY total DESC, fonte",
            estados,
        ).fetchall()
    return [{"fonte": l["fonte"], "total": l["total"]} for l in linhas]


def contar_noticias(db_path: Path = DB_PATH, incluir_nao_verificadas: bool = INCLUIR_NAO_VERIFICADAS) -> int:
    return sum(item["total"] for item in contar_por_fonte(db_path, incluir_nao_verificadas))


def listar_categorias(db_path: Path = DB_PATH, incluir_nao_verificadas: bool = INCLUIR_NAO_VERIFICADAS) -> list[dict]:
    """Categorias com notícias visíveis, na ordem de ORDEM_CATEGORIAS."""
    estados = _estados_visiveis(incluir_nao_verificadas)
    with closing(ligar(db_path)) as con:
        linhas = con.execute(
            f"SELECT topicos, categoria FROM noticias WHERE acesso IN ({','.join('?' * len(estados))}) AND COALESCE(topicos, '') <> '' AND duplicada = 0", estados
        ).fetchall()
    contagem: dict[str, int] = {}
    for linha in linhas:
        nomes = [t for t in (linha["topicos"] or "").split("|") if t] or [linha["categoria"] or "Outras"]
        for nome in set(nomes):
            contagem[nome] = contagem.get(nome, 0) + 1
    ordem = {nome: i for i, nome in enumerate(ORDEM_CATEGORIAS)}
    nomes_ordenados = sorted(contagem, key=lambda n: (ordem.get(n, 99), n))
    return [{"categoria": n, "total": contagem[n]} for n in nomes_ordenados]


# ---------------------------------------------------------------------------
# Diagnóstico e linha de comandos
# ---------------------------------------------------------------------------


def diagnosticar_fontes() -> None:
    """Testa cada feed e página de cada fonte e escreve o resultado (não grava na base de dados)."""
    for fonte in FONTES:
        print(f"\n{fonte['nome']}")
        for url in fonte.get("feeds", []):
            try:
                print(f"  RSS ok ({len(ler_feed(url))} entradas): {url}")
            except Exception as exc:
                print(f"  RSS falhou: {url}\n      {_resumo_erro(exc)}")
        for pagina in fonte.get("paginas", []):
            try:
                feeds = descobrir_feeds(pagina)
                ligacoes = extrair_da_pagina(pagina, fonte)
                print(f"  Página ok: {pagina}\n      feeds descobertos: {len(feeds)} | ligações extraídas: {len(ligacoes)}")
            except Exception as exc:
                print(f"  Página falhou: {pagina}\n      {_resumo_erro(exc)}")


def _escrever_resumo(stats: dict, duracao: float, db: Path) -> None:
    print("\n" + "=" * 66)
    print("  Hub de Inovação e Comunicações Emergentes: resumo da atualização")
    print("=" * 66)
    print(f"  Base de dados            : {db}")
    print(f"  Duração                  : {duracao:.1f} s")
    print(f"  Artigos recolhidos       : {stats['total_recolhidos']}")
    print(f"  Aceites pelos filtros    : {stats['total_filtrados']}")
    print(f"  Já existentes            : {stats['total_duplicados']}")
    print(f"  Novos gravados           : {stats['total_inseridos']}")
    print(f"  Bloqueados (ocultos)     : {stats['total_bloqueadas']}")
    print(f"  Acesso por confirmar     : {stats['total_nao_verificadas']}")
    print("-" * 66)
    for nome, dados in stats["fontes"].items():
        print(f"  {nome:<20} recolhidos={dados['recolhidos']:>3}  aceites={dados['filtrados']:>3}  novos={dados['novos']:>3}  [{dados['estado']}]")
        for aviso in dados["avisos"]:
            print(f"      aviso: {aviso}")
    print("=" * 66 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Recolha de notícias do Hub de Inovação e Comunicações Emergentes")
    parser.add_argument("--init", action="store_true", help="apaga e recria a base de dados")
    parser.add_argument("--db", default=str(DB_PATH), help="caminho do ficheiro SQLite")
    parser.add_argument("--diagnostico", action="store_true", help="testa as fontes sem gravar")
    parser.add_argument("--relatorio-migracao", action="store_true", help="mostra o relatório da migração «Carros»")
    parser.add_argument("--verificar", metavar="URL", help="testa a deteção de acesso a um artigo")
    parser.add_argument("--sem-verificacao", action="store_true", help="não abre os artigos para detetar paywall")
    parser.add_argument("-v", "--verbose", action="store_true", help="mais detalhe")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.diagnostico:
        diagnosticar_fontes()
        return
    if args.relatorio_migracao:
        criar_base_dados(Path(args.db))
        with closing(ligar(Path(args.db))) as con:
            linha = con.execute("SELECT valor FROM metadados WHERE chave = 'relatorio_migracao_carros'").fetchone()
        print(json.dumps(json.loads(linha["valor"]), ensure_ascii=False, indent=2) if linha else "sem relatório")
        return
    if args.verificar:
        estado, motivo, imagem = verificar_acesso(args.verificar)
        print(f"{estado}: {motivo}" + (f"\nimagem: {imagem}" if imagem else ""))
        return

    db = Path(args.db)
    if args.init and db.exists():
        db.unlink()
        for sufixo in ("-wal", "-shm"):
            Path(str(db) + sufixo).unlink(missing_ok=True)

    inicio = time.time()
    stats = actualizar_base_dados(db, verificar=not args.sem_verificacao)
    _escrever_resumo(stats, time.time() - inicio, db)

    recentes = listar_noticias(db, limite=5)
    if recentes:
        print("  Notícias mais recentes:")
        for n in recentes:
            print(f"   [{n['fonte']}] {n['titulo'][:90]}")
        print()


if __name__ == "__main__":
    main()
