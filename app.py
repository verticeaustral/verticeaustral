"""
╔══════════════════════════════════════════════════════════════════╗
║              VÉRTICE AUSTRAL — Motor Geopolítico v2.0            ║
║         Observatorio de Sesgo Mediático y Prognosis de Agenda    ║
╠══════════════════════════════════════════════════════════════════╣
║  CAMBIOS v2.0:                                                   ║
║  · Navegación lateral (sidebar) con dos vistas                   ║
║  · Rate limit: 2 consultas por sesión                            ║
║  · CTA "Solicitar SITREP" post-resultados                        ║
║  · Vista "Acerca del Director"                                   ║
║  · API Key migrada a st.secrets (nunca más en el código)         ║
╚══════════════════════════════════════════════════════════════════╝

Setup de st.secrets (local):
  1. Crear carpeta .streamlit/ en la raíz del proyecto
  2. Crear archivo .streamlit/secrets.toml con:
       ANTHROPIC_API_KEY = "sk-ant-..."
  3. Agregar .streamlit/secrets.toml al .gitignore

Servidor: streamlit run app.py
"""

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
import feedparser
import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN GLOBAL  (sin cambios respecto a v1.0)
# ─────────────────────────────────────────────────────────────────────────────

RSS_FEEDS = {
    "Clarín": "https://www.clarin.com/rss/lo-ultimo/",
    "La Nación": "https://www.lanacion.com.ar/arc/outboundfeeds/rss/",
    "Página/12": "https://www.pagina12.com.ar/rss/portada",
}

LLM_MODEL = "claude-sonnet-4-5"
HORAS_VENTANA = 24

# ── NUEVO v2.0: límite de consultas gratuitas por sesión ──────────────────────
LIMITE_CONSULTAS = 2

# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 1: EXTRACCIÓN RSS  (sin cambios respecto a v1.0)
# ─────────────────────────────────────────────────────────────────────────────

def parsear_fecha(entry) -> Optional[datetime]:
    for campo in ("published_parsed", "updated_parsed"):
        t = getattr(entry, campo, None)
        if t:
            import time
            ts = time.mktime(t)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    return None


