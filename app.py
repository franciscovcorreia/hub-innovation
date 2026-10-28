"""
app.py

Hub de Inovação - Portugal
Interface Streamlit sobre a base de dados criada por database_and_scraper.py.

Execução local:   streamlit run app.py
Sem APIs de inteligência artificial: toda a seleção é feita por regras e palavras-chave.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

import streamlit as st

from database_and_scraper import (
    DB_PATH,
    FONTES,
    FUSO_LISBOA,
    actualizar_base_dados,
    contar_por_fonte,
    criar_base_dados,
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

NOTICIAS_POR_PAGINA = 24
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
TODAS = "Todas as categorias"

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
.block-container { padding-top: 2.2rem; max-width: 1180px; }
.hub-titulo { font-size: 2rem; font-weight: 750; line-height: 1.2; margin: 0 0 .6rem; letter-spacing: -.01em; }
.hub-nota { border-left: 4px solid #0E7C9B; background: rgba(14,124,155,.09); border-radius: 0 8px 8px 0;
            padding: .85rem 1.1rem; font-size: .95rem; line-height: 1.6; margin-bottom: 1.4rem; }
.nc { border: 1px solid rgba(128,128,128,.30); border-radius: 12px; padding: 1rem 1.2rem 1.05rem;
      margin: 0 0 .85rem; background: rgba(128,128,128,.05); }
.nc:hover { border-color: rgba(14,124,155,.75); background: rgba(14,124,155,.07); }
.nc-topo { display: flex; flex-wrap: wrap; gap: .4rem; align-items: center; }
.nc-titulo { font-size: 1.12rem; font-weight: 650; line-height: 1.38; margin: .55rem 0 .35rem; }
.nc-titulo a { color: inherit !important; text-decoration: none !important; }
.nc-titulo a::after { content: " \\2197"; font-size: .85em; opacity: .55; }
.nc-titulo a:hover { text-decoration: underline !important; text-underline-offset: 3px; }
.nc-meta { font-size: .82rem; opacity: .78; display: flex; flex-wrap: wrap; gap: .25rem .75rem; }
.nc-resumo { font-size: .93rem; line-height: 1.6; margin: .6rem 0 0; opacity: .92; }
.badge { display: inline-block; font-size: .7rem; font-weight: 650; letter-spacing: .04em; text-transform: uppercase;
         padding: 2px 10px; border-radius: 999px; border: 1px solid hsla(var(--h), 65%, 48%, .6);
         background: hsla(var(--h), 65%, 48%, .17); }
.chip { display: inline-block; font-size: .7rem; padding: 2px 9px; border-radius: 999px;
        border: 1px solid rgba(128,128,128,.35); opacity: .8; }
.est-linha { display: flex; justify-content: space-between; font-size: .86rem; margin-top: .45rem; }
.est-barra { height: 5px; border-radius: 3px; background: rgba(128,128,128,.22); overflow: hidden; margin-top: 3px; }
.est-barra i { display: block; height: 100%; background: #0E7C9B; border-radius: 3px; }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: .45rem; }
.dot.ok { background: #2e9e5b; } .dot.falha { background: #d64545; }
.fonte-linha { font-size: .84rem; line-height: 1.5; margin-bottom: .45rem; }
.fonte-linha small { opacity: .7; display: block; margin-left: 1.15rem; }
.rodape { font-size: .8rem; opacity: .7; margin-top: 2rem; line-height: 1.6; }
.st-key-btn_atualizar, .st-key-btn_atualizar [data-testid="stButton"], .st-key-btn_atualizar button,
.st-key-btn_limpar, .st-key-btn_limpar [data-testid="stButton"], .st-key-btn_limpar button { width: 100% !important; }
.st-key-btn_atualizar button { font-weight: 650; }
.est-final { height: .6rem; }
</style>
"""

# ---------------------------------------------------------------------------
# Funções auxiliares
# ---------------------------------------------------------------------------


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
    return (
        f'<div class="nc"><div class="nc-topo">{etiquetas}</div>'
        f'<div class="nc-titulo"><a href="{esc(link)}" target="_blank" rel="noopener noreferrer">{titulo}</a></div>'
        f'<div class="nc-meta"><span><strong>{esc(noticia["fonte"])}</strong></span><span>{esc(data)}</span></div>'
        f"{corpo_resumo}</div>"
    )


# ---------------------------------------------------------------------------
# Base de dados e atualização automática
# ---------------------------------------------------------------------------


@st.cache_resource
def preparar_base_dados() -> bool:
    criar_base_dados(DB_PATH)
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


def limpar_filtros() -> None:
    st.session_state["f_pesquisa"] = ""
    st.session_state["f_categoria"] = TODAS
    st.session_state["f_fontes"] = []
    st.session_state["f_periodo"] = "Todo o período"
    st.session_state["f_ordem"] = "Mais recentes"


# ---------------------------------------------------------------------------
# Barra lateral
# ---------------------------------------------------------------------------


