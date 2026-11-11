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
import streamlit.components.v1 as components

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
@import url('https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600;700&display=swap');
.block-container { padding-top: 2.2rem; max-width: 1180px; }
.hub-titulo, .hub-info, .nc, .pag-info { font-family: "Schibsted Grotesk", "Source Sans Pro", sans-serif; }
.hub-titulo { font-size: 2.05rem; font-weight: 700; line-height: 1.15; letter-spacing: -.02em; margin: 0 0 .45rem; padding: 0; }
.hub-info { font-size: .86rem; color: rgba(49,51,63,.62); padding-bottom: .9rem; margin-bottom: .2rem;
            border-bottom: 1px solid rgba(49,51,63,.12); }

/* Notícia: texto à esquerda, imagem à direita */
.nc { display: flex; gap: 1.6rem; padding: 1.15rem 0; border-bottom: 1px solid rgba(49,51,63,.1); }
.nc-texto { flex: 1; min-width: 0; }
.nc-topo { display: flex; flex-wrap: wrap; gap: .35rem; }
.badge, .chip { display: inline-block; font-size: .74rem; padding: 2px 8px; border-radius: 4px; line-height: 1.5; }
.badge { font-weight: 600; color: hsl(var(--h), 70%, 26%); background: hsla(var(--h), 70%, 45%, .14);
         border: 1px solid hsla(var(--h), 60%, 40%, .35); }
.chip { font-weight: 500; color: rgba(49,51,63,.72); border: 1px solid rgba(49,51,63,.22); }
.nc-titulo { font-size: 1.16rem; font-weight: 600; line-height: 1.32; letter-spacing: -.01em; margin: .5rem 0 .2rem; }
.nc-titulo a { color: #1d2430 !important; text-decoration: underline !important; text-decoration-color: transparent !important;
               text-decoration-thickness: 1.5px !important; text-underline-offset: 3px; }
.nc-titulo a:hover { text-decoration-color: currentColor !important; }
/* Artigos já abertos: sublinhados e em tom mais claro */
.nc-titulo a:visited { color: #5a6577 !important; text-decoration-color: #0E7C9B !important; }
.nc-resumo { font-family: "Source Sans Pro", sans-serif; font-size: .95rem; line-height: 1.55; color: rgba(49,51,63,.85);
             margin: .2rem 0 0; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.nc-meta { font-size: .8rem; color: rgba(49,51,63,.62); margin-top: .5rem; }
.nc-meta strong { color: #31333F; font-weight: 600; margin-right: .6rem; }
.nc-img { flex: none; width: 208px; height: 128px; border-radius: 8px; overflow: hidden; position: relative;
          display: flex; align-items: center; justify-content: center; text-align: center; padding: .5rem;
          background: hsla(var(--h), 55%, 50%, .13); font-size: 1.35rem; font-weight: 700; letter-spacing: -.02em; line-height: 1.1; }
.nc a.nc-img, .nc a.nc-img:visited { color: hsla(var(--h), 55%, 32%, .8) !important; text-decoration: none !important; }
.nc-img img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; margin: 0; }
@media (max-width: 640px) {
  .nc { gap: .9rem; }
  .nc-img { width: 96px; height: 72px; font-size: .85rem; }
  .nc-titulo { font-size: 1.02rem; }
  .nc-resumo { display: none; }
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

    # Sem imagem (ou se a imagem não carregar) fica visível o nome da fonte sobre a cor da categoria.
    imagem = ligacao_segura(noticia.get("imagem") or "")
    foto = f'<img src="{esc(imagem)}" alt="" loading="lazy" referrerpolicy="no-referrer">' if imagem else ""
    return (
        f'<div class="nc"><div class="nc-texto"><div class="nc-topo">{etiquetas}</div>'
        f'<div class="nc-titulo"><a href="{esc(link)}" target="_blank" rel="noopener noreferrer">{titulo}</a></div>'
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
def procurar_noticias(texto: str, categoria: Optional[str], fontes: tuple, periodo: str, ordem: str, versao: str) -> list[dict]:
    """Todas as notícias que correspondem aos filtros. versao muda a cada atualização e renova a cache."""
    intervalo = OPCOES_PERIODO[periodo]
    return listar_noticias(
        DB_PATH,
        limite=100_000,
        keyword=texto,
        categorias=[categoria] if categoria else None,
        fontes=list(fontes) or None,
        desde=(datetime.now(timezone.utc) - intervalo) if intervalo else None,
        ordem=OPCOES_ORDEM[ordem],
    )


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
            st.session_state["pagina"] = 1
            st.rerun()

        ultima = para_hora_local(obter_meta("ultima_actualizacao", DB_PATH))
        st.caption(f"Última atualização: {ultima:%d/%m/%Y às %H:%M}" if ultima else "Ainda sem atualização.")

        contagens = {item["fonte"]: item["total"] for item in contar_por_fonte(DB_PATH)}
        total = sum(contagens.values())
        nomes = [f["nome"] for f in FONTES] + [n for n in contagens if n not in {f["nome"] for f in FONTES}]

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

        st.markdown("### Estatísticas")
        st.metric("Notícias disponíveis", total)
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
        "categoria": None if categoria == TODAS else categoria,
        "fontes": tuple(fontes_escolhidas),
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

    if st.session_state.pop("subir_ao_topo", False):
        # Depois de mudar de página, volta ao início da lista.
        components.html(
            "<script>const t = window.parent.document.querySelector('.hub-titulo');"
            "if (t) t.scrollIntoView({block: 'start'});</script>",
            height=0,
        )
    st.markdown('<h1 class="hub-titulo">📡 Hub de Inovação - Portugal</h1>', unsafe_allow_html=True)

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

    # Novos filtros voltam sempre à primeira página.
    assinatura = (filtros["texto"], filtros["categoria"], filtros["fontes"], filtros["periodo"], filtros["ordem"])
    if st.session_state.get("assinatura") != assinatura:
        st.session_state["assinatura"] = assinatura
        st.session_state["pagina"] = 1

    try:
        noticias = procurar_noticias(*assinatura, versao=str(obter_meta("ultima_actualizacao", DB_PATH)))
    except Exception as exc:
        st.error(f"Não foi possível ler a base de dados: {exc}")
        return

    if not noticias:
        st.warning("Nenhuma notícia corresponde aos filtros selecionados. Experimente alargar o período ou limpar os filtros.")
        return

    total_paginas = -(-len(noticias) // NOTICIAS_POR_PAGINA)
    pagina = min(max(int(st.session_state.get("pagina", 1)), 1), total_paginas)
    st.session_state["pagina"] = pagina
    inicio = (pagina - 1) * NOTICIAS_POR_PAGINA
    mostrar = noticias[inicio:inicio + NOTICIAS_POR_PAGINA]

    st.markdown(
        f'<div class="hub-info">A mostrar {inicio + 1}–{inicio + len(mostrar)} de {len(noticias)} notícias. '
        "Os títulos sublinhados correspondem a artigos que já abriu.</div>",
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
        "Ferramenta de acompanhamento informativo, sem carácter oficial.<br>"
        "<strong>Autor: FVC, 2026</strong></div>",
        unsafe_allow_html=True,
    )


main()
