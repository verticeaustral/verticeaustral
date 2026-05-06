"""
╔══════════════════════════════════════════════════════════════════╗
║              VÉRTICE AUSTRAL — Motor Geopolítico v3.0            ║
║         Observatorio de Sesgo Mediático y Prognosis de Agenda    ║
╠══════════════════════════════════════════════════════════════════╣
║  CAMBIOS v3.0:                                                   ║
║  · RSS múltiples por portal argentino (cobertura ampliada)       ║
║  · Infobae reemplaza a Página/12 (feeds P12 caídos)             ║
║  · Nueva pestaña: Radar Internacional (NYT, Guardian, El País)  ║
║  · Prompt LLM internacional adaptado a medios anglosajones       ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
from streamlit_cookies_controller import CookieController
import feedparser
import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN GLOBAL v3.0
# ─────────────────────────────────────────────────────────────────────────────

# Feeds argentinos: múltiples URLs por portal, se leen y fusionan.
# Página/12 reemplazada por Infobae (feeds P12 devuelven 0 artículos).
RSS_FEEDS_ARG = {
    "Clarín": [
        "https://www.clarin.com/rss/lo-ultimo/",
        "https://www.clarin.com/rss/politica/",
        "https://www.clarin.com/rss/economia/",
        "https://www.clarin.com/rss/sociedad/",
    ],
    "La Nación": [
        "https://www.lanacion.com.ar/arc/outboundfeeds/rss/",
    ],
    "Infobae": [
        "https://www.infobae.com/feeds/rss/politica/",
        "https://www.infobae.com/feeds/rss/economia/",
        "https://www.infobae.com/feeds/rss/america/",
    ],
}

# Feeds internacionales: NYT, The Guardian, El País.
RSS_FEEDS_INTL = {
    "New York Times": [
        "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Americas.xml",
    ],
    "The Guardian": [
        "https://www.theguardian.com/world/rss",
        "https://www.theguardian.com/politics/rss",
        "https://www.theguardian.com/business/rss",
        "https://www.theguardian.com/world/americas/rss",
    ],
    "El País": [
        "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada",
        "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/internacional/portada",
        "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/america/portada",
    ],
}

LLM_MODEL       = "claude-sonnet-4-5"
HORAS_VENTANA   = 24
LIMITE_CONSULTAS = 2


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 1: EXTRACCIÓN RSS (genérico — sirve para arg e intl)
# ─────────────────────────────────────────────────────────────────────────────

def parsear_fecha(entry) -> Optional[datetime]:
    for campo in ("published_parsed", "updated_parsed"):
        t = getattr(entry, campo, None)
        if t:
            import time
            ts = time.mktime(t)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    return None


def extraer_articulos(keyword: str, feeds_config: dict[str, list[str]]) -> dict[str, list[dict]]:
    """
    Itera sobre feeds_config = {portal: [url1, url2, ...]}.
    Fusiona artículos de todas las URLs de un mismo portal,
    deduplica por título y filtra por keyword y ventana temporal.
    """
    ahora = datetime.now(tz=timezone.utc)
    corte = ahora - timedelta(hours=HORAS_VENTANA)
    resultados = {}

    for portal, urls in feeds_config.items():
        vistos = set()          # para deduplicar por título
        articulos_portal = []

        for url in urls:
            try:
                feed = feedparser.parse(url)
            except Exception:
                continue

            for entry in feed.entries:
                titulo  = getattr(entry, "title", "").strip()
                resumen = getattr(entry, "summary", "")
                link    = getattr(entry, "link", "")

                # Deduplicación
                if titulo in vistos:
                    continue
                vistos.add(titulo)

                # Filtro temporal
                fecha = parsear_fecha(entry)
                if fecha and fecha < corte:
                    continue

                # Filtro keyword
                texto_completo = f"{titulo} {resumen}".lower()
                if keyword.lower() not in texto_completo:
                    continue

                articulos_portal.append({
                    "titulo": titulo,
                    "resumen": resumen,
                    "link":    link,
                    "fecha":   fecha.strftime("%Y-%m-%d %H:%M UTC") if fecha else "s/d",
                    "portal":  portal,
                })

        resultados[portal] = articulos_portal

    return resultados


def armar_corpus(articulos_por_portal: dict[str, list[dict]]) -> str:
    bloques = []
    for portal, arts in articulos_por_portal.items():
        if not arts:
            bloque = f"=== {portal} ===\n[Sin artículos en las últimas 24 horas para esta keyword]"
        else:
            items = "\n".join(
                f"- [{a['fecha']}] {a['titulo']}: {a['resumen'][:300]}..."
                for a in arts
            )
            bloque = f"=== {portal} ===\n{items}"
        bloques.append(bloque)
    return "\n\n".join(bloques)


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 2: LLM — cliente compartido + dos prompts distintos
# ─────────────────────────────────────────────────────────────────────────────

PROMPT_SISTEMA_ARG = """Sos un analista de medios y geopolítica especializado en el ecosistema
mediático latinoamericano. Tu metodología combina el análisis del framing (Entman, 1993) con
la decodificación de la agenda setting. Respondés únicamente en español rioplatense formal."""

PROMPT_SISTEMA_INTL = """You are a senior geopolitical media analyst specializing in
Anglo-Saxon and European press. Your methodology combines framing analysis (Entman, 1993)
with agenda-setting theory. You always respond in formal Argentine Spanish (español rioplatense),
regardless of the language of the source material."""

PROMPT_ARG = """Analizá el siguiente corpus de noticias argentinas sobre el tema "{keyword}",
publicadas en las últimas 24 horas. El corpus está segmentado por portal.

