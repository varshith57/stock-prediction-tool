"""Monthly Audit: built in M10."""

from __future__ import annotations

import streamlit as st


def render() -> None:
    st.header("Monthly Audit")
    st.info("The audit starts once signals are being logged (M10).")
