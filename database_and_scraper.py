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
MAX_REVERIFICACOES = 40
RETENCAO_DIAS = 365
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
# aceita_sem_topico: aceita notícias sem palavra-chave de tópico (ANACOM).
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
    {
        "nome": "IT Security",
        "feeds": [],
        "paginas": ["https://www.itsecurity.pt/news/news"],
    },
]

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
        "operador virtual", "mvno", "banda larga", "fibra optica", "ftth",
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
        "modelo de ia", "modelos de ia", "sistemas de ia", "deepseek", "mistral ai",
        "microsoft copilot", "github copilot", "google gemini", "supercomputador",
        "supercomputadores", "computacao de alto desempenho", "=HPC", "eurohpc",
        "deucalion", "semicondutores", "chips act",
    ],
    "Agentes de IA": [
        "agentes de ia", "agente de ia", "agentes de ai", "agente de ai",
        "agentes de inteligencia artificial", "agente de inteligencia artificial",
        "agentes autonomos", "agente autonomo", "ia agentica", "ai agentica",
        "agentic", "agentic ai", "ai agents", "ai agent", "agentes inteligentes",
        "agentes autonomos de ia", "model context protocol", "sistemas multiagente",
        "orquestracao de agentes", "assistentes de ia", "assistente de ia", "automacao inteligente",
    ],
    "Carros Conectados": [
        "carros conectados", "carro conectado", "veiculos conectados", "veiculo conectado",
        "automoveis conectados", "automovel conectado", "carros ligados", "carro ligado",
        "veiculos ligados", "conectividade automovel", "conectividade veicular",
        "v2x", "c-v2x", "c-its", "ecall", "mobilidade conectada",
        "internet dos veiculos", "veiculo definido por software", "software-defined vehicle",
        "software defined vehicle", "carros inteligentes", "estrada inteligente",
        "estradas inteligentes", "sistemas de transporte inteligentes",
        "sistemas inteligentes de transporte",
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
    ],
    "Cloud": [
        "cloud", "computacao em nuvem", "servicos de nuvem", "nuvem publica",
        "nuvem privada", "nuvem soberana", "multicloud", "multi-cloud",
        "hiperescala", "hiperescalar", "hyperscaler", "hyperscalers",
        "=AWS", "azure", "google cloud", "oracle cloud", "saas", "iaas", "paas",
        "armazenamento em nuvem", "computacao na nuvem", "migracao para a nuvem",
        "nuvem hibrida", "soberania digital", "soberania de dados", "data act",
        "ovhcloud", "alibaba cloud",
    ],
    "Data Centers": [
        "data center", "data centers", "data centre", "data centres", "datacenter",
        "datacenters", "centro de dados", "centros de dados",
        "centro de processamento de dados", "campus de dados", "start campus",
        "equinix", "digital realty", "colocation", "ponto de troca de trafego",
        "pontos de troca de trafego", "ixp", "ixps",
    ],
    "Computação Quântica": [
        "computacao quantica", "computador quantico", "computadores quanticos",
        "quantum", "qubit", "qubits", "tecnologia quantica", "processador quantico",
        "processadores quanticos", "sistemas quanticos", "supremacia quantica",
        "vantagem quantica", "pos-quantica", "ibm quantum",
        "criptografia pos-quantica", "criptografia quantica", "comunicacoes quanticas",
        "comunicacao quantica", "internet quantica", "distribuicao quantica de chaves",
        "qkd", "pqc", "tecnologias quanticas", "sensores quanticos",
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
        "redes de muito alta capacidade", "partilha de infraestruturas", "antenas",
        "estacoes base", "operador de rede", "operadores de rede", "obrigacoes de cobertura",
        "direito de passagem", "direitos de passagem", "infraestruturas aptas",
        "qualidade de servico das comunicacoes",
    ],
    "Drones": [
        "drone", "drones", "=UAS", "rpas", "uav", "uavs",
        "veiculos aereos nao tripulados", "veiculo aereo nao tripulado",
        "aeronave nao tripulada", "aeronaves nao tripuladas", "u-space",
        "anti-drone", "antidrone", "antidrones", "entregas por drone", "evtol", "evtols", "taxi aereo eletrico", "taxis aereos eletricos",
        "mobilidade aerea avancada",
    ],
    "Outras Tecnologias Emergentes": [
        "internet das coisas", "=IoT", "edge computing", "computacao de periferia",
        "computacao na periferia", "gemeos digitais", "digital twin", "metaverso",
        "realidade aumentada", "ciberseguranca", "nis2", "tecnologia emergente",
        "tecnologias emergentes", "constelacao de satelites", "constelacoes de satelites",
        "internet por satelite", "comunicacoes por satelite", "starlink", "kuiper",
        "iris2", "direct-to-cell", "direct-to-device", "wi-fi 7", "wifi 7",
        "lorawan", "nb-iot", "lte-m", "lpwan", "wi-fi 6e", "wi-fi 6", "wifi 6",
        "internet via satelite", "banda larga por satelite", "satelites de comunicacoes",
        "oneweb", "eutelsat", "amazon leo", "porto espacial", "spaceport",
        "cidades inteligentes", "cidade inteligente", "smart city", "smart cities",
        "gemeo digital", "realidade mista",
        "identidade digital", "chave movel digital", "eidas", "carteira digital europeia",
        "cibercrime", "burlas telefonicas", "cyber resilience act", "regulamento de ciberresiliencia",
        "fotonica",
    ],
}

