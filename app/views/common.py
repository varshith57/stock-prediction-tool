"""Shared helpers for the app screens."""

from __future__ import annotations

import streamlit as st

from stockapp.config import AppConfig, get_app_config
from stockapp.lake import Lake


@st.cache_resource
def lake() -> Lake:
    return Lake.from_settings()


def cfg() -> AppConfig:
    return get_app_config()


def inr(x: float | None, decimals: int = 0) -> str:
    """Indian digit grouping: 12,34,567."""
    if x is None:
        return "—"
    sign = "-" if x < 0 else ""
    whole, _, frac = f"{abs(x):.{decimals}f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    return f"{sign}₹{whole}" + (f".{frac}" if decimals else "")


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x:+.2f}%"