def desenhar_barra_lateral() -> dict:
    with st.sidebar:
        st.markdown("### Atualização")
        if st.button("🔄 Atualizar Notícias Agora", type="primary", key="btn_atualizar"):
            with st.spinner("A recolher notícias e a verificar o acesso a cada artigo. Pode demorar cerca de um minuto."):
                try:
                    st.session_state["resultado_atualizacao"] = ("ok", actualizar_base_dados(DB_PATH))
                except Exception as exc:
                    st.session_state["resultado_atualizacao"] = ("erro", str(exc))
            st.session_state["limite"] = NOTICIAS_POR_PAGINA
            st.rerun()

        ultima = para_hora_local(obter_meta("ultima_actualizacao", DB_PATH))
        st.caption(f"Última atualização: {ultima:%d/%m/%Y às %H:%M}" if ultima else "Ainda sem atualização.")

        st.markdown("### Estatísticas")
        contagens = {item["fonte"]: item["total"] for item in contar_por_fonte(DB_PATH)}
        total = sum(contagens.values())
        st.metric("Notícias disponíveis", total)
        nomes = [f["nome"] for f in FONTES] + [n for n in contagens if n not in {f["nome"] for f in FONTES}]
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

        st.markdown("### Filtros")
        st.text_input("Pesquisar", key="f_pesquisa", placeholder="ex.: 5G, data center, espetro")
        categorias = listar_categorias(DB_PATH)
        etiquetas = {c["categoria"]: f'{c["categoria"]} ({c["total"]})' for c in categorias}
        categoria = st.selectbox(
            "Categoria", [TODAS] + [c["categoria"] for c in categorias],
            key="f_categoria", format_func=lambda c: c if c == TODAS else etiquetas.get(c, c),
        )
        fontes_escolhidas = st.multiselect("Fonte", [n for n in nomes if contagens.get(n, 0) > 0], key="f_fontes", placeholder="Todas as fontes")
        periodo = st.selectbox("Período", list(OPCOES_PERIODO), key="f_periodo")
        ordem = st.selectbox("Ordenar por", list(OPCOES_ORDEM), key="f_ordem")
        st.button("Limpar filtros", key="btn_limpar", on_click=limpar_filtros)

    return {
        "texto": st.session_state.get("f_pesquisa", ""),
        "categoria": None if categoria == TODAS else categoria,
        "fontes": fontes_escolhidas,
        "desde": (datetime.now(timezone.utc) - OPCOES_PERIODO[periodo]) if OPCOES_PERIODO[periodo] else None,
        "ordem": OPCOES_ORDEM[ordem],
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

    st.markdown('<h1 class="hub-titulo">📡 Hub de Inovação - Portugal</h1>', unsafe_allow_html=True)
    st.markdown(
        '<div class="hub-nota">Este painel reúne notícias de fontes portuguesas sobre temas de inovação: '
        "telecomunicações, redes 5G e 6G, inteligência artificial e agentes de IA, carros conectados e autónomos, "
        "cloud e data centers, quântica, cabos submarinos, redes e comunicações eletrónicas e drones. "
        "Só são apresentadas notícias com referência a Portugal e de acesso livre; os artigos reservados a assinantes são ocultados.</div>",
        unsafe_allow_html=True,
    )

    filtros = desenhar_barra_lateral()

    resultado = st.session_state.pop("resultado_atualizacao", None)
    if resultado:
        tipo, dados = resultado
        if tipo == "erro":
            st.error(f"A atualização falhou: {dados}")
        elif dados.get("total_recolhidos", 0) == 0:
            st.error("Não foi possível obter notícias de nenhuma fonte. Verifique a ligação à Internet e o painel «Estado das fontes» na barra lateral.")
        else:
            st.success(
                f"Atualização concluída em {dados.get('duracao', 0):.0f} s: {dados.get('total_inseridos', 0)} notícias novas, "
                f"das quais {dados.get('total_bloqueadas', 0)} foram ocultadas por terem acesso reservado."
            )

    if filtros["total_base"] == 0:
        st.info("A base de dados está vazia. Clique em «🔄 Atualizar Notícias Agora» na barra lateral para recolher as notícias das fontes.")
        return

    assinatura = (filtros["texto"], filtros["categoria"], tuple(filtros["fontes"]), filtros["desde"] is None, filtros["ordem"])
    if st.session_state.get("assinatura") != assinatura:
        st.session_state["assinatura"] = assinatura
        st.session_state["limite"] = NOTICIAS_POR_PAGINA
    limite = st.session_state.setdefault("limite", NOTICIAS_POR_PAGINA)

    try:
        noticias = listar_noticias(
            DB_PATH,
            limite=limite + 1,
            keyword=filtros["texto"],
            categorias=[filtros["categoria"]] if filtros["categoria"] else None,
            fontes=filtros["fontes"] or None,
            desde=filtros["desde"],
            ordem=filtros["ordem"],
        )
    except Exception as exc:
        st.error(f"Não foi possível ler a base de dados: {exc}")
        return

    mostrar = noticias[:limite]
    if not mostrar:
        st.warning("Nenhuma notícia corresponde aos filtros selecionados. Experimente alargar o período ou limpar os filtros.")
        return

    st.caption(f"A mostrar {len(mostrar)} notícias" + (" (há mais resultados)" if len(noticias) > limite else ""))
    st.markdown("".join(cartao(n) for n in mostrar), unsafe_allow_html=True)

    if len(noticias) > limite and st.button("Mostrar mais notícias", key="btn_mais"):
        st.session_state["limite"] = limite + NOTICIAS_POR_PAGINA
        st.rerun()

    nomes_fontes = ", ".join(f["nome"] for f in FONTES)
    st.markdown(
        f'<div class="rodape">Fontes monitorizadas: {esc(nomes_fontes)}. Os títulos e as ligações são os publicados pelas fontes. '
        "Ferramenta de acompanhamento informativo, sem carácter oficial.</div>",
        unsafe_allow_html=True,
    )


main()