# Equivalentes em inglês dos termos acima (singular e plural são tratados automaticamente).
TERMOS_EM_INGLES: dict[str, list[str]] = {
    "Telecomunicações": [
        "telecommunications", "telco", "mobile operator", "mobile network", "fixed network",
        "broadband", "optical fibre", "optical fiber", "fibre optic", "fiber optic",
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
        "large language model", "neural network", "ai model", "ai system", "ai regulation",
        "sovereign ai", "european ai act", "ai gigafactory", "supercomputer",
        "high-performance computing", "high performance computing", "semiconductor",
    ],
    "Agentes de IA": [
        "autonomous agent", "ai assistant", "intelligent agent", "multi-agent system",
        "agent orchestration", "intelligent automation", "ai copilot",
    ],
    "Carros Conectados": [
        "connected car", "connected vehicle", "vehicle connectivity", "connected mobility",
        "internet of vehicles", "smart car", "smart road", "intelligent transport system",
        "intelligent transportation system",
    ],
    "Carros Autónomos": [
        "autonomous car", "autonomous vehicle", "self-driving", "self driving",
        "driverless", "autonomous driving", "automated driving", "autonomous taxi",
        "autonomous bus", "autonomous truck", "autonomous shuttle",
    ],
    "Cloud": ["digital sovereignty", "data sovereignty", "sovereign cloud"],
    "Data Centers": ["datacentre", "internet exchange point", "internet exchange"],
    "Computação Quântica": [
        "quantum computing", "quantum computer", "quantum technology", "quantum cryptography",
        "quantum communication", "quantum internet", "quantum key distribution",
        "post-quantum cryptography", "post-quantum", "quantum sensor", "quantum processor",
        "quantum supremacy", "quantum advantage",
    ],
    "Cabos Submarinos": [
        "submarine cable", "undersea cable", "subsea cable", "transatlantic cable",
        "cable landing", "cable sabotage", "subsea infrastructure", "undersea infrastructure",
    ],
    "Redes e Comunicações Eletrónicas": [
        "electronic communications", "communications network", "communication network",
        "universal service", "communications regulator", "telecom regulator",
        "radio spectrum", "spectrum auction", "spectrum allocation", "spectrum licence",
        "spectrum license", "spectrum management", "very high capacity network",
        "infrastructure sharing", "european electronic communications code",
        "net neutrality", "base station", "telecom tower", "right of way",
        "network operator", "coverage obligation",
    ],
    "Drones": [
        "unmanned aerial vehicle", "unmanned aircraft", "drone delivery", "air taxi",
        "urban air mobility", "advanced air mobility",
    ],
    "Outras Tecnologias Emergentes": [
        "internet of things", "metaverse", "augmented reality", "mixed reality",
        "cybersecurity", "emerging technology", "satellite constellation", "satellite internet",
        "satellite communications", "satellite broadband", "digital identity",
        "digital identity wallet", "cybercrime", "phone scam", "photonics",
        "low-power wide-area",
    ],
}
for _nome, _extra in TERMOS_EM_INGLES.items():
    TOPICOS[_nome].extend(t for t in _extra if t not in TOPICOS[_nome])

