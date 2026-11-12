"""
app.py

Hub de Inovação
Interface Streamlit sobre a base de dados criada por database_and_scraper.py.

Execução local:   streamlit run app.py
Sem APIs de inteligência artificial: toda a seleção é feita por regras e palavras-chave.
"""

from __future__ import annotations

import html
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

import streamlit as st
import streamlit.components.v1 as components

from database_and_scraper import (
    DB_PATH,
    FONTES,
    FUSO_LISBOA,
    actualizar_base_dados,
    contar_por_fonte,
    criar_base_dados,
    preencher_imagens_pendentes,
    listar_categorias,
    listar_noticias,
    obter_meta,
)

# ---------------------------------------------------------------------------
# Configuração da página (tem de ser o primeiro comando Streamlit)
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Hub de Inovação",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="auto",
)

NOTICIAS_POR_PAGINA = 20
ATUALIZAR_AUTOMATICAMENTE_CADA_HORAS = 6

OPCOES_PERIODO = {
    "Todo o período": None,
    "Últimas 24 horas": timedelta(hours=24),
    "Últimos 7 dias": timedelta(days=7),
    "Últimos 30 dias": timedelta(days=30),
}
OPCOES_ORDEM = {
    "Mais recentes": "recentes",
    "Mais antigas": "antigas",
    "Fonte (A-Z)": "fonte",
}

# Categorias por ordem de importância nos filtros do topo; as restantes seguem a ordem da base de dados.
ORDEM_FILTRO_TOPO = [
    "5G e 6G", "Inteligência Artificial", "Agentes de IA", "Drones",
    "Redes e Comunicações Eletrónicas", "Cloud", "Data Centers",
]

# Matiz (0 a 360) de cada categoria, usado nas etiquetas.
MATIZ_CATEGORIA = {
    "Telecomunicações": 205,
    "5G e 6G": 165,
    "Inteligência Artificial": 280,
    "Agentes de IA": 325,
    "Carros Conectados": 35,
    "Carros Autónomos": 55,
    "Cloud": 225,
    "Data Centers": 190,
    "Computação Quântica": 255,
    "Cabos Submarinos": 15,
    "Redes e Comunicações Eletrónicas": 140,
    "Drones": 85,
    "Outras Tecnologias Emergentes": 65,
}

ESTILO = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600;700&display=swap');
.block-container { padding-top: .4rem !important; max-width: 1180px; }
/* Cabeçalho do Streamlit transparente e sem ocupar espaço; só os botões (menu, barra lateral) ficam ativos */
[data-testid="stHeader"] { background: transparent !important; pointer-events: none; }
[data-testid="stHeader"] button { pointer-events: auto; }
[data-testid="stToolbar"] button { background: rgba(255,255,255,.92); border-radius: 8px; }
[data-testid="stAppDeployButton"] { display: none; }
.hub-titulo, .hub-info, .nc, .pag-info { font-family: "Schibsted Grotesk", "Source Sans Pro", sans-serif; }
.hub-titulo { font-size: 1.9rem; font-weight: 700; line-height: 1.15; letter-spacing: -.02em; margin: 0 0 .5rem; padding: 0; text-align: left; }
.hub-info { font-size: .74rem; color: rgba(49,51,63,.6); margin: .5rem 0 .1rem; }
/* Com a barra lateral fechada, o botão de a abrir fica à esquerda: o título desloca-se para não ficar por baixo */
.stApp:has([data-testid="stExpandSidebarButton"]) .hub-titulo { margin-left: 2.7rem; }

/* Menu da barra lateral com os símbolos habituais nas apps de telemóvel: ☰ para abrir e ✕ para fechar */
[data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapseButton"] button {
  position: relative; width: 2.5rem; height: 2.5rem; border-radius: 10px; }