CORPUS:
{corpus}

Devolvé ÚNICAMENTE un JSON válido con esta estructura exacta (sin texto antes ni después,
sin bloques de código markdown):

{{
  "sesgo": {{
    "Clarín": {{
      "omisiones": "...",
      "carga_adjetival": "...",
      "veredicto_framing": "..."
    }},
    "La Nación": {{
      "omisiones": "...",
      "carga_adjetival": "...",
      "veredicto_framing": "..."
    }},
    "Infobae": {{
      "omisiones": "...",
      "carga_adjetival": "...",
      "veredicto_framing": "..."
    }}
  }},
  "prognosis": {{
    "probabilidad_continuidad": <entero 0-100>,
    "razonamiento_continuidad": "...",
    "derivadas_narrativas": [
      {{"escenario": "Principal",  "descripcion": "...", "probabilidad": <entero 0-100>}},
      {{"escenario": "Secundario", "descripcion": "...", "probabilidad": <entero 0-100>}},
      {{"escenario": "Marginal",   "descripcion": "...", "probabilidad": <entero 0-100>}}
    ]
  }}
}}"""

PROMPT_INTL = """Analizá el siguiente corpus de noticias internacionales sobre el tema "{keyword}",
publicadas en las últimas 24 horas. El corpus proviene de tres medios anglosajones/europeos de
referencia global. Respondé en español rioplatense formal.

CORPUS:
{corpus}

Devolvé ÚNICAMENTE un JSON válido con esta estructura exacta (sin texto antes ni después,
sin bloques de código markdown):

