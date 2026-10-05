"""Streamlit entry point: sign-in, sidebar navigation across the four screens, data status.

Run locally (this Mac only): ``uv run streamlit run app/streamlit_app.py``
"""

from __future__ import annotations

import streamlit as st

from stockapp.auth import verify_password
from stockapp.config import get_settings

st.set_page_config(page_title="Stockapp", page_icon=":material/monitoring:", layout="wide")

from pathlib import Path  # noqa: E402

from views import ui  # noqa: E402

ui.inject()
LOGO = str(Path(__file__).parent / "assets" / "logo.svg")


def _login() -> bool:
    if st.session_state.get("authed"):
        return True
    stored = get_settings().app_password_hash
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        st.markdown('<div style="height:12vh"></div>', unsafe_allow_html=True)
        st.markdown('<div class="sa-brand">stock<span>app</span></div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="sa-title" style="margin-top:.6rem">Sign in</div>', unsafe_allow_html=True
        )
        ui.muted("Your private research workspace on this Mac.")
        if stored is None:
            st.error(
                "No login is configured. Run `uv run stockapp set-password`, add the printed "
                "APP_PASSWORD_HASH line to .env, then restart the app."
            )
            return False
        with st.form("login", border=False):
            password = st.text_input("Password", type="password")
            ok = st.form_submit_button("Sign in", type="primary", use_container_width=True)
        if ok:
            if verify_password(password, stored.get_secret_value()):
                st.session_state["authed"] = True
                st.rerun()
            st.error("Wrong password.")
    return False


if not _login():
    st.stop()

from views import audit, data_status, portfolio, settings, this_week  # noqa: E402

st.logo(LOGO, size="large")

nav = st.navigation(
    [
        st.Page(
            this_week.render,
            title="This week",
            icon=":material/today:",
            url_path="this-week",
            default=True,
        ),
        st.Page(
            portfolio.render,
            title="Portfolio",
            icon=":material/account_balance_wallet:",
            url_path="portfolio",
        ),
        st.Page(
            audit.render, title="Monthly audit", icon=":material/fact_check:", url_path="audit"
        ),
        st.Page(settings.render, title="Settings", icon=":material/tune:", url_path="settings"),
    ]
)

with st.sidebar:
    st.markdown('<div style="height:1.2rem"></div>', unsafe_allow_html=True)
    ui.muted("Nifty 500 · weekly decisions")
    data_status.sidebar_status()
    if st.button("Sign out", icon=":material/logout:", type="tertiary"):
        st.session_state.clear()
        st.rerun()

data_status.quality_alert()
nav.run()
st.markdown('<div style="height:2rem"></div>', unsafe_allow_html=True)
ui.muted("Personal research tool, not investment advice. No orders are placed from this app.")