def extraer_articulos(keyword: str) -> dict[str, list[dict]]:
    ahora = datetime.now(tz=timezone.utc)
    corte = ahora - timedelta(hours=HORAS_VENTANA)
    resultados = {}

    for portal, url in RSS_FEEDS.items():
        feed = feedparser.parse(url)
        articulos_portal = []

        for entry in feed.entries:
            fecha = parsear_fecha(entry)
            if fecha and fecha < corte:
                continue

            titulo = getattr(entry, "title", "")
            resumen = getattr(entry, "summary", "")
            link = getattr(entry, "link", "")
            texto_completo = f"{titulo} {resumen}".lower()

            if keyword.lower() in texto_completo:
                articulos_portal.append({
                    "titulo": titulo,
                    "resumen": resumen,
                    "link": link,
                    "fecha": fecha.strftime("%Y-%m-%d %H:%M UTC") if fecha else "s/d",
                    "portal": portal,
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
# MÓDULO 2: ANÁLISIS LLM  (sin cambios en prompts; solo cambia la lectura de key)
# ─────────────────────────────────────────────────────────────────────────────

PROMPT_SISTEMA = """Sos un analista de medios y geopolítica especializado en el ecosistema mediático
latinoamericano. Tu metodología combina el análisis del framing (Entman, 1993) con la
decodificación de la agenda setting. Respondés únicamente en español rioplatense formal."""

PROMPT_ANALISIS = """Analizá el siguiente corpus de noticias argentinas sobre el tema "{keyword}",
publicadas en las últimas 24 horas. El corpus está segmentado por portal.

CORPUS:
{corpus}

Realizá dos análisis y devolvé ÚNICAMENTE un JSON válido con la siguiente estructura exacta
(sin texto antes ni después, sin bloques de código markdown):

{{
  "sesgo": {{
    "Clarín": {{
      "omisiones": "Descripción de qué hechos o voces relevantes omite o minimiza este portal.",
      "carga_adjetival": "Análisis de los adjetivos, verbos y encuadres emocionales predominantes.",
      "veredicto_framing": "Una oración que sintetice el posicionamiento editorial de este portal sobre el tema."
    }},
    "La Nación": {{
      "omisiones": "...",
      "carga_adjetival": "...",
      "veredicto_framing": "..."
    }},
    "Página/12": {{
      "omisiones": "...",
      "carga_adjetival": "...",
      "veredicto_framing": "..."
    }}
  }},
  "prognosis": {{
    "probabilidad_continuidad": <número entero 0-100>,
    "razonamiento_continuidad": "Breve justificación de por qué el tema tiene esa probabilidad de mantenerse en agenda.",
    "derivadas_narrativas": [
      {{
        "escenario": "Principal",
        "descripcion": "El escenario mediático más probable en los próximos 7 días.",
        "probabilidad": <número entero 0-100>
      }},
      {{
        "escenario": "Secundario",
        "descripcion": "Un escenario alternativo de menor probabilidad.",
        "probabilidad": <número entero 0-100>
      }},
      {{
        "escenario": "Marginal",
        "descripcion": "Un escenario de baja probabilidad pero alto impacto potencial.",
        "probabilidad": <número entero 0-100>
      }}
    ]
  }}
}}"""


def llamar_llm(keyword: str, corpus: str) -> dict:
    """
    CAMBIO v2.0: La API key se lee desde st.secrets en lugar de os.environ.
    Esto permite deployar en Streamlit Cloud sin exponer credenciales.
    Localmente, la key va en .streamlit/secrets.toml (ver instrucciones arriba).
    """
    try:
        api_key = st.secrets["ANTHROPIC_API_KEY"]
    except (KeyError, FileNotFoundError):
        raise EnvironmentError(
            "API Key no encontrada en st.secrets. "
            "Creá el archivo .streamlit/secrets.toml con: ANTHROPIC_API_KEY = 'sk-ant-...'"
        )

    client = anthropic.Anthropic(api_key=api_key)
    prompt_usuario = PROMPT_ANALISIS.format(keyword=keyword, corpus=corpus)

    message = client.messages.create(
        model=LLM_MODEL,
        max_tokens=4096,
        system=PROMPT_SISTEMA,
        messages=[{"role": "user", "content": prompt_usuario}],
    )

    respuesta_raw = message.content[0].text.strip()
    respuesta_limpia = re.sub(r"^```(?:json)?\s*", "", respuesta_raw)
    respuesta_limpia = re.sub(r"\s*```$", "", respuesta_limpia).strip()

    try:
        return json.loads(respuesta_limpia)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"El LLM no devolvió JSON válido. Error: {e}\n\nRespuesta raw:\n{respuesta_raw}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# MÓDULO 3: INTERFAZ STREAMLIT
# ─────────────────────────────────────────────────────────────────────────────

def configurar_pagina():
    st.set_page_config(
        page_title="Vértice Austral",
        page_icon="◈",
        layout="wide",
        initial_sidebar_state="expanded",  # CAMBIO v2.0: sidebar visible por defecto
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
        --rule:      #c8b89a;
        --muted:     #7a7060;
        --tag-bg:    #e8e0d0;
        --mono:      'JetBrains Mono', monospace;
      }

      .stApp { background-color: var(--paper); }
      html, body, .stApp { font-family: 'Source Serif 4', serif; color: var(--ink); }

      /* ── Sidebar ── */
      [data-testid="stSidebar"] {
        background-color: var(--ink) !important;
        border-right: 1px solid #2d2d4e;
      }
      [data-testid="stSidebar"] * { color: var(--paper) !important; }
      [data-testid="stSidebar"] .sidebar-title {
        font-family: 'Playfair Display', serif;
        font-size: 1.1rem;
        letter-spacing: 0.08em;
        border-bottom: 1px solid #3d3d5c;
        padding-bottom: 0.8rem;
        margin-bottom: 1rem;
      }
      [data-testid="stSidebar"] hr { border-color: #3d3d5c !important; }

      /* ── Masthead ── */
      .va-masthead {
        border-top: 3px solid var(--ink);
        border-bottom: 1px solid var(--rule);
        padding: 1.2rem 0 1rem;
        margin-bottom: 1.5rem;
        text-align: center;
      }
      .va-title {
        font-family: 'Playfair Display', serif;
        font-size: 2.6rem;
        font-weight: 700;
        letter-spacing: 0.04em;
        color: var(--ink);
        line-height: 1.1;
      }
      .va-subtitle {
        font-size: 0.78rem;
        letter-spacing: 0.2em;
        text-transform: uppercase;
        color: var(--muted);
        margin-top: 0.3rem;
      }
      .va-rule { border: none; border-top: 1px solid var(--rule); margin: 0.8rem auto; width: 60%; }

      /* ── Sección header ── */
      .section-header {
        font-family: 'Playfair Display', serif;
        font-size: 1.1rem;
        font-weight: 600;
        color: var(--accent);
        letter-spacing: 0.08em;
        text-transform: uppercase;
        border-bottom: 2px solid var(--accent);
        padding-bottom: 0.3rem;
        margin-bottom: 1rem;
      }

      /* ── Tarjeta de portal ── */
      .portal-card {
        background: #fff;
        border: 1px solid var(--rule);
        border-top: 3px solid var(--accent-2);
        border-radius: 2px;
        padding: 1.2rem 1.4rem;
        margin-bottom: 1rem;
        box-shadow: 2px 2px 8px rgba(26,26,46,0.06);
      }
      .portal-name {
        font-family: 'Playfair Display', serif;
        font-size: 1.05rem;
        font-weight: 700;
        color: var(--accent-2);
        margin-bottom: 0.8rem;
        border-bottom: 1px solid var(--rule);
        padding-bottom: 0.4rem;
      }
      .field-label {
        font-size: 0.65rem;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: var(--muted);
        font-family: var(--mono);
        margin-bottom: 0.2rem;
      }
      .field-value { font-size: 0.92rem; color: var(--ink-light); line-height: 1.55; margin-bottom: 0.9rem; }
      .veredicto-chip {
        display: inline-block;
        background: var(--tag-bg);
        border: 1px solid var(--rule);
        border-left: 3px solid var(--accent);
        padding: 0.4rem 0.8rem;
        font-size: 0.88rem;
        font-style: italic;
        color: var(--ink);
        border-radius: 0 2px 2px 0;
        line-height: 1.4;
      }

      /* ── Prognosis ── */
      .prognosis-box {
        background: #fff;
        border: 1px solid var(--rule);
        border-top: 3px solid var(--ink);
        padding: 1.4rem 1.6rem;
        border-radius: 2px;
        box-shadow: 2px 2px 8px rgba(26,26,46,0.06);
      }
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

      /* ── NUEVO v2.0: Rate limit badge ── */
      .rate-badge {
        display: inline-block;
        font-family: var(--mono);
        font-size: 0.75rem;
        letter-spacing: 0.1em;
        background: var(--tag-bg);
        border: 1px solid var(--rule);
        padding: 0.25rem 0.6rem;
        border-radius: 2px;
        color: var(--muted);
      }
      .rate-badge.agotado { background: #fbf0f0; border-color: var(--accent); color: var(--accent); }

      /* ── NUEVO v2.0: CTA SITREP ── */
      .cta-sitrep {
        margin-top: 2.5rem;
        border: 1px solid var(--ink);
        border-left: 4px solid var(--accent);
        background: #fff;
        padding: 1.2rem 1.6rem;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 1rem;
      }
      .cta-texto-principal {
        font-family: 'Playfair Display', serif;
        font-size: 1rem;
        font-weight: 600;
        color: var(--ink);
      }
      .cta-texto-secundario {
        font-size: 0.82rem;
        color: var(--muted);
        margin-top: 0.2rem;
        font-style: italic;
      }
      .cta-boton {
        display: inline-block;
        background: var(--accent);
        color: #fff !important;
        text-decoration: none !important;
        font-family: 'Playfair Display', serif;
        font-size: 0.85rem;
        letter-spacing: 0.06em;
        padding: 0.6rem 1.4rem;
        border-radius: 2px;
        white-space: nowrap;
        flex-shrink: 0;
      }
      .cta-boton:hover { background: var(--ink) !important; }

      /* ── NUEVO v2.0: Vista Director ── */
      .director-card {
        max-width: 720px;
        margin: 2rem auto;
        background: #fff;
        border: 1px solid var(--rule);
        border-top: 4px solid var(--ink);
        padding: 2.5rem 3rem;
        box-shadow: 4px 4px 16px rgba(26,26,46,0.08);
      }
      .director-nombre {
        font-family: 'Playfair Display', serif;
        font-size: 1.8rem;
        font-weight: 700;
        color: var(--ink);
        margin-bottom: 0.2rem;
      }
      .director-cargo {
        font-size: 0.72rem;
        letter-spacing: 0.2em;
        text-transform: uppercase;
        color: var(--accent);
        font-family: var(--mono);
        margin-bottom: 1.5rem;
        padding-bottom: 1rem;
        border-bottom: 1px solid var(--rule);
      }
      .director-bio {
        font-size: 0.97rem;
        line-height: 1.75;
        color: var(--ink-light);
        margin-bottom: 1.2rem;
      }
      .director-contacto-header {
        font-size: 0.65rem;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: var(--muted);
        font-family: var(--mono);
        margin-bottom: 0.6rem;
        border-top: 1px solid var(--rule);
        padding-top: 1rem;
      }
      .director-contacto a { color: var(--accent-2) !important; text-decoration: none; }
      .director-contacto a:hover { text-decoration: underline; }

      /* ── Inputs y botones nativos Streamlit ── */
      .stTextInput > div > div > input {
        background-color: #fff !important;
        border: 1px solid var(--rule) !important;
        border-radius: 2px !important;
        color: var(--ink) !important;
        font-family: 'Source Serif 4', serif !important;
      }
      .stButton > button {
        background-color: var(--ink) !important;
        color: var(--paper) !important;
        border: none !important;
        border-radius: 2px !important;
        font-family: 'Playfair Display', serif !important;
        font-size: 0.9rem !important;
        letter-spacing: 0.08em !important;
        padding: 0.5rem 2rem !important;
      }
      .stButton > button:hover { background-color: var(--accent) !important; }

      .sys-timestamp {
        font-family: var(--mono);
        font-size: 0.68rem;
        color: var(--muted);
        text-align: right;
        margin-top: 0.5rem;
      }

      #MainMenu, footer, .stDeployButton { display: none !important; }
    </style>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# NUEVO v2.0: MÓDULO DE NAVEGACIÓN LATERAL
# ─────────────────────────────────────────────────────────────────────────────

def render_sidebar() -> str:
    """
    Renderiza el sidebar de navegación.
    Retorna la vista activa: 'radar' o 'director'.
    """
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
            options=["📡  Radar OSINT", "◈  Acerca del Director"],
            label_visibility="collapsed",
        )

        st.markdown("---")

        # Contador de consultas en el sidebar
        consultas_usadas = st.session_state.get("consultas_realizadas", 0)
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
                "Límite del radar abierto alcanzado."
                "</div>",
                unsafe_allow_html=True,
            )

    return "director" if "Director" in vista else "radar"


# ─────────────────────────────────────────────────────────────────────────────
# COMPONENTES DE RENDER (sin cambios respecto a v1.0)
# ─────────────────────────────────────────────────────────────────────────────

def render_masthead():
    st.markdown("""
    <div class="va-masthead">
      <div class="va-subtitle">Observatorio Geopolítico Austral · Análisis Mediático Comparativo</div>
      <div class="va-title">◈ VÉRTICE AUSTRAL</div>
      <hr class="va-rule">
      <div class="va-subtitle">Motor de Sesgo · Prognosis de Agenda · Síntesis LLM</div>
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


def render_detector_sesgo(sesgo: dict):
    st.markdown("<div class='section-header'>I. Detector de Sesgo Mediático</div>", unsafe_allow_html=True)
    cols = st.columns(3)
    portales = ["Clarín", "La Nación", "Página/12"]

    for col, portal in zip(cols, portales):
        datos = sesgo.get(portal, {})
        with col:
            st.markdown(f"""
            <div class="portal-card">
              <div class="portal-name">{portal}</div>
              <div class="field-label">Omisiones detectadas</div>
              <div class="field-value">{datos.get('omisiones', '—')}</div>
              <div class="field-label">Carga adjetival</div>
              <div class="field-value">{datos.get('carga_adjetival', '—')}</div>
              <div class="field-label">Veredicto de encuadre</div>
              <div class="veredicto-chip">{datos.get('veredicto_framing', '—')}</div>
            </div>
            """, unsafe_allow_html=True)


def render_prognosis(prognosis: dict):
    st.markdown("<div class='section-header'>II. Prognosis de Agenda — Horizonte 7 días</div>", unsafe_allow_html=True)

    prob = int(prognosis.get("probabilidad_continuidad", 0))
    razon = prognosis.get("razonamiento_continuidad", "")
    derivadas = prognosis.get("derivadas_narrativas", [])

    col_prob, col_escenarios = st.columns([1, 2])

    with col_prob:
        st.markdown(f"""
        <div class="prognosis-box">
          <div class="prob-label">Probabilidad de continuidad en tapa</div>
          <div class="prob-number">{prob}%</div>
          <div class="razonamiento">{razon}</div>
        </div>
        """, unsafe_allow_html=True)
        st.progress(prob / 100)

    with col_escenarios:
        # FIX: construir el HTML completo en una sola string y pasarlo a
        # st.markdown de una vez. La concatenación iterativa causaba que
        # Streamlit escapara el HTML en lugar de renderizarlo.
        tag_clases = {"Principal": "tag-principal", "Secundario": "tag-secundario", "Marginal": "tag-marginal"}

        filas_html = ""
        for esc in derivadas:
            nombre     = esc.get("escenario", "")
            desc       = esc.get("descripcion", "")
            p_esc      = esc.get("probabilidad", 0)
            tag_cls    = tag_clases.get(nombre, "tag-marginal")
            color_prob = 'accent' if nombre == 'Principal' else 'accent-2' if nombre == 'Secundario' else 'muted'
            filas_html += (
                f'<div class="escenario-row">'
                f'<span class="escenario-tag {tag_cls}">{nombre}</span>'
                f'<span class="escenario-texto">{desc}</span>'
                f'<span class="escenario-prob" style="color:var(--{color_prob})">{p_esc}%</span>'
                f'</div>'
            )

        st.markdown(
            f'<div class="prognosis-box">'
            f'<div class="field-label" style="margin-bottom:0.6rem">Derivadas narrativas proyectadas</div>'
            f'{filas_html}'
            f'</div>',
            unsafe_allow_html=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# NUEVO v2.0: CTA SITREP
# ─────────────────────────────────────────────────────────────────────────────

def enviar_email_sitrep(nombre: str, email_remitente: str, organizacion: str, mensaje: str) -> bool:
    """
    Envía el formulario SITREP a joaquindestefano@gmail.com usando smtplib.
    Requiere en .streamlit/secrets.toml:
        GMAIL_USER     = "tucuenta@gmail.com"       # cuenta que envía
        GMAIL_APP_PASS = "xxxx xxxx xxxx xxxx"      # App Password de Google
    Cómo generar App Password:
        myaccount.google.com → Seguridad → Verificación en 2 pasos → Contraseñas de aplicación
    """
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    try:
        gmail_user     = st.secrets["GMAIL_USER"]
        gmail_app_pass = st.secrets["GMAIL_APP_PASS"]
    except KeyError:
        st.error(
            "Faltan credenciales de email en secrets.toml. "
            "Agregá GMAIL_USER y GMAIL_APP_PASS."
        )
        return False

    destinatario = "joaquindestefano@gmail.com"

    cuerpo = f"""
NUEVA SOLICITUD DE REPORTE SITREP — Vértice Austral
════════════════════════════════════════════════════

Nombre:        {nombre}
Email:         {email_remitente}
Organización:  {organizacion}

Mensaje:
{mensaje}

────────────────────────────────────────────────────
Enviado desde el formulario de Vértice Austral
{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""

    msg = MIMEMultipart()
    msg["From"]    = gmail_user
    msg["To"]      = destinatario
    msg["Subject"] = f"[SITREP] Solicitud de {nombre} — Vértice Austral"
    msg["Reply-To"] = email_remitente
    msg.attach(MIMEText(cuerpo, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as servidor:
            servidor.login(gmail_user, gmail_app_pass)
            servidor.sendmail(gmail_user, destinatario, msg.as_string())
        return True
    except smtplib.SMTPAuthenticationError:
        st.error("Error de autenticación Gmail. Verificá GMAIL_USER y GMAIL_APP_PASS en secrets.toml.")
        return False
    except Exception as e:
        st.error(f"Error al enviar el email: {e}")
        return False


def render_cta_sitrep():
    """
    Formulario de contacto inline. Al enviarse, dispara enviar_email_sitrep()
    que manda el mensaje directamente a joaquindestefano@gmail.com.
    Usa st.session_state para evitar reenvíos al recargar la página.
    """
    st.markdown("""
    <div class="cta-sitrep" style="flex-direction:column; align-items:flex-start;">
      <div style="margin-bottom:1rem;">
        <div class="cta-texto-principal">¿Necesitás un análisis más profundo?</div>
        <div class="cta-texto-secundario">
          Los reportes SITREP incluyen análisis de fuentes primarias, inteligencia
          de redes sociales y recomendaciones estratégicas personalizadas.
          Completá el formulario y la Dirección te contacta en 24–48 hs.
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # Evitar que el formulario reaparezca si ya se envió en esta sesión
    if st.session_state.get("sitrep_enviado"):
        st.success("✓ Solicitud recibida. La Dirección se pondrá en contacto a la brevedad.")
        return

    with st.form("form_sitrep", clear_on_submit=True):
        col_a, col_b = st.columns(2)
        with col_a:
            nombre = st.text_input("Nombre y apellido *")
            email  = st.text_input("Email de contacto *")
        with col_b:
            org     = st.text_input("Organización / Empresa")
            tema    = st.text_input("Tema o región de interés")

        mensaje = st.text_area(
            "Descripción breve del análisis requerido *",
            placeholder="Ej: Necesito un SITREP sobre las negociaciones del FMI y su impacto en la agenda de medios...",
            height=100,
        )

        enviado = st.form_submit_button("✉  Enviar Solicitud SITREP", use_container_width=True)

        if enviado:
            if not nombre or not email or not mensaje:
                st.warning("Completá los campos obligatorios: nombre, email y descripción.")
            else:
                mensaje_completo = f"{mensaje}\n\nTema de interés: {tema}" if tema else mensaje
                exito = enviar_email_sitrep(nombre, email, org, mensaje_completo)
                if exito:
                    st.session_state.sitrep_enviado = True
                    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# NUEVO v2.0: VISTA "ACERCA DEL DIRECTOR"
# ─────────────────────────────────────────────────────────────────────────────

def render_vista_director():
    render_masthead()
    st.markdown("""
    <div class="director-card">

      <div class="director-nombre">Joaquín Destéfano</div>
      <div class="director-cargo">Director · Vértice Austral Observatorio Geopolítico</div>

      <div class="director-bio">
        Joaquín Destéfano es Abogado por la Universidad de Buenos Aires y Magíster en Geopolítica
        y Estrategia por la Escuela Superior de Guerra. Como especialista en Relaciones
        Gubernamentales y Asuntos Públicos, su trabajo se centra en construir puentes operativos
        entre las organizaciones y los tomadores de decisión.
      </div>

      <div class="director-bio">
        Actualmente se desempeña como Asesor en la Embajada de la India en Buenos Aires, donde
        gestiona de manera directa la relación institucional con ministerios clave y coordina
        agendas de alto nivel para delegaciones comerciales y diplomáticas.
      </div>

      <div class="director-bio">
        Su trayectoria combina la comprensión interna de la maquinaria estatal —habiendo
        participado en la redacción de normativa en la administración pública municipal— con la
        agilidad del sector privado corporativo, donde ha diseñado e implementado estrategias de
        cumplimiento normativo (Compliance) ante entidades reguladoras.
      </div>

      <div class="director-bio">
        Esta doble experiencia le permite decodificar las necesidades del negocio y los mecanismos
        del Estado, facilitando la expansión institucional en el complejo entorno legal y político
        de la región.
      </div>

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
# NUEVO v2.0: VISTA "RADAR OSINT" con rate limit integrado
# ─────────────────────────────────────────────────────────────────────────────

def render_vista_radar():
    render_masthead()

    # Inicializar contador en session_state
    # Persiste durante la sesión del navegador; se resetea al cerrar o recargar.
    if "consultas_realizadas" not in st.session_state:
        st.session_state.consultas_realizadas = 0

    consultas_usadas = st.session_state.consultas_realizadas
    restantes = LIMITE_CONSULTAS - consultas_usadas

    # ── Panel de búsqueda ──────────────────────────────────────────────────
    col_k, col_b = st.columns([4, 1])
    with col_k:
        keyword = st.text_input(
            "Término de análisis",
            placeholder="ej. Mercosur, Milei, litio, OTAN...",
            label_visibility="collapsed",
            disabled=(restantes <= 0),
        )
    with col_b:
        ejecutar = st.button(
            "◈  Analizar",
            use_container_width=True,
            disabled=(restantes <= 0),
        )

    st.markdown(
        f"<div class='sys-timestamp'>"
        f"Ventana temporal: últimas {HORAS_VENTANA}h · "
        f"Fuentes: {', '.join(RSS_FEEDS.keys())} · "
        f"Motor: {LLM_MODEL}"
        f"</div>",
        unsafe_allow_html=True,
    )

    # ── FRENO DE COSTOS ────────────────────────────────────────────────────
    if restantes <= 0:
        st.warning(
            "Has alcanzado el límite de consultas del radar abierto. "
            "Para acceso ilimitado y reportes en profundidad, contactá a la Dirección "
            "a través de la pestaña **◈ Acerca del Director**."
        )
        return

    # ── Estado inicial ─────────────────────────────────────────────────────
    if not ejecutar:
        st.markdown("""
        <div style="text-align:center; margin-top:3rem; color:#7a7060; font-size:0.9rem; font-style:italic;">
          Ingresá un término geopolítico y presioná <strong>◈ Analizar</strong> para activar el motor.<br>
          El sistema extraerá artículos de las últimas 24 horas y ejecutará el análisis de sesgo y prognosis.
        </div>
        """, unsafe_allow_html=True)
        return

    if not keyword.strip():
        st.warning("⚠ Ingresá un término de búsqueda para continuar.")
        return

    keyword = keyword.strip()

    # ── Paso 1: Extracción RSS ─────────────────────────────────────────────
    with st.spinner(f"Extrayendo corpus RSS para «{keyword}»..."):
        articulos_por_portal = extraer_articulos(keyword)

    total_arts = sum(len(v) for v in articulos_por_portal.values())

    if total_arts == 0:
        st.warning(
            f"No se encontraron artículos sobre «{keyword}» en las últimas {HORAS_VENTANA} horas "
            f"en ninguno de los portales. Probá con otro término o verificá que los feeds RSS estén activos."
        )
        # Sin artículos = sin llamada LLM = no se consume cuota
        return

    render_articulos_fuente(articulos_por_portal)
    st.markdown("---")

    # ── Paso 2: Análisis LLM ───────────────────────────────────────────────
    corpus = armar_corpus(articulos_por_portal)

    with st.spinner("Ejecutando análisis de sesgo y prognosis vía LLM... (puede tomar 15-30 seg)"):
        try:
            resultado = llamar_llm(keyword, corpus)
        except EnvironmentError as e:
            st.error(f"🔑 Error de configuración: {e}")
            return
        except ValueError as e:
            st.error(f"⚠ Error al parsear respuesta del LLM: {e}")
            return
        except Exception as e:
            st.error(f"Error inesperado al llamar a la API: {e}")
            return

    # Incrementar contador SOLO si el análisis fue exitoso
    st.session_state.consultas_realizadas += 1

    # ── Paso 3: Visualización ──────────────────────────────────────────────
    sesgo     = resultado.get("sesgo", {})
    prognosis = resultado.get("prognosis", {})

    render_detector_sesgo(sesgo)
    st.markdown("<br>", unsafe_allow_html=True)
    render_prognosis(prognosis)

    st.markdown(
        f"<div class='sys-timestamp' style='margin-top:2rem; border-top:1px solid #c8b89a; padding-top:0.5rem;'>"
        f"Análisis generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · "
        f"Término: «{keyword}» · {total_arts} artículo(s) procesado(s)"
        "</div>",
        unsafe_allow_html=True,
    )

    # CTA de conversión al final de cada análisis exitoso
    render_cta_sitrep()


# ─────────────────────────────────────────────────────────────────────────────
# PUNTO DE ENTRADA PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

def main():
    configurar_pagina()
    vista_activa = render_sidebar()

    if vista_activa == "director":
        render_vista_director()
    else:
        render_vista_radar()


if __name__ == "__main__":
    main()