{{
  "sesgo": {{
    "New York Times": {{
      "omisiones": "Qué hechos, voces o perspectivas omite o minimiza este medio.",
      "encuadre_editorial": "Análisis del framing predominante: qué marco interpretativo usa el NYT.",
      "posicionamiento_geopolitico": "Una oración sobre el alineamiento geopolítico implícito en su cobertura."
    }},
    "The Guardian": {{
      "omisiones": "...",
      "encuadre_editorial": "...",
      "posicionamiento_geopolitico": "..."
    }},
    "El País": {{
      "omisiones": "...",
      "encuadre_editorial": "...",
      "posicionamiento_geopolitico": "..."
    }}
  }},
  "convergencias": "Qué narrativa comparten los tres medios sobre este tema (el consenso occidental).",
  "angulo_ausente": "Qué perspectiva geopolítica relevante está completamente ausente en los tres medios.",
  "prognosis": {{
    "probabilidad_continuidad": <entero 0-100>,
    "razonamiento_continuidad": "...",
    "derivadas_narrativas": [
      {{"escenario": "Principal",  "descripcion": "...", "probabilidad": <entero 0-100>}},
      {{"escenario": "Secundario", "descripcion": "...", "probabilidad": <entero 0-100>}},
      {{"escenario": "Marginal",   "descripcion": "...", "probabilidad": <entero 0-100>}}
    ]
  }}
}}"""


def get_api_key() -> str:
    try:
        return st.secrets["ANTHROPIC_API_KEY"]
    except (KeyError, FileNotFoundError):
        raise EnvironmentError(
            "API Key no encontrada. Creá .streamlit/secrets.toml con: ANTHROPIC_API_KEY = 'sk-ant-...'"
        )


def llamar_llm(keyword: str, corpus: str, modo: str = "arg") -> dict:
    """
    modo='arg'  → prompt y sistema argentino
    modo='intl' → prompt y sistema internacional
    """
    client = anthropic.Anthropic(api_key=get_api_key())

    sistema  = PROMPT_SISTEMA_ARG  if modo == "arg" else PROMPT_SISTEMA_INTL
    template = PROMPT_ARG          if modo == "arg" else PROMPT_INTL

    prompt_usuario = template.format(keyword=keyword, corpus=corpus)

    message = client.messages.create(
        model=LLM_MODEL,
        max_tokens=4096,
        system=sistema,
        messages=[{"role": "user", "content": prompt_usuario}],
    )

    respuesta_raw   = message.content[0].text.strip()
    respuesta_limpia = re.sub(r"^```(?:json)?\s*", "", respuesta_raw)
    respuesta_limpia = re.sub(r"\s*```$", "", respuesta_limpia).strip()

    try:
        return json.loads(respuesta_limpia)
    except json.JSONDecodeError as e:
        raise ValueError(f"El LLM no devolvió JSON válido.\nError: {e}\n\nRaw:\n{respuesta_raw}")


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 3: INTERFAZ — CSS global
# ─────────────────────────────────────────────────────────────────────────────

def configurar_pagina():
    st.set_page_config(
        page_title="Vértice Austral",
        page_icon="◈",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown("""
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600;700&family=Source+Serif+4:ital,wght@0,300;0,400;0,600;1,300&family=JetBrains+Mono:wght@400;500&display=swap');

      :root {
        --ink:       #1a1a2e;
        --ink-light: #3d3d5c;
        --paper:     #f5f0e8;
        --accent:    #8b1a1a;
        --accent-2:  #1a4a6b;
        --accent-3:  #1a5c3a;   /* verde para pestaña internacional */
        --rule:      #c8b89a;
        --muted:     #7a7060;
        --tag-bg:    #e8e0d0;
        --mono:      'JetBrains Mono', monospace;
      }

      .stApp { background-color: var(--paper); }
      html, body, .stApp { font-family: 'Source Serif 4', serif; color: var(--ink); }

      /* ── Sidebar ── */
      [data-testid="stSidebar"] { background-color: var(--ink) !important; }
      [data-testid="stSidebar"] * { color: var(--paper) !important; }
      [data-testid="stSidebar"] hr { border-color: #3d3d5c !important; }
      .sidebar-title {
        font-family: 'Playfair Display', serif;
        font-size: 1.1rem;
        letter-spacing: 0.08em;
        border-bottom: 1px solid #3d3d5c;
        padding-bottom: 0.8rem;
        margin-bottom: 1rem;
      }

      /* ── Masthead ── */
      .va-masthead {
        border-top: 3px solid var(--ink);
        border-bottom: 1px solid var(--rule);
        padding: 1.2rem 0 1rem;
        margin-bottom: 1.5rem;
        text-align: center;
      }
      .va-title { font-family: 'Playfair Display', serif; font-size: 2.6rem; font-weight: 700; letter-spacing: 0.04em; color: var(--ink); line-height: 1.1; }
      .va-subtitle { font-size: 0.78rem; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-top: 0.3rem; }
      .va-rule { border: none; border-top: 1px solid var(--rule); margin: 0.8rem auto; width: 60%; }

      /* ── Sección headers ── */
      .section-header {
        font-family: 'Playfair Display', serif; font-size: 1.1rem; font-weight: 600;
        color: var(--accent); letter-spacing: 0.08em; text-transform: uppercase;
        border-bottom: 2px solid var(--accent); padding-bottom: 0.3rem; margin-bottom: 1rem;
      }
      .section-header-intl {
        font-family: 'Playfair Display', serif; font-size: 1.1rem; font-weight: 600;
        color: var(--accent-3); letter-spacing: 0.08em; text-transform: uppercase;
        border-bottom: 2px solid var(--accent-3); padding-bottom: 0.3rem; margin-bottom: 1rem;
      }

      /* ── Tarjetas de portal ── */
      .portal-card {
        background: #fff; border: 1px solid var(--rule); border-top: 3px solid var(--accent-2);
        border-radius: 2px; padding: 1.2rem 1.4rem; margin-bottom: 1rem;
        box-shadow: 2px 2px 8px rgba(26,26,46,0.06);
      }
      .portal-card-intl {
        background: #fff; border: 1px solid var(--rule); border-top: 3px solid var(--accent-3);
        border-radius: 2px; padding: 1.2rem 1.4rem; margin-bottom: 1rem;
        box-shadow: 2px 2px 8px rgba(26,46,26,0.06);
      }
      .portal-name { font-family: 'Playfair Display', serif; font-size: 1.05rem; font-weight: 700; color: var(--accent-2); margin-bottom: 0.8rem; border-bottom: 1px solid var(--rule); padding-bottom: 0.4rem; }
      .portal-name-intl { font-family: 'Playfair Display', serif; font-size: 1.05rem; font-weight: 700; color: var(--accent-3); margin-bottom: 0.8rem; border-bottom: 1px solid var(--rule); padding-bottom: 0.4rem; }
      .field-label { font-size: 0.65rem; letter-spacing: 0.18em; text-transform: uppercase; color: var(--muted); font-family: var(--mono); margin-bottom: 0.2rem; }
      .field-value { font-size: 0.92rem; color: var(--ink-light); line-height: 1.55; margin-bottom: 0.9rem; }
      .veredicto-chip { display: inline-block; background: var(--tag-bg); border: 1px solid var(--rule); border-left: 3px solid var(--accent); padding: 0.4rem 0.8rem; font-size: 0.88rem; font-style: italic; color: var(--ink); border-radius: 0 2px 2px 0; line-height: 1.4; }
      .veredicto-chip-intl { display: inline-block; background: #f0f6f2; border: 1px solid var(--rule); border-left: 3px solid var(--accent-3); padding: 0.4rem 0.8rem; font-size: 0.88rem; font-style: italic; color: var(--ink); border-radius: 0 2px 2px 0; line-height: 1.4; }

      /* ── Cajas de convergencia / ángulo ausente (intl) ── */
      .convergencia-box {
        background: #f0f6f2; border: 1px solid #a8c8b8; border-left: 4px solid var(--accent-3);
        padding: 1rem 1.4rem; border-radius: 2px; margin-bottom: 1rem;
      }
      .angulo-box {
        background: #fbf0f0; border: 1px solid #d4a0a0; border-left: 4px solid var(--accent);
        padding: 1rem 1.4rem; border-radius: 2px; margin-bottom: 1rem;
      }

      /* ── Prognosis ── */
      .prognosis-box { background: #fff; border: 1px solid var(--rule); border-top: 3px solid var(--ink); padding: 1.4rem 1.6rem; border-radius: 2px; box-shadow: 2px 2px 8px rgba(26,26,46,0.06); }
      .prob-number { font-family: 'Playfair Display', serif; font-size: 3.2rem; font-weight: 700; color: var(--accent); line-height: 1; }
      .prob-label { font-size: 0.65rem; letter-spacing: 0.18em; text-transform: uppercase; color: var(--muted); font-family: var(--mono); }
      .razonamiento { font-size: 0.9rem; color: var(--ink-light); font-style: italic; margin-top: 0.6rem; line-height: 1.6; }
      .escenario-row { border-bottom: 1px solid var(--rule); padding: 0.7rem 0; display: flex; align-items: flex-start; gap: 1rem; }
      .escenario-tag { font-family: var(--mono); font-size: 0.65rem; letter-spacing: 0.12em; text-transform: uppercase; padding: 0.2rem 0.5rem; border: 1px solid; border-radius: 2px; white-space: nowrap; flex-shrink: 0; }
      .tag-principal  { color: var(--accent);  border-color: var(--accent);  background: #fbf0f0; }
      .tag-secundario { color: var(--accent-2); border-color: var(--accent-2); background: #f0f4fb; }
      .tag-marginal   { color: var(--muted);    border-color: var(--rule);    background: var(--tag-bg); }
      .escenario-texto { font-size: 0.91rem; color: var(--ink-light); line-height: 1.5; }
      .escenario-prob  { font-family: 'Playfair Display', serif; font-size: 1.1rem; font-weight: 700; flex-shrink: 0; margin-left: auto; }

      /* ── Artículos fuente ── */
      .articulo-item { font-size: 0.85rem; color: var(--ink-light); padding: 0.35rem 0; border-bottom: 1px dotted var(--rule); line-height: 1.45; }
      .articulo-fecha { font-family: var(--mono); font-size: 0.7rem; color: var(--muted); }

      /* ── Rate badge ── */
      .rate-badge { display: inline-block; font-family: var(--mono); font-size: 0.75rem; letter-spacing: 0.1em; background: var(--tag-bg); border: 1px solid var(--rule); padding: 0.25rem 0.6rem; border-radius: 2px; color: var(--muted); }
      .rate-badge.agotado { background: #fbf0f0; border-color: var(--accent); color: var(--accent); }

      /* ── CTA SITREP ── */
      .cta-sitrep { margin-top: 2.5rem; border: 1px solid var(--ink); border-left: 4px solid var(--accent); background: #fff; padding: 1.2rem 1.6rem; }
      .cta-texto-principal { font-family: 'Playfair Display', serif; font-size: 1rem; font-weight: 600; color: var(--ink); }
      .cta-texto-secundario { font-size: 0.82rem; color: var(--muted); margin-top: 0.2rem; font-style: italic; }

      /* ── Director ── */
      .director-card { max-width: 720px; margin: 2rem auto; background: #fff; border: 1px solid var(--rule); border-top: 4px solid var(--ink); padding: 2.5rem 3rem; box-shadow: 4px 4px 16px rgba(26,26,46,0.08); }
      .director-nombre { font-family: 'Playfair Display', serif; font-size: 1.8rem; font-weight: 700; color: var(--ink); margin-bottom: 0.2rem; }
      .director-cargo { font-size: 0.72rem; letter-spacing: 0.2em; text-transform: uppercase; color: var(--accent); font-family: var(--mono); margin-bottom: 1.5rem; padding-bottom: 1rem; border-bottom: 1px solid var(--rule); }
      .director-bio { font-size: 0.97rem; line-height: 1.75; color: var(--ink-light); margin-bottom: 1.2rem; }
      .director-contacto-header { font-size: 0.65rem; letter-spacing: 0.18em; text-transform: uppercase; color: var(--muted); font-family: var(--mono); margin-bottom: 0.6rem; border-top: 1px solid var(--rule); padding-top: 1rem; }
      .director-contacto a { color: var(--accent-2) !important; text-decoration: none; }
      .director-contacto a:hover { text-decoration: underline; }

      /* ── Inputs y botones ── */
      .stTextInput > div > div > input { background-color: #fff !important; border: 1px solid var(--rule) !important; border-radius: 2px !important; color: var(--ink) !important; font-family: 'Source Serif 4', serif !important; }
      .stButton > button { background-color: var(--ink) !important; color: var(--paper) !important; border: none !important; border-radius: 2px !important; font-family: 'Playfair Display', serif !important; font-size: 0.9rem !important; letter-spacing: 0.08em !important; padding: 0.5rem 2rem !important; }
      .stButton > button:hover { background-color: var(--accent) !important; }

      .sys-timestamp { font-family: var(--mono); font-size: 0.68rem; color: var(--muted); text-align: right; margin-top: 0.5rem; }

      #MainMenu, footer, .stDeployButton { display: none !important; }
    </style>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────────────────

def render_sidebar() -> str:
    with st.sidebar:
        st.markdown("""
        <div class="sidebar-title">◈ VÉRTICE AUSTRAL</div>
        <div style="font-size:0.65rem; letter-spacing:0.15em; text-transform:uppercase;
                    opacity:0.5; margin-bottom:1.5rem; font-family:'JetBrains Mono',monospace;">
          Observatorio Geopolítico
        </div>
        """, unsafe_allow_html=True)

        st.markdown("---")

        vista = st.radio(
            "Navegación",
            options=[
                "📡  Radar OSINT — Argentina",
                "🌐  Radar Internacional",
                "◈  Acerca del Director",
            ],
            label_visibility="collapsed",
        )

        st.markdown("---")

        # COOKIE: leer contador real del navegador para el badge del sidebar
        consultas_usadas = leer_consultas()
        restantes = max(0, LIMITE_CONSULTAS - consultas_usadas)
        badge_clase = "agotado" if restantes == 0 else ""

        st.markdown(
            f"<div style='font-size:0.65rem; letter-spacing:0.1em; text-transform:uppercase; "
            f"opacity:0.6; font-family:\"JetBrains Mono\",monospace; margin-bottom:0.5rem;'>"
            f"Consultas disponibles</div>"
            f"<span class='rate-badge {badge_clase}'>{restantes} / {LIMITE_CONSULTAS}</span>",
            unsafe_allow_html=True,
        )
        if restantes == 0:
            st.markdown(
                "<div style='font-size:0.72rem; opacity:0.6; margin-top:0.8rem; font-style:italic;'>"
                "Límite del radar abierto alcanzado.</div>",
                unsafe_allow_html=True,
            )

    if "Internacional" in vista:
        return "internacional"
    if "Director" in vista:
        return "director"
    return "radar"


# ─────────────────────────────────────────────────────────────────────────────
# COMPONENTES COMPARTIDOS
# ─────────────────────────────────────────────────────────────────────────────

def render_masthead(subtitulo: str = "Motor de Sesgo · Prognosis de Agenda · Síntesis LLM"):
    st.markdown(f"""
    <div class="va-masthead">
      <div class="va-subtitle">Observatorio Geopolítico Austral · Análisis Mediático Comparativo</div>
      <div class="va-title">◈ VÉRTICE AUSTRAL</div>
      <hr class="va-rule">
      <div class="va-subtitle">{subtitulo}</div>
    </div>
    """, unsafe_allow_html=True)


def render_articulos_fuente(articulos_por_portal: dict[str, list[dict]]):
    total = sum(len(v) for v in articulos_por_portal.values())
    with st.expander(f"▸  Corpus fuente — {total} artículo(s) recuperado(s)"):
        for portal, arts in articulos_por_portal.items():
            st.markdown(f"**{portal}** — {len(arts)} artículo(s)")
            if not arts:
                st.markdown("*Sin resultados en esta ventana temporal.*")
            for a in arts:
                st.markdown(
                    f"<div class='articulo-item'>"
                    f"<span class='articulo-fecha'>{a['fecha']}</span> · "
                    f"<a href='{a['link']}' target='_blank'>{a['titulo']}</a>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            st.markdown("---")


def render_prognosis(prognosis: dict):
    st.markdown("<div class='section-header'>Prognosis de Agenda — Horizonte 7 días</div>", unsafe_allow_html=True)

    prob     = int(prognosis.get("probabilidad_continuidad", 0))
    razon    = prognosis.get("razonamiento_continuidad", "")
    derivadas = prognosis.get("derivadas_narrativas", [])

    col_prob, col_escenarios = st.columns([1, 2])

    with col_prob:
        st.markdown(
            f'<div class="prognosis-box">'
            f'<div class="prob-label">Probabilidad de continuidad en agenda</div>'
            f'<div class="prob-number">{prob}%</div>'
            f'<div class="razonamiento">{razon}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.progress(prob / 100)

    with col_escenarios:
        tag_clases = {"Principal": "tag-principal", "Secundario": "tag-secundario", "Marginal": "tag-marginal"}
        filas = ""
        for esc in derivadas:
            nombre     = esc.get("escenario", "")
            desc       = esc.get("descripcion", "")
            p_esc      = esc.get("probabilidad", 0)
            tag_cls    = tag_clases.get(nombre, "tag-marginal")
            color_prob = 'accent' if nombre == 'Principal' else 'accent-2' if nombre == 'Secundario' else 'muted'
            filas += (
                f'<div class="escenario-row">'
                f'<span class="escenario-tag {tag_cls}">{nombre}</span>'
                f'<span class="escenario-texto">{desc}</span>'
                f'<span class="escenario-prob" style="color:var(--{color_prob})">{p_esc}%</span>'
                f'</div>'
            )
        st.markdown(
            f'<div class="prognosis-box">'
            f'<div class="field-label" style="margin-bottom:0.6rem">Derivadas narrativas proyectadas</div>'
            f'{filas}</div>',
            unsafe_allow_html=True,
        )


def render_panel_busqueda(disabled: bool = False) -> tuple[str, bool]:
    col_k, col_b = st.columns([4, 1])
    with col_k:
        keyword = st.text_input(
            "Término de análisis",
            placeholder="ej. Milei, litio, Gaza, aranceles...",
            label_visibility="collapsed",
            disabled=disabled,
        )
    with col_b:
        ejecutar = st.button("◈  Analizar", use_container_width=True, disabled=disabled)
    return keyword.strip(), ejecutar


def render_freno_costos():
    st.warning(
        "Has alcanzado el límite de consultas del radar abierto. "
        "Para acceso ilimitado, contactá a la Dirección a través de la pestaña **◈ Acerca del Director**."
    )


# ─────────────────────────────────────────────────────────────────────────────
# CTA SITREP
# ─────────────────────────────────────────────────────────────────────────────

def enviar_email_sitrep(nombre: str, email_remitente: str, organizacion: str, mensaje: str) -> bool:
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    try:
        gmail_user     = st.secrets["GMAIL_USER"]
        gmail_app_pass = st.secrets["GMAIL_APP_PASS"]
    except KeyError:
        st.error("Faltan GMAIL_USER y GMAIL_APP_PASS en secrets.toml.")
        return False

    cuerpo = f"""
NUEVA SOLICITUD DE REPORTE SITREP — Vértice Austral
════════════════════════════════════════════════════
Nombre:        {nombre}
Email:         {email_remitente}
Organización:  {organizacion}

Mensaje:
{mensaje}

Enviado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
    msg = MIMEMultipart()
    msg["From"]     = gmail_user
    msg["To"]       = "joaquindestefano@gmail.com"
    msg["Subject"]  = f"[SITREP] Solicitud de {nombre} — Vértice Austral"
    msg["Reply-To"] = email_remitente
    msg.attach(MIMEText(cuerpo, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(gmail_user, gmail_app_pass)
            srv.sendmail(gmail_user, "joaquindestefano@gmail.com", msg.as_string())
        return True
    except smtplib.SMTPAuthenticationError:
        st.error("Error de autenticación Gmail. Verificá GMAIL_USER y GMAIL_APP_PASS.")
        return False
    except Exception as e:
        st.error(f"Error al enviar: {e}")
        return False


def render_cta_sitrep():
    st.markdown("""
    <div class="cta-sitrep">
      <div class="cta-texto-principal">¿Necesitás un análisis más profundo?</div>
      <div class="cta-texto-secundario">
        Los reportes SITREP incluyen fuentes primarias, inteligencia de redes sociales
        y recomendaciones estratégicas personalizadas. Completá el formulario y la
        Dirección te contacta en 24–48 hs.
      </div>
    </div>
    """, unsafe_allow_html=True)

    if st.session_state.get("sitrep_enviado"):
        st.success("✓ Solicitud recibida. La Dirección se pondrá en contacto a la brevedad.")
        return

    with st.form("form_sitrep", clear_on_submit=True):
        col_a, col_b = st.columns(2)
        with col_a:
            nombre = st.text_input("Nombre y apellido *")
            email  = st.text_input("Email de contacto *")
        with col_b:
            org  = st.text_input("Organización / Empresa")
            tema = st.text_input("Tema o región de interés")
        mensaje = st.text_area("Descripción breve del análisis requerido *", height=100)
        enviado = st.form_submit_button("✉  Enviar Solicitud SITREP", use_container_width=True)

        if enviado:
            if not nombre or not email or not mensaje:
                st.warning("Completá nombre, email y descripción.")
            else:
                msg_completo = f"{mensaje}\nTema: {tema}" if tema else mensaje
                if enviar_email_sitrep(nombre, email, org, msg_completo):
                    st.session_state.sitrep_enviado = True
                    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# VISTA 1: RADAR ARGENTINA
# ─────────────────────────────────────────────────────────────────────────────

def render_detector_sesgo_arg(sesgo: dict):
    st.markdown("<div class='section-header'>I. Detector de Sesgo Mediático</div>", unsafe_allow_html=True)
    portales = ["Clarín", "La Nación", "Infobae"]
    cols = st.columns(3)

    for col, portal in zip(cols, portales):
        datos = sesgo.get(portal, {})
        with col:
            st.markdown(
                f'<div class="portal-card">'
                f'<div class="portal-name">{portal}</div>'
                f'<div class="field-label">Omisiones detectadas</div>'
                f'<div class="field-value">{datos.get("omisiones", "—")}</div>'
                f'<div class="field-label">Carga adjetival</div>'
                f'<div class="field-value">{datos.get("carga_adjetival", "—")}</div>'
                f'<div class="field-label">Veredicto de encuadre</div>'
                f'<div class="veredicto-chip">{datos.get("veredicto_framing", "—")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )


def render_vista_radar():
    render_masthead("Radar Argentina · Clarín · La Nación · Infobae")

    # COOKIE: leer contador persistente del navegador
    restantes = LIMITE_CONSULTAS - leer_consultas()
    keyword, ejecutar = render_panel_busqueda(disabled=(restantes <= 0))

    portales_str = " · ".join(RSS_FEEDS_ARG.keys())
    st.markdown(
        f"<div class='sys-timestamp'>Ventana: últimas {HORAS_VENTANA}h · Fuentes: {portales_str} · Motor: {LLM_MODEL}</div>",
        unsafe_allow_html=True,
    )

    if restantes <= 0:
        render_freno_costos()
        return

    if not ejecutar:
        st.markdown(
            "<div style='text-align:center;margin-top:3rem;color:#7a7060;font-size:0.9rem;font-style:italic;'>"
            "Ingresá un término y presioná <strong>◈ Analizar</strong> para activar el motor.</div>",
            unsafe_allow_html=True,
        )
        return

    if not keyword:
        st.warning("⚠ Ingresá un término de búsqueda.")
        return

    with st.spinner(f"Extrayendo corpus RSS argentino para «{keyword}»..."):
        arts = extraer_articulos(keyword, RSS_FEEDS_ARG)

    total = sum(len(v) for v in arts.values())
    if total == 0:
        st.warning(f"Sin artículos sobre «{keyword}» en las últimas {HORAS_VENTANA}h. Probá otro término.")
        return

    render_articulos_fuente(arts)
    st.markdown("---")

    corpus = armar_corpus(arts)
    with st.spinner("Analizando sesgo y calculando prognosis..."):
        try:
            resultado = llamar_llm(keyword, corpus, modo="arg")
        except EnvironmentError as e:
            st.error(f"🔑 {e}")
            return
        except ValueError as e:
            st.error(f"⚠ {e}")
            return
        except Exception as e:
            st.error(f"Error inesperado: {e}")
            return

    # COOKIE: incrementar contador persistente
    incrementar_consultas()

    render_detector_sesgo_arg(resultado.get("sesgo", {}))
    st.markdown("<br>", unsafe_allow_html=True)
    render_prognosis(resultado.get("prognosis", {}))

    st.markdown(
        f"<div class='sys-timestamp' style='margin-top:2rem;border-top:1px solid #c8b89a;padding-top:0.5rem;'>"
        f"Análisis: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · «{keyword}» · {total} art.</div>",
        unsafe_allow_html=True,
    )
    render_cta_sitrep()


# ─────────────────────────────────────────────────────────────────────────────
# VISTA 2: RADAR INTERNACIONAL
# ─────────────────────────────────────────────────────────────────────────────

def render_detector_sesgo_intl(sesgo: dict, convergencias: str, angulo_ausente: str):
    st.markdown("<div class='section-header-intl'>I. Análisis de Encuadre por Medio</div>", unsafe_allow_html=True)

    portales = ["New York Times", "The Guardian", "El País"]
    cols = st.columns(3)

    for col, portal in zip(cols, portales):
        datos = sesgo.get(portal, {})
        with col:
            st.markdown(
                f'<div class="portal-card-intl">'
                f'<div class="portal-name-intl">{portal}</div>'
                f'<div class="field-label">Omisiones detectadas</div>'
                f'<div class="field-value">{datos.get("omisiones", "—")}</div>'
                f'<div class="field-label">Encuadre editorial</div>'
                f'<div class="field-value">{datos.get("encuadre_editorial", "—")}</div>'
                f'<div class="field-label">Posicionamiento geopolítico</div>'
                f'<div class="veredicto-chip-intl">{datos.get("posicionamiento_geopolitico", "—")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("<div class='section-header-intl'>II. Consenso Occidental y Ángulo Ausente</div>", unsafe_allow_html=True)

    col_c, col_a = st.columns(2)
    with col_c:
        st.markdown(
            f'<div class="convergencia-box">'
            f'<div class="field-label" style="color:#1a5c3a;margin-bottom:0.4rem">Narrativa convergente</div>'
            f'<div style="font-size:0.92rem;color:#1a1a2e;line-height:1.6;">{convergencias}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with col_a:
        st.markdown(
            f'<div class="angulo-box">'
            f'<div class="field-label" style="color:#8b1a1a;margin-bottom:0.4rem">Perspectiva ausente</div>'
            f'<div style="font-size:0.92rem;color:#1a1a2e;line-height:1.6;">{angulo_ausente}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )


def render_vista_internacional():
    render_masthead("Radar Internacional · NYT · The Guardian · El País")

    # COOKIE: leer contador persistente del navegador
    restantes = LIMITE_CONSULTAS - leer_consultas()
    keyword, ejecutar = render_panel_busqueda(disabled=(restantes <= 0))

    portales_str = " · ".join(RSS_FEEDS_INTL.keys())
    st.markdown(
        f"<div class='sys-timestamp'>Ventana: últimas {HORAS_VENTANA}h · Fuentes: {portales_str} · Motor: {LLM_MODEL}</div>",
        unsafe_allow_html=True,
    )

    if restantes <= 0:
        render_freno_costos()
        return

    if not ejecutar:
        st.markdown(
            "<div style='text-align:center;margin-top:3rem;color:#7a7060;font-size:0.9rem;font-style:italic;'>"
            "Ingresá un término en inglés o español y presioná <strong>◈ Analizar</strong>.<br>"
            "El motor buscará en NYT, The Guardian y El País simultáneamente.</div>",
            unsafe_allow_html=True,
        )
        return

    if not keyword:
        st.warning("⚠ Ingresá un término de búsqueda.")
        return

    with st.spinner(f"Extrayendo corpus internacional para «{keyword}»..."):
        arts = extraer_articulos(keyword, RSS_FEEDS_INTL)

    total = sum(len(v) for v in arts.values())
    if total == 0:
        st.warning(
            f"Sin artículos sobre «{keyword}» en las últimas {HORAS_VENTANA}h en fuentes internacionales. "
            f"Probá el término en inglés (ej. 'tariffs', 'Gaza', 'Argentina')."
        )
        return

    render_articulos_fuente(arts)
    st.markdown("---")

    corpus = armar_corpus(arts)
    with st.spinner("Analizando encuadre geopolítico internacional..."):
        try:
            resultado = llamar_llm(keyword, corpus, modo="intl")
        except EnvironmentError as e:
            st.error(f"🔑 {e}")
            return
        except ValueError as e:
            st.error(f"⚠ {e}")
            return
        except Exception as e:
            st.error(f"Error inesperado: {e}")
            return

    # COOKIE: incrementar contador persistente
    incrementar_consultas()

    sesgo         = resultado.get("sesgo", {})
    convergencias = resultado.get("convergencias", "—")
    angulo        = resultado.get("angulo_ausente", "—")

    render_detector_sesgo_intl(sesgo, convergencias, angulo)
    st.markdown("<br>", unsafe_allow_html=True)
    render_prognosis(resultado.get("prognosis", {}))

    st.markdown(
        f"<div class='sys-timestamp' style='margin-top:2rem;border-top:1px solid #c8b89a;padding-top:0.5rem;'>"
        f"Análisis: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · «{keyword}» · {total} art.</div>",
        unsafe_allow_html=True,
    )
    render_cta_sitrep()


# ─────────────────────────────────────────────────────────────────────────────
# VISTA 3: ACERCA DEL DIRECTOR
# ─────────────────────────────────────────────────────────────────────────────

def render_vista_director():
    render_masthead()
    st.markdown("""
    <div class="director-card">
      <div class="director-nombre">Joaquín Destéfano</div>
      <div class="director-cargo">Director · Vértice Austral Observatorio Geopolítico</div>
      <div class="director-bio">Joaquín Destéfano es Abogado por la Universidad de Buenos Aires y Magíster en Geopolítica y Estrategia por la Escuela Superior de Guerra. Como especialista en Relaciones Gubernamentales y Asuntos Públicos, su trabajo se centra en construir puentes operativos entre las organizaciones y los tomadores de decisión.</div>
      <div class="director-bio">Actualmente se desempeña como Asesor en la Embajada de la India en Buenos Aires, donde gestiona de manera directa la relación institucional con ministerios clave y coordina agendas de alto nivel para delegaciones comerciales y diplomáticas.</div>
      <div class="director-bio">Su trayectoria combina la comprensión interna de la maquinaria estatal —habiendo participado en la redacción de normativa en la administración pública municipal— con la agilidad del sector privado corporativo, donde ha diseñado e implementado estrategias de cumplimiento normativo (Compliance) ante entidades reguladoras.</div>
      <div class="director-bio">Esta doble experiencia le permite decodificar las necesidades del negocio y los mecanismos del Estado, facilitando la expansión institucional en el complejo entorno legal y político de la región.</div>
      <div class="director-contacto-header">Contacto Institucional y Solicitudes</div>
      <div class="director-contacto">
        <p>
          ✉&nbsp; <a href="mailto:joaquindestefano@gmail.com">joaquindestefano@gmail.com</a><br>
          ⟶&nbsp; <a href="https://www.linkedin.com/in/jrdestefano/" target="_blank">linkedin.com/in/jrdestefano</a>
        </p>
      </div>
    </div>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO DE COOKIES — Rate limit persistente entre recargas
# ─────────────────────────────────────────────────────────────────────────────

COOKIE_NAME      = "va_consultas"   # nombre de la cookie en el navegador
COOKIE_DIAS      = 7                # días hasta expiración


def get_controller() -> CookieController:
    """
    Instancia única del controlador de cookies por sesión.
    Almacenarla en session_state evita recrearla en cada rerun.
    """
    if "_cookie_ctrl" not in st.session_state:
        st.session_state._cookie_ctrl = CookieController()
    return st.session_state._cookie_ctrl


def leer_consultas() -> int:
    """
    Lee el contador de consultas desde la cookie del navegador.
    Si la cookie no existe o tiene un valor inválido, retorna 0.
    """
    ctrl = get_controller()
    try:
        valor = ctrl.get(COOKIE_NAME)
        return int(valor) if valor is not None else 0
    except (TypeError, ValueError):
        return 0


def incrementar_consultas() -> int:
    """
    Incrementa el contador en la cookie y retorna el nuevo valor.
    La cookie se setea con max_age en segundos (7 días por defecto).
    """
    ctrl  = get_controller()
    nuevo = leer_consultas() + 1
    ctrl.set(COOKIE_NAME, str(nuevo), max_age=COOKIE_DIAS * 86400)
    return nuevo


# ─────────────────────────────────────────────────────────────────────────────
# PUNTO DE ENTRADA
# ─────────────────────────────────────────────────────────────────────────────

def main():
    configurar_pagina()
    vista = render_sidebar()

    if vista == "internacional":
        render_vista_internacional()
    elif vista == "director":
        render_vista_director()
    else:
        render_vista_radar()


if __name__ == "__main__":
    main()