[data-testid="stExpandSidebarButton"] { background: #fff; box-shadow: 0 1px 4px rgba(0,0,0,.18); }
[data-testid="stExpandSidebarButton"] > *, [data-testid="stSidebarCollapseButton"] button > * { display: none !important; }
[data-testid="stExpandSidebarButton"]::before, [data-testid="stSidebarCollapseButton"] button::before {
  content: ""; position: absolute; inset: 0; margin: auto; width: 1.55rem; height: 1.55rem; background-color: #31333F;
  -webkit-mask: var(--ic-menu) center / contain no-repeat; mask: var(--ic-menu) center / contain no-repeat; }
[data-testid="stSidebarCollapseButton"] button::before { -webkit-mask-image: var(--ic-fechar); mask-image: var(--ic-fechar); }
:root {
  --ic-menu: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M3 18h18v-2H3v2zm0-5h18v-2H3v2zm0-7v2h18V6H3z'/></svg>");
  --ic-fechar: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z'/></svg>");
  --ic-copiar: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z'/></svg>");
  --ic-visto: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z'/></svg>");
}

/* Filtro de categorias: etiquetas em retângulo, como as dos cartões; várias podem estar ativas */
[class*="st-key-f_categorias"] { margin-bottom: .2rem; }
[class*="st-key-f_categorias"] button { font-family: "Schibsted Grotesk", "Source Sans Pro", sans-serif; font-size: .8rem !important;
  font-weight: 600; line-height: 1.3; min-height: 0; height: auto !important; padding: .2rem .65rem; border-radius: 5px !important;
  border: 1px solid rgba(49,51,63,.25); background: #fff; color: #31333F; }
[class*="st-key-f_categorias"] button p { font-size: .8rem !important; font-weight: 600; line-height: 1.3; margin: 0; }
[class*="st-key-f_categorias"] button:hover { border-color: rgba(60,64,67,.55); background: #f3f4f6; }
/* Selecionada: cinzento ligeiramente mais escuro (aria-pressed é o atributo padrão; os outros são alternativas de versões do Streamlit) */
[class*="st-key-f_categorias"] button[aria-pressed="true"], [class*="st-key-f_categorias"] button[aria-checked="true"],
[class*="st-key-f_categorias"] button[data-selected="true"], [class*="st-key-f_categorias"] button[data-testid="stBaseButton-pillsActive"] {
  background: #dfe2e6 !important; border-color: rgba(60,64,67,.5) !important; color: #1d2430 !important;
  box-shadow: inset 0 1px 3px rgba(60,64,67,.22) !important; }
[class*="st-key-f_categorias"] button[aria-pressed="true"] p, [class*="st-key-f_categorias"] button[aria-checked="true"] p,
[class*="st-key-f_categorias"] button[data-selected="true"] p, [class*="st-key-f_categorias"] button[data-testid="stBaseButton-pillsActive"] p { color: #1d2430 !important; }

/* Notícia: texto à esquerda, imagem à direita, dentro de um cartão com sombra leve */
.nc { display: flex; gap: 1.4rem; padding: 1rem 1.15rem; margin: .75rem 0; background: #fff; border-radius: 10px;
      border: 1px solid rgba(49,51,63,.10); box-shadow: 0 1px 2px rgba(60,64,67,.16), 0 3px 9px rgba(60,64,67,.17); }
.nc-texto { flex: 1; min-width: 0; }
.nc-topo { display: flex; flex-wrap: wrap; gap: .35rem; }
.badge, .chip { display: inline-block; font-size: .74rem; padding: 2px 8px; border-radius: 4px; line-height: 1.5; }
.badge { font-weight: 600; color: hsl(var(--h), 70%, 26%); background: hsla(var(--h), 70%, 45%, .14);
         border: 1px solid hsla(var(--h), 60%, 40%, .35); }
.chip { font-weight: 500; color: rgba(49,51,63,.72); border: 1px solid rgba(49,51,63,.22); }
.nc-titulo { display: flex; align-items: flex-start; justify-content: space-between; gap: .6rem;
             font-size: 1.16rem; font-weight: 600; line-height: 1.32; letter-spacing: -.01em; margin: .5rem 0 .2rem; }
.nc-titulo a:first-child { min-width: 0; }
/* Botão para copiar a ligação da notícia (dois retângulos sobrepostos) */
.copiar { flex: none; width: 1.7rem; height: 1.7rem; margin: -.15rem -.2rem 0 0; border-radius: 6px; cursor: pointer;
          position: relative; color: #6b7686; }
.copiar::before { content: ""; position: absolute; inset: 0; margin: auto; width: 1.05rem; height: 1.05rem; background-color: currentColor;
                  -webkit-mask: var(--ic-copiar) center / contain no-repeat; mask: var(--ic-copiar) center / contain no-repeat; }
.copiar:hover, .copiar:focus-visible { background: rgba(14,124,155,.1); color: #0E7C9B; outline: none; }
.copiar.ok { color: #2e9e5b; }
.copiar.ok::before { -webkit-mask-image: var(--ic-visto); mask-image: var(--ic-visto); }
.nc-titulo a { color: #1d2430 !important; text-decoration: underline !important; text-decoration-color: transparent !important;
               text-decoration-thickness: 1.5px !important; text-underline-offset: 3px; }
.nc-titulo a:hover { text-decoration-color: currentColor !important; }
/* Artigos já abertos: sublinhados e em tom mais claro */
.nc-titulo a:visited { color: #5a6577 !important; text-decoration-color: #0E7C9B !important; }
.nc-resumo { font-family: "Source Sans Pro", sans-serif; font-size: .88rem; line-height: 1.5; color: rgba(49,51,63,.85);
             margin: .2rem 0 0; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.nc-meta { font-size: .72rem; color: rgba(49,51,63,.6); margin-top: .5rem; }
.nc-meta strong { color: #31333F; font-weight: 600; margin-right: .6rem; }
.nc-img { flex: none; width: 208px; height: 128px; border-radius: 8px; overflow: hidden; position: relative;
          display: flex; align-items: center; justify-content: center; text-align: center; padding: .5rem;
          background: hsla(var(--h), 55%, 50%, .13); font-size: 1.35rem; font-weight: 700; letter-spacing: -.02em; line-height: 1.1; }
.nc a.nc-img, .nc a.nc-img:visited { color: hsla(var(--h), 55%, 32%, .8) !important; text-decoration: none !important; }
.nc-img img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; margin: 0; }
@media (max-width: 640px) {
  .block-container { padding: .7rem .75rem 3rem !important; }
  .hub-titulo { font-size: 1.4rem !important; }
  .nc { gap: .75rem; padding: .8rem .85rem; margin: .55rem 0; }
  .nc-img { width: 84px; height: 84px; font-size: .78rem; }
  .nc-titulo { font-size: 1rem; }
  .nc-resumo { font-size: .82rem; -webkit-line-clamp: 2; }
  [class*="st-key-f_categorias"] button { padding: .15rem .5rem; }
  [class*="st-key-f_categorias"] button p { font-size: .74rem !important; }
  [class*="st-key-pag_"] button { min-width: 2.2rem; padding: 0 .35rem; }
}

/* Paginação */
.pag-info { text-align: center; font-size: .82rem; color: rgba(49,51,63,.6); margin-top: 1.4rem; }
[class*="st-key-pag_"] button { min-width: 2.6rem; }

.est-linha { display: flex; justify-content: space-between; font-size: .86rem; margin-top: .45rem; }
.est-barra { height: 5px; border-radius: 3px; background: rgba(128,128,128,.22); overflow: hidden; margin-top: 3px; }
.est-barra i { display: block; height: 100%; background: #0E7C9B; border-radius: 3px; }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: .45rem; }
.dot.ok { background: #2e9e5b; } .dot.falha { background: #d64545; }
.fonte-linha { font-size: .84rem; line-height: 1.5; margin-bottom: .45rem; }
.fonte-linha small { opacity: .7; display: block; margin-left: 1.15rem; }
.rodape { font-size: .8rem; opacity: .7; margin-top: 2rem; line-height: 1.6; }
.st-key-btn_atualizar, .st-key-btn_atualizar [data-testid="stButton"], .st-key-btn_atualizar button { width: 100% !important; }
.st-key-btn_atualizar button { font-weight: 650; }
.est-final { height: .6rem; }
.rodape-autor { font-family: "Schibsted Grotesk", "Source Sans Pro", sans-serif; font-size: .78rem; font-weight: 600;
  color: rgba(49,51,63,.7); text-align: right; border-top: 1px solid rgba(49,51,63,.15); margin-top: .9rem; padding-top: .55rem; }
</style>
"""

# O Streamlit não executa JavaScript em st.markdown. Este script corre num componente e instala, uma só vez,
# um ouvinte de cliques na página principal: ao clicar em .copiar, copia a ligação do título do cartão.
SCRIPT_COPIAR = """
<script>
(function () {
  var pai = window.parent;
  if (pai.__hubCopiar) return;
  var instalar = function () {
    if (window.__hubCopiar) return;
    window.__hubCopiar = true;
    function copiar(texto) {
      if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(texto);
      return new Promise(function (ok, falha) {
        var t = document.createElement('textarea');
        t.value = texto; t.style.position = 'fixed'; t.style.opacity = '0';
        document.body.appendChild(t); t.select();
        try { document.execCommand('copy') ? ok() : falha(); } catch (e) { falha(e); }
        document.body.removeChild(t);
      });
    }
    function acionar(el) {
      var cartao = el.closest('.nc');
      var a = cartao && cartao.querySelector('.nc-titulo a');
      if (!a) return;
      copiar(a.getAttribute('href')).then(function () {
        el.classList.add('ok'); el.title = 'Ligação copiada';
        setTimeout(function () { el.classList.remove('ok'); el.title = 'Copiar ligação'; }, 1600);
      }, function () {});
    }
    document.addEventListener('click', function (e) {
      var el = e.target.closest && e.target.closest('.copiar');
      if (el) { e.preventDefault(); acionar(el); }
    });
    document.addEventListener('keydown', function (e) {
      var el = e.target.closest && e.target.closest('.copiar');
      if (el && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); acionar(el); }
    });
  };
  var s = pai.document.createElement('script');
  s.textContent = '(' + instalar.toString() + ')();';
  pai.document.head.appendChild(s);
  pai.__hubCopiar = true;
})();
</script>
"""

# ---------------------------------------------------------------------------
# Funções auxiliares
# ---------------------------------------------------------------------------


def executar_script(codigo_html: str) -> None:
    """Corre um script numa moldura invisível. st.components.v1.html está a ser retirado do Streamlit: usa-se st.iframe quando existe."""
    if hasattr(st, "iframe"):
        try:
            st.iframe(codigo_html, height=1)
            return
        except Exception:
            pass
    components.html(codigo_html, height=0)


def esc(texto: object) -> str:
    """Escapa HTML e o símbolo $ (que o Markdown pode tratar como fórmula)."""
    return html.escape(str(texto or ""), quote=True).replace("$", "&#36;")


def para_hora_local(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(FUSO_LISBOA)


def tempo_relativo(dt: datetime) -> str:
    segundos = int((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
    if segundos < 0:
        return ""
    if segundos < 3600:
        return f"há {max(segundos // 60, 1)} min"
    if segundos < 86400:
        return f"há {segundos // 3600} h"
    dias = segundos // 86400
    if dias < 30:
        return "há 1 dia" if dias == 1 else f"há {dias} dias"
    return ""


def resumir(texto: str, titulo: str, limite: int = 320) -> str:
    """Limpa restos de feeds WordPress e corta o resumo numa palavra inteira."""
    texto = re.sub(r"\s*(O post|The post|O artigo)\s.+?(apareceu primeiro em|appeared first on)\s.+$", "", texto or "", flags=re.I)
    texto = re.sub(r"\s*\[(…|\.\.\.)\]\s*", " ", texto).strip()
    if not texto or texto.strip().lower() == titulo.strip().lower():
        return ""
    if len(texto) <= limite:
        return texto
    return texto[:limite].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "…"


def ligacao_segura(url: str) -> Optional[str]:
    partes = urlsplit(url or "")
    return url if partes.scheme in ("http", "https") and partes.netloc else None


def cartao(noticia: dict) -> str:
    """HTML de um cartão. O título é o publicado pela fonte; bloqueadas nunca chegam aqui."""
    link = ligacao_segura(noticia["link"])
    if noticia.get("acesso") == "bloqueado" or not link:
        return ""
    titulo = esc(noticia["titulo"])
    topicos = [t for t in (noticia.get("topicos") or "").split("|") if t] or [noticia.get("categoria") or "Outras"]
    principal = noticia.get("categoria") or topicos[0]
    secundarios = [t for t in topicos if t != principal][:2]

    matiz = MATIZ_CATEGORIA.get(principal, 200)
    etiquetas = f'<span class="badge" style="--h:{matiz}">{esc(principal)}</span>'
    etiquetas += "".join(f'<span class="chip">{esc(t)}</span>' for t in secundarios)
    if noticia.get("acesso") == "nao_verificado":
        etiquetas += '<span class="chip" title="Não foi possível confirmar se o artigo é de acesso livre">acesso por confirmar</span>'

    publicada = para_hora_local(noticia.get("data_publicacao"))
    if publicada:
        relativo = tempo_relativo(publicada)
        data = f"{publicada:%d/%m/%Y %H:%M}" + (f" ({relativo})" if relativo else "")
    else:
        recolhida = para_hora_local(noticia.get("criado_em"))
        data = f"Recolhida em {recolhida:%d/%m/%Y %H:%M}" if recolhida else "Data não indicada"

    resumo = resumir(noticia.get("resumo", ""), noticia["titulo"])
    corpo_resumo = f'<p class="nc-resumo">{esc(resumo)}</p>' if resumo else ""

    # Sem imagem (ou se a imagem não carregar) fica visível o nome da fonte sobre a cor da categoria.
    imagem = ligacao_segura(noticia.get("imagem") or "")
    foto = f'<img src="{esc(imagem)}" alt="" referrerpolicy="no-referrer">' if imagem else ""
    return (
        f'<div class="nc"><div class="nc-texto"><div class="nc-topo">{etiquetas}</div>'
        f'<div class="nc-titulo"><a href="{esc(link)}" target="_blank" rel="noopener noreferrer">{titulo}</a>'
        '<span class="copiar" role="button" tabindex="0" title="Copiar ligação" aria-label="Copiar ligação"></span></div>'
        f"{corpo_resumo}"
        f'<div class="nc-meta"><strong>{esc(noticia["fonte"])}</strong>{esc(data)}</div></div>'
        f'<a class="nc-img" style="--h:{matiz}" href="{esc(link)}" target="_blank" rel="noopener noreferrer" tabindex="-1">'
        f'{esc(noticia["fonte"])}{foto}</a></div>'
    )


def paginas_visiveis(atual: int, total: int) -> list[Optional[int]]:
    """Números de página a mostrar; None marca reticências (ex.: 1 … 4 5 6 … 12)."""
    if total <= 7:
        return list(range(1, total + 1))
    numeros = {1, total, atual - 1, atual, atual + 1}
    if atual <= 4:
        numeros |= {2, 3, 4, 5}
    if atual >= total - 3:
        numeros |= {total - 4, total - 3, total - 2, total - 1}
    resultado: list[Optional[int]] = []
    anterior = 0
    for n in sorted(x for x in numeros if 1 <= x <= total):
        if n - anterior > 1:
            resultado.append(None)
        resultado.append(n)
        anterior = n
    return resultado


def ir_para_pagina(numero: int) -> None:
    st.session_state["pagina"] = numero
    st.session_state["subir_ao_topo"] = True


def desenhar_paginacao(atual: int, total: int) -> None:
    """Botões numerados no fundo da página."""
    if total <= 1:
        return
    try:
        barra = st.container(horizontal=True, horizontal_alignment="center", gap="small")
    except TypeError:  # versões do Streamlit anteriores a 1.50
        barra = st.container()
    with barra:
        st.button("‹ Anterior", key="pag_anterior", disabled=atual <= 1, on_click=ir_para_pagina, args=(atual - 1,))
        for indice, numero in enumerate(paginas_visiveis(atual, total)):
            if numero is None:
                st.button("…", key=f"pag_ret_{indice}", disabled=True)
            else:
                st.button(
                    str(numero), key=f"pag_{numero}", on_click=ir_para_pagina, args=(numero,),
                    type="primary" if numero == atual else "secondary",
                )
        st.button("Seguinte ›", key="pag_seguinte", disabled=atual >= total, on_click=ir_para_pagina, args=(atual + 1,))


# ---------------------------------------------------------------------------
# Base de dados e atualização automática
# ---------------------------------------------------------------------------


@st.cache_resource
def preparar_base_dados() -> bool:
    criar_base_dados(DB_PATH)
    return True


@st.cache_resource(ttl=3600, show_spinner=False)
def preencher_imagens_em_segundo_plano() -> bool:
    """Notícias sem imagem (guardadas antes de a recolha existir, ou depois de a base ser reposta) ganham-na sem esperar
    pela próxima atualização. Corre fora do pedido da página: as imagens aparecem quando a página for recarregada."""
    def tarefa() -> None:
        try:
            preencher_imagens_pendentes(DB_PATH)
        except Exception:
            pass

    threading.Thread(target=tarefa, daemon=True).start()
    return True


def base_desatualizada() -> bool:
    ultima = para_hora_local(obter_meta("ultima_actualizacao", DB_PATH))
    if ultima is None:
        return True
    return datetime.now(timezone.utc) - ultima.astimezone(timezone.utc) > timedelta(hours=ATUALIZAR_AUTOMATICAMENTE_CADA_HORAS)


@st.cache_resource(
    ttl=ATUALIZAR_AUTOMATICAMENTE_CADA_HORAS * 3600,
    show_spinner="A recolher as notícias mais recentes das fontes. A primeira carga pode demorar cerca de um minuto.",
)
def atualizacao_automatica() -> dict:
    """Corre no máximo uma vez por intervalo e por servidor, mesmo com vários utilizadores."""
    try:
        return actualizar_base_dados(DB_PATH)
    except Exception as exc:  # a aplicação nunca deve falhar por causa da recolha
        return {"erro": str(exc)}


@st.cache_data(ttl=600, show_spinner=False)
def procurar_noticias(texto: str, categorias: tuple, periodo: str, ordem: str, versao: str) -> list[dict]:
    """Todas as notícias que correspondem aos filtros. versao muda a cada atualização e renova a cache."""
    intervalo = OPCOES_PERIODO[periodo]
    return listar_noticias(
        DB_PATH,
        limite=100_000,
        keyword=texto,
        categorias=list(categorias) or None,
        desde=(datetime.now(timezone.utc) - intervalo) if intervalo else None,
        ordem=OPCOES_ORDEM[ordem],
    )


# ---------------------------------------------------------------------------
# Filtro de categorias (topo da página)
# ---------------------------------------------------------------------------


def desenhar_filtro_categorias() -> tuple:
    """Categorias como etiquetas selecionáveis (uma ou várias). Devolve as escolhidas."""
    existentes = [c["categoria"] for c in listar_categorias(DB_PATH)]
    if not existentes:
        return ()
    opcoes = [c for c in ORDEM_FILTRO_TOPO if c in existentes] + [c for c in existentes if c not in ORDEM_FILTRO_TOPO]
    # Se uma categoria deixar de existir (ex.: após uma atualização), a escolha anterior é descartada.
    st.session_state["f_categorias"] = [c for c in st.session_state.get("f_categorias", []) if c in opcoes]
    escolhidas = st.pills("Categorias", opcoes, selection_mode="multi", key="f_categorias", label_visibility="collapsed")
    return tuple(escolhidas or ())


# ---------------------------------------------------------------------------
# Barra lateral
# ---------------------------------------------------------------------------


def desenhar_barra_lateral() -> dict:
    with st.sidebar:
        if st.button("🔄 Atualizar Hub", type="primary", key="btn_atualizar"):
            with st.spinner("A recolher notícias e a verificar o acesso a cada artigo. Pode demorar cerca de um minuto."):
                try:
                    st.session_state["resultado_atualizacao"] = ("ok", actualizar_base_dados(DB_PATH))
                except Exception as exc:
                    st.session_state["resultado_atualizacao"] = ("erro", str(exc))
            st.session_state["pagina"] = 1
            st.rerun()

        ultima = para_hora_local(obter_meta("ultima_actualizacao", DB_PATH))
        st.caption(f"Última atualização: {ultima:%d/%m/%Y às %H:%M}" if ultima else "Ainda sem atualização.")

        contagens = {item["fonte"]: item["total"] for item in contar_por_fonte(DB_PATH)}
        total = sum(contagens.values())
        nomes = [f["nome"] for f in FONTES] + [n for n in contagens if n not in {f["nome"] for f in FONTES}]

        st.markdown("### Filtros")
        st.text_input("Pesquisar", key="f_pesquisa", placeholder="ex.: 5G, data center")
        periodo = st.selectbox("Período", list(OPCOES_PERIODO), key="f_periodo")
        ordem = st.selectbox("Ordenar por", list(OPCOES_ORDEM), key="f_ordem")

        st.metric("Notícias recolhidas", total)
        with st.expander("Estatísticas por fonte"):
            maximo = max(contagens.values(), default=1) or 1
            barras = "".join(
                f'<div class="est-linha"><span>{esc(n)}</span><span>{contagens.get(n, 0)}</span></div>'
                f'<div class="est-barra"><i style="width:{round(100 * contagens.get(n, 0) / maximo)}%"></i></div>'
                for n in nomes
            )
            st.markdown(barras + '<div class="est-final"></div>', unsafe_allow_html=True)

        with st.expander("Estado das fontes"):
            resumo = obter_meta("ultimo_resumo", DB_PATH) or {}
            fontes = resumo.get("fontes", {})
            if not fontes:
                st.caption("Sem dados da última atualização.")
            for nome, dados in fontes.items():
                bom = str(dados.get("estado", "")).startswith("ok")
                aviso = f"<small>{esc(dados['avisos'][0])}</small>" if dados.get("avisos") and not bom else ""
                st.markdown(
                    f'<div class="fonte-linha"><span class="dot {"ok" if bom else "falha"}"></span>'
                    f'<strong>{esc(nome)}</strong>: {esc(dados.get("estado", ""))}, '
                    f'{dados.get("filtrados", 0)} aceites em {dados.get("recolhidos", 0)}{aviso}</div>',
                    unsafe_allow_html=True,
                )
            if resumo.get("total_bloqueadas"):
                st.caption(f"Na última atualização, {resumo['total_bloqueadas']} notícias de acesso reservado foram ocultadas.")

    return {
        "texto": st.session_state.get("f_pesquisa", ""),
        "periodo": periodo,
        "ordem": ordem,
        "total_base": total,
    }


# ---------------------------------------------------------------------------
# Página principal
# ---------------------------------------------------------------------------


def main() -> None:
    st.markdown(ESTILO, unsafe_allow_html=True)
    preparar_base_dados()
    if base_desatualizada():
        atualizacao_automatica()
    preencher_imagens_em_segundo_plano()

    if st.session_state.pop("subir_ao_topo", False):
        # Depois de mudar de página, volta ao início da lista.
        executar_script(
            "<script>const t = window.parent.document.querySelector('.hub-titulo');"
            "if (t) t.scrollIntoView({block: 'start'});</script>"
        )
    st.markdown('<h1 class="hub-titulo">📡 Hub de Inovação - Portugal</h1>', unsafe_allow_html=True)

    categorias = desenhar_filtro_categorias()
    filtros = desenhar_barra_lateral()

    resultado = st.session_state.pop("resultado_atualizacao", None)
    if resultado:
        tipo, dados = resultado
        if tipo == "erro":
            st.error(f"A atualização falhou: {dados}")
        elif dados.get("total_recolhidos", 0) == 0:
            st.error("Não foi possível obter notícias de nenhuma fonte. Verifique a ligação à Internet e o painel «Estado das fontes» na barra lateral.")

    if filtros["total_base"] == 0:
        st.info("A base de dados está vazia. Clique em «🔄 Atualizar Hub» na barra lateral para recolher as notícias das fontes.")
        return

    # Novos filtros voltam sempre à primeira página.
    assinatura = (filtros["texto"], categorias, filtros["periodo"], filtros["ordem"])
    if st.session_state.get("assinatura") != assinatura:
        st.session_state["assinatura"] = assinatura
        st.session_state["pagina"] = 1

    try:
        noticias = procurar_noticias(*assinatura, versao=str(obter_meta("ultima_actualizacao", DB_PATH)))
    except Exception as exc:
        st.error(f"Não foi possível ler a base de dados: {exc}")
        return

    if not noticias:
        st.warning("Nenhuma notícia corresponde aos filtros selecionados. Experimente alargar o período ou desativar categorias.")
        return

    total_paginas = -(-len(noticias) // NOTICIAS_POR_PAGINA)
    pagina = min(max(int(st.session_state.get("pagina", 1)), 1), total_paginas)
    st.session_state["pagina"] = pagina
    inicio = (pagina - 1) * NOTICIAS_POR_PAGINA
    mostrar = noticias[inicio:inicio + NOTICIAS_POR_PAGINA]

    st.markdown(
        f'<div class="hub-info">A mostrar {inicio + 1}–{inicio + len(mostrar)} de {len(noticias)} notícias</div>',
        unsafe_allow_html=True,
    )
    st.markdown("".join(cartao(n) for n in mostrar), unsafe_allow_html=True)

    if total_paginas > 1:
        st.markdown(f'<div class="pag-info">Página {pagina} de {total_paginas}</div>', unsafe_allow_html=True)
    desenhar_paginacao(pagina, total_paginas)

    nomes_fontes = ", ".join(f["nome"] for f in FONTES)
    st.markdown(
        f'<div class="rodape">Fontes monitorizadas: {esc(nomes_fontes)}. Só são apresentadas notícias com referência a Portugal '
        "e de acesso livre; os artigos reservados a assinantes são ocultados. Os títulos e as ligações são os publicados pelas fontes. "
        "Ferramenta de acompanhamento informativo, sem carácter oficial.</div>"
        '<div class="rodape-autor">Autor: FVC, nov.2026</div>',
        unsafe_allow_html=True,
    )
    executar_script(SCRIPT_COPIAR)


main()
