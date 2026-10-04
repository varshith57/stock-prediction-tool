"""This Week (home): built in M8. Shows an honest placeholder until signals exist."""

from __future__ import annotations

import streamlit as st


def render() -> None:
    st.header("This Week")
    st.info(
        "No signals yet. The weekly plan arrives with the models (M6 to M8), and every signal "
        "type stays OFF until it passes the 90% validation gate."
    )
