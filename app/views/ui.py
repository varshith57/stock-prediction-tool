"""Design system for the app: one calm, merchant-dashboard look (white workspace, quiet sidebar,
bold page titles, bordered cards, one red accent, status pills). Screens use these helpers
instead of raw Streamlit styling so they stay consistent."""

from __future__ import annotations

from collections.abc import Iterable
from html import escape

import streamlit as st

INK, MUTED, LINE, SOFT = "#191919", "#6B6B6B", "#E6E6E6", "#F6F6F6"
RED, GREEN, AMBER, BLUE = "#EB1700", "#00A86B", "#B26B00", "#3D7BF7"
TONES = {
    "red": (RED, "#FDECEA"),
    "green": (GREEN, "#E6F6EF"),
    "amber": (AMBER, "#FFF4E0"),
    "blue": (BLUE, "#EAF1FE"),
    "grey": (MUTED, SOFT),
}

CSS = f"""
<style>
.block-container {{ padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1180px; }}
header[data-testid="stHeader"] {{ background: transparent; }}
#MainMenu, footer {{ visibility: hidden; }}
h1, h2, h3 {{ letter-spacing: -0.01em; color: {INK}; }}
.sa-title, div.sa-head .sa-title {{ font-size: 1.9rem !important; font-weight: 700;
    color: {INK}; margin: 0; line-height: 1.2; }}
.sa-sub {{ color: {MUTED}; font-size: 0.92rem; margin-top: .25rem; }}
.sa-head {{ display:flex; justify-content:space-between; align-items:flex-end; gap: 1rem;
            margin-bottom: 1.4rem; flex-wrap: wrap; }}
.sa-section {{ font-size: 1.05rem; font-weight: 600; color: {INK}; margin: 1.6rem 0 .6rem; }}
.sa-kpis {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: .8rem;
            margin: .2rem 0 1rem; }}
.sa-kpi {{ border: 1px solid {LINE}; border-radius: .75rem; padding: .9rem 1rem;
           background: #fff; }}
.sa-kpi .l {{ color: {MUTED}; font-size: .8rem; font-weight: 500; }}
.sa-kpi .v {{ color: {INK}; font-size: 1.45rem; font-weight: 700; margin-top: .2rem;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.sa-kpi .d {{ font-size: .8rem; font-weight: 600; margin-top: .15rem; }}
.sa-pill {{ display:inline-block; padding: .14rem .55rem; border-radius: 999px; font-size: .75rem;
            font-weight: 600; line-height: 1.4; vertical-align: middle; }}
.sa-hero {{ border: 1px solid {LINE}; border-radius: .9rem; padding: 1.2rem 1.3rem;
            background: #fff; margin-bottom: 1rem; }}
.sa-hero .t {{ font-size: 1.35rem; font-weight: 700; color: {INK}; }}
.sa-hero .s {{ color: {MUTED}; margin-top: .3rem; font-size: .92rem; }}
.sa-row {{ display:flex; justify-content:space-between; align-items:center; gap: .8rem;
           flex-wrap: wrap; }}
.sa-sym {{ font-weight: 700; font-size: 1.02rem; color: {INK}; }}
.sa-muted {{ color: {MUTED}; font-size: .86rem; }}
.sa-brand {{ font-weight: 800; font-size: 1.15rem; color: {INK}; letter-spacing: -0.02em; }}
.sa-brand span {{ color: {RED}; }}
.sa-foot {{ color: {MUTED}; font-size: .78rem; line-height: 1.5; }}
div[data-testid="stSidebarNav"] a, [data-testid="stSidebarNavLink"] {{ border-radius: .55rem; }}
[data-testid="stMetricValue"] {{ font-weight: 700; }}
button[kind="primary"] {{ font-weight: 600; }}
.sa-moves {{ color: {MUTED}; font-size: .8rem; line-height: 1.35; margin: -.35rem 0 .9rem; }}
.st-key-sa_main {{ border-color: {INK}22 !important; }}
.st-key-sa_main label p {{ font-weight: 600; color: {INK}; }}
.sa-quiet-title {{ color: {MUTED}; font-size: .95rem; margin-top: 2.4rem; }}
.st-key-sa_quiet {{ background: {SOFT}; border-radius: .75rem; padding: .9rem 1rem; }}
.st-key-sa_quiet [data-testid="stExpander"] details {{ background: {SOFT}; border-color: {LINE}; }}
.st-key-sa_quiet [data-testid="stExpander"] summary p,
.st-key-sa_quiet label p {{ color: {MUTED}; font-weight: 500; }}
.st-key-sa_quiet input {{ color: {MUTED}; }}
.sa-line {{ color: {INK}; font-size: .88rem; margin-top: .3rem; }}
.sa-colhead {{ display:flex; align-items:center; gap:.5rem; font-size: 1.25rem; font-weight: 700;
               color: {INK}; margin-top: .6rem; }}
.sa-colhint {{ color: {MUTED}; font-size: .84rem; margin: .1rem 0 .7rem; }}
.sa-watchhead {{ color: {MUTED}; font-size: .78rem; font-weight: 600; text-transform: uppercase;
                 letter-spacing: .04em; margin: 1.1rem 0 .4rem; }}
[class*="st-key-watch_"] {{ background: {SOFT}; border-radius: .75rem; padding: .65rem .85rem;
                            opacity: .8; }}
[class*="st-key-watch_"] .sa-sym {{ color: {MUTED}; }}
.sa-holdrow {{ display:flex; justify-content:space-between; align-items:center; gap:.6rem;
               padding: .55rem 0; border-bottom: 1px solid {LINE}; }}
.sa-holdrow:last-child {{ border-bottom: 0; }}
</style>
"""


def inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def pill(text: str, tone: str = "grey") -> str:
    fg, bg = TONES[tone]
    return f'<span class="sa-pill" style="color:{fg};background:{bg}">{escape(text)}</span>'


def header(title: str, subtitle: str = "", right_html: str = "") -> None:
    st.markdown(
        f'<div class="sa-head"><div><div class="sa-title">{escape(title)}</div>'
        f'<div class="sa-sub">{escape(subtitle)}</div></div><div>{right_html}</div></div>',
        unsafe_allow_html=True,
    )


def section(title: str) -> None:
    st.markdown(f'<div class="sa-section">{escape(title)}</div>', unsafe_allow_html=True)


def kpis(items: Iterable[tuple[str, str, str | None, str]]) -> None:
    """items: (label, value, delta or None, tone of the delta)."""
    cells = []
    for label, value, delta, tone in items:
        d = f'<div class="d" style="color:{TONES[tone][0]}">{escape(delta)}</div>' if delta else ""
        cells.append(
            f'<div class="sa-kpi"><div class="l">{escape(label)}</div>'
            f'<div class="v" title="{escape(value)}">{escape(value)}</div>{d}</div>'
        )
    st.markdown(f'<div class="sa-kpis">{"".join(cells)}</div>', unsafe_allow_html=True)


def hero(title: str, subtitle: str = "", pills_html: str = "") -> None:
    st.markdown(
        f'<div class="sa-hero"><div class="sa-row"><div class="t">{escape(title)}</div>'
        f"<div>{pills_html}</div></div>"
        + (f'<div class="s">{escape(subtitle)}</div>' if subtitle else "")
        + "</div>",
        unsafe_allow_html=True,
    )


def muted(text: str) -> None:
    st.markdown(f'<div class="sa-muted">{escape(text)}</div>', unsafe_allow_html=True)


def tone_for(value: float | None) -> str:
    if value is None or value == 0:
        return "grey"
    return "green" if value > 0 else "red"
