"""Streamlit entry point: login, navigation across the four screens, and the data-health line.

Run locally: ``uv run streamlit run app/streamlit_app.py``
"""

from __future__ import annotations

import streamlit as st

from stockapp.auth import verify_password
from stockapp.config import get_settings

st.set_page_config(page_title="Stockapp", page_icon="📈", layout="wide")


def _login() -> bool:
    if st.session_state.get("authed"):
        return True
    stored = get_settings().app_password_hash
    st.title("Stockapp")
    if stored is None:
        st.error(
            "No login is configured. Run `uv run stockapp set-password`, add the printed "
            "APP_PASSWORD_HASH line to .env, then restart the app."
        )
        return False
    with st.form("login"):
        password = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in")
    if ok:
        if verify_password(password, stored.get_secret_value()):
            st.session_state["authed"] = True
            st.rerun()
        st.error("Wrong password.")
    return False


if not _login():
    st.stop()

from views import audit, data_status, portfolio, settings, this_week  # noqa: E402

data_status.header_line()
nav = st.navigation(
    [
        st.Page(this_week.render, title="This Week", icon="🗓️", url_path="this-week", default=True),
        st.Page(portfolio.render, title="Portfolio", icon="💼", url_path="portfolio"),
        st.Page(audit.render, title="Monthly Audit", icon="📋", url_path="audit"),
        st.Page(settings.render, title="Settings", icon="⚙️", url_path="settings"),
    ]
)
nav.run()
st.caption("Personal research tool, not investment advice. No orders are placed from this app.")