# Em caso de pontuação igual, vence o tópico mais específico (primeiro da lista).
PRIORIDADE_TOPICOS = [
    "Agentes de IA", "Cabos Submarinos", "Carros Autónomos", "Carros Conectados",
    "Computação Quântica", "Data Centers", "Cloud", "Drones", "Inteligência Artificial",
    "5G e 6G", "Redes e Comunicações Eletrónicas", "Telecomunicações",
    "Outras Tecnologias Emergentes",
]

# Ordem de apresentação nos filtros.
ORDEM_CATEGORIAS = list(TOPICOS.keys()) + [CATEGORIA_REGULACAO]

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
    "nowo", "dstelecom", "altice labs", "outsystems", "feedzai", "talkdesk", "farfetch",
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
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(partes) + r")(?![A-Za-z0-9])")


def _compilar_topico(termos: list[str]) -> tuple[Optional[re.Pattern], Optional[re.Pattern]]:
    insensiveis = [t for t in termos if not t.startswith("=")]
    sensiveis = [t[1:] for t in termos if t.startswith("=")]
    return _padrao_de_termos(insensiveis), _padrao_de_termos(sensiveis)


_TOPICOS_COMPILADOS = {nome: _compilar_topico(termos) for nome, termos in TOPICOS.items()}
_PT_INSENSIVEL = _padrao_de_termos(TERMOS_CONTEXTO_PT)
_PT_SENSIVEL = _padrao_de_termos(TERMOS_CONTEXTO_PT_SENSIVEIS)


def _contagem(padrao: Optional[re.Pattern], texto: str) -> int:
    return len(padrao.findall(texto)) if padrao else 0


