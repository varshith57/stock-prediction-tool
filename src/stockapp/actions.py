"""Parse NSE corporate-action subjects into typed events with price-adjustment factors.

A subject can hold several actions joined by "/" (but "Rs 5/-" is a price, not a separator):
"Bonus 1:1/Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share".

``price_factor`` multiplies prices *before* the ex-date so they're comparable with prices after it:

* bonus a:b (a new shares for every b held): b / (a + b)
* face-value split or consolidation from X to Y: Y / X
* rights a:b at an issue price: computed later from the prior close (theoretical ex-rights price),
  since it depends on the market price; here only the terms are parsed.

Events that move the price but can't be adjusted from the subject alone (demerger, amalgamation,
capital reduction, bonus debentures, partly paid rights) are ``unadjustable``: they become series
breaks, so later features never compute a return across them. A component that looks
price-affecting but doesn't match a known wording is ``unparsed`` and gets quarantined: nothing
is guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal[
    "bonus", "split", "consolidation", "rights", "dividend", "demerger", "amalgamation",
    "capital_reduction", "bonus_debenture", "restructuring", "other",
]  # fmt: skip
Status = Literal["parsed", "unadjustable", "no_price_effect", "unparsed"]

_NUM = r"(\d+(?:\.\d+)?)"
_RS = r"(?:rs\.?|re\.?|inr)?\s*"
# "/" not followed by "-" (so "Rs 5/-" stays intact), or " + " ("Interim Div ... + FV Split")
_SEP = re.compile(r"\s*/(?!\s*-)\s*|\s+\+\s+")

_BONUS = re.compile(rf"\bbonus\b\s*(?:issue)?\s*-?\s*{_NUM}\s*:\s*{_NUM}")
# "from Rs 10/- per share to Rs 2/-", or without "from": "Consolidation Rs 1 to Rs 10"
# "From Rs 10/- ... To Rs 2/-" (also abbreviated "Frm"); without from: "Consolidation Rs 1 to Rs 10"
_FV_FROM = re.compile(rf"\b(?:from|frm)\s*{_RS}{_NUM}\s*(?:/-)?.*?\bto\s*{_RS}{_NUM}")
_FV_BARE = re.compile(rf"\b(?:rs|re)\.?\s*{_NUM}\s*(?:/-)?\s*(?:per share\s*)?to\s*{_RS}{_NUM}")
_RIGHTS = re.compile(
    rf"\brights\b\s*(?:issue)?\s*-?\s*{_NUM}\s*:\s*{_NUM}"
    rf"(?:\s*(?:fully paid up shares)?\s*@\s*(?:prem(?:ium)?\.?|prm\.?)?\s*{_RS}{_NUM})?"
)
# currency prefix as written in dividend subjects: "Rs 2", "Rs - 2", "Rs- 1.40", "Rs Rs 0.50"
_RS_LOOSE = r"(?:(?:rs\.?|re\.?|inr)\s*-?\s*)*"
_DIV_RS = re.compile(rf"dividend\s*-?\s*{_RS_LOOSE}{_NUM}")
_DIV_PER_UNIT = re.compile(rf"{_RS_LOOSE}{_NUM}\s*per\s*(?:unit|share)\s*as\s*dividend")
_DIV_PCT = re.compile(rf"dividend\s*-?\s*{_NUM}\s*%")

_UNADJUSTABLE = (
    ("demerger", re.compile(r"de-?merger")),
    ("amalgamation", re.compile(r"amalgamation|\bmerger\b")),
    ("capital_reduction", re.compile(r"capital reduction|reduction of capital")),
    # a bare "Scheme of Arrangement" is usually a demerger or restructuring (e.g. SINTEX 2017)
    ("restructuring", re.compile(r"scheme of arrangement")),
)
_PRICE_WORDS = re.compile(r"bonus|split|splt|sub-?division|consolidat|rights|dividend")


@dataclass(frozen=True)
class Parsed:
    kind: Kind
    status: Status
    component: str
    ratio_new: float | None = None  # bonus/rights: a in a:b
    ratio_held: float | None = None  # bonus/rights: b in a:b
    from_fv: float | None = None
    to_fv: float | None = None
    rights_premium: float | None = None
    dividend_per_share: float | None = None
    price_factor: float | None = None


def split_components(subject: str) -> list[str]:
    return [c for c in _SEP.split(subject.strip()) if c.strip()]


def parse_component(component: str, face_value: float | None) -> Parsed:
    text = component.lower().strip()

    if "bonus" in text and re.search(r"debenture|ncrps|preference", text):
        # bonus debentures / preference shares distribute value without a share-count ratio
        return Parsed("bonus_debenture", "unadjustable", component)
    for kind, pattern in _UNADJUSTABLE:
        if pattern.search(text):
            # "Capital Reduction ... / Consolidation Rs 1 to Rs 10" is split into components first,
            # so a consolidation alongside a reduction is still parsed on its own.
            return Parsed(kind, "unadjustable", component)  # type: ignore[arg-type]

    if "bonus" in text:
        m = _BONUS.search(text)
        if not m:
            return Parsed("bonus", "unparsed", component)
        a, b = float(m[1]), float(m[2])
        if a <= 0 or b <= 0:
            return Parsed("bonus", "unparsed", component)
        return Parsed("bonus", "parsed", component, a, b, price_factor=b / (a + b))

    if re.search(r"split|splt|sub-?division|consolidat", text):
        kind: Kind = "consolidation" if "consolidat" in text else "split"
        m = _FV_FROM.search(text) or _FV_BARE.search(text)
        if not m:
            return Parsed(kind, "unparsed", component)
        frm, to = float(m[1]), float(m[2])
        if frm <= 0 or to <= 0 or frm == to:
            return Parsed(kind, "unparsed", component)
        kind = "consolidation" if to > frm else "split"
        return Parsed(kind, "parsed", component, from_fv=frm, to_fv=to, price_factor=to / frm)

    if "rights" in text:
        if re.search(r"partly paid|ccps|debenture|warrant|preference", text):
            # rights to other instruments or partly paid shares: no clean equity ratio
            return Parsed("rights", "unadjustable", component)
        m = _RIGHTS.search(text)
        if not m:
            return Parsed("rights", "unparsed", component)
        a, b = float(m[1]), float(m[2])
        if m[3] is None:
            # no issue price in the subject: the dilution can't be computed, so don't guess
            return Parsed("rights", "unadjustable", component, a, b)
        return Parsed("rights", "parsed", component, a, b, rights_premium=float(m[3]))

    if re.search(r"dividend|\bdiv\b", text):
        text = re.sub(r"\bdiv\b", "dividend", text)
        pct = _DIV_PCT.search(text)  # check "50%" first, or it would read as Rs 50
        if pct:
            if not face_value:
                return Parsed("dividend", "unparsed", component)
            per_share = float(pct[1]) / 100 * face_value
            return Parsed("dividend", "parsed", component, dividend_per_share=per_share)
        amounts = [float(x) for x in _DIV_RS.findall(text)] or [
            float(x) for x in _DIV_PER_UNIT.findall(text)
        ]
        if amounts:
            return Parsed("dividend", "parsed", component, dividend_per_share=sum(amounts))
        return Parsed("dividend", "unparsed", component)

    if _PRICE_WORDS.search(text):
        return Parsed("other", "unparsed", component)
    return Parsed("other", "no_price_effect", component)


def parse_subject(subject: str, face_value: float | None = None) -> list[Parsed]:
    return [parse_component(c, face_value) for c in split_components(subject)]


def rights_factor(
    prior_close: float, ratio_new: float, ratio_held: float, issue_price: float
) -> float:
    """Price factor for a rights issue from the theoretical ex-rights price (TERP).

    TERP = (held x prior close + new x issue price) / (held + new); factor = TERP / prior close.
    A rights price at or above the market transfers no value, so the factor is capped at 1.
    """
    terp = (ratio_held * prior_close + ratio_new * issue_price) / (ratio_held + ratio_new)
    return min(1.0, terp / prior_close)