def classificar(titulo: str, resumo: Optional[str]) -> tuple[list[str], bool]:
    """
    Devolve (tópicos por ordem de relevância, algum_termo_no_título).
    Um termo no título pesa mais do que no resumo.
    """
    titulo_ascii, resumo_ascii = sem_acentos(titulo).lower(), sem_acentos(resumo or "").lower()
    pontos: dict[str, int] = {}
    no_titulo = False
    for nome, (ins, sens) in _TOPICOS_COMPILADOS.items():
        n_titulo = _contagem(ins, titulo_ascii) + _contagem(sens, titulo)
        n_resumo = _contagem(ins, resumo_ascii) + _contagem(sens, resumo or "")
        if n_titulo:
            no_titulo = True
        total = 3 * min(n_titulo, 2) + min(n_resumo, 3)
        if total:
            pontos[nome] = total
    prioridade = {nome: i for i, nome in enumerate(PRIORIDADE_TOPICOS)}
    ordenados = sorted(pontos, key=lambda n: (-pontos[n], prioridade.get(n, 99)))
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
                criado_em        TEXT
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
        con.commit()


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
) -> bool:
    """Insere a notícia. Devolve False se o URL já existir."""
    marcados = "|" + "|".join(topicos or [categoria]) + "|"
    cursor = con.execute(
        """
        INSERT OR IGNORE INTO noticias
            (titulo, link, fonte, data_publicacao, categoria, resumo, topicos, acesso, criado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (titulo, link, fonte, data_publicacao, categoria, resumo, marcados, acesso, agora_utc()),
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
    if not topicos and not fonte.get("aceita_sem_topico"):
        return None, "sem tópico"
    if RUIDO_TITULO.search(sem_acentos(titulo).lower()) and not fonte.get("aceita_sem_topico"):
        # Um termo de produto no título só é aceite se houver um tópico específico
        # no título ("5G" sozinho é típico de análises de telemóveis).
        if not no_titulo or topicos[0] == "5G e 6G":
            return None, "produto ou entretenimento"
    if EXIGIR_CONTEXTO_PT and not fonte.get("sempre_pt") and not tem_contexto_pt(titulo, resumo):
        return None, "sem contexto português"

    categoria = topicos[0] if topicos else CATEGORIA_REGULACAO
    aceite = dict(artigo, categoria=categoria, topicos=topicos or [CATEGORIA_REGULACAO])
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


def verificar_acesso(url: str) -> tuple[str, str]:
    """
    Abre o artigo e devolve (estado, motivo).
    Estados: "livre", "bloqueado" ou "nao_verificado" (sem resposta fiável).
    """
    try:
        resposta = SESSAO.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
    except requests.RequestException as exc:
        return "nao_verificado", _resumo_erro(exc)
    try:
        codigo = resposta.status_code
        if codigo in (401, 402, 404, 410):
            return "bloqueado", f"HTTP {codigo}"
        if codigo >= 400:
            return "nao_verificado", f"HTTP {codigo}"
        if URL_DE_BLOQUEIO.search(urlsplit(resposta.url).path) and not URL_DE_BLOQUEIO.search(urlsplit(url).path):
            return "bloqueado", "redirecionado para página de login ou subscrição"
        if "html" not in resposta.headers.get("Content-Type", "").lower():
            return "livre", "ficheiro não HTML"
        corpo = b""
        for bloco in resposta.iter_content(65_536):
            corpo += bloco
            if len(corpo) >= MAX_BYTES_PAGINA:
                break
        codificacao = resposta.encoding if resposta.encoding and resposta.encoding.lower() != "iso-8859-1" else "utf-8"
        texto = corpo.decode(codificacao, errors="replace")
    except requests.RequestException as exc:
        return "nao_verificado", _resumo_erro(exc)
    finally:
        resposta.close()
    motivo = detectar_paywall(texto)
    if motivo:
        return "bloqueado", motivo
    return "livre", "acesso livre"


def verificar_lote(links: list[str], max_threads: int = 6) -> dict[str, tuple[str, str]]:
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
        "ORDER BY criado_em DESC LIMIT ?",
        (antes_de or "9999", limite),
    ).fetchall()
    resultados = verificar_lote([l["link"] for l in linhas])
    alteradas = 0
    for linha in linhas:
        estado, _ = resultados[linha["link"]]
        if estado != "nao_verificado":
            con.execute("UPDATE noticias SET acesso = ? WHERE id = ?", (estado, linha["id"]))
            alteradas += 1
    return alteradas


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
            if "acesso" in aceite:
                acesso = aceite["acesso"]
            else:
                acesso = resultados.get(aceite["link"], ("nao_verificado", ""))[0]
            inserida = inserir_noticia(
                con, aceite["titulo"], aceite["link"], nome, aceite.get("data_publicacao"),
                aceite["categoria"], aceite.get("resumo", ""), aceite["topicos"], acesso,
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

        limite = (datetime.now(timezone.utc) - timedelta(days=RETENCAO_DIAS)).isoformat(timespec="seconds")
        con.execute("DELETE FROM noticias WHERE COALESCE(data_publicacao, criado_em) < ?", (limite,))

        stats["duracao"] = round(time.time() - inicio, 1)
        definir_meta(con, "ultima_actualizacao", agora_utc())
        definir_meta(con, "ultimo_resumo", stats)
        con.commit()
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
    condicoes = [f"acesso IN ({','.join('?' * len(estados))})"]
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
               criado_em, COALESCE(data_publicacao, criado_em) AS data_ordem
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
            f"SELECT fonte, COUNT(*) AS total FROM noticias WHERE acesso IN ({','.join('?' * len(estados))}) "
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
            f"SELECT topicos, categoria FROM noticias WHERE acesso IN ({','.join('?' * len(estados))})", estados
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
    if args.verificar:
        estado, motivo = verificar_acesso(args.verificar)
        print(f"{estado}: {motivo}")
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
