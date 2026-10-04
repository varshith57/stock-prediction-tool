"""Synthetic files in each NSE format (made-up symbols and values; real layouts).

Used by parser and pipeline tests so CI never needs real exchange data.
"""

from __future__ import annotations

import json
from datetime import date

from conftest import zip_csv

LEGACY_HEADER = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,"
    "TOTALTRADES,ISIN,"
)
UDIFF_HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
    "PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
    "TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)
_MON = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

# symbol, series, close, volume
STOCKS = [
    ("ALPHAIND", "EQ", 103.2, 250_000),
    ("BETAFIN", "EQ", 1991.1, 12_000),
    ("GAMMAPWR", "BE", 42.85, 800),
    ("DELTASME", "SM", 312.0, 3_000),
]


def legacy_zip(day: date, stocks=STOCKS) -> bytes:
    ts = f"{day:%d}-{_MON[day.month - 1]}-{day:%Y}"
    rows = [LEGACY_HEADER]
    for i, (sym, ser, close, vol) in enumerate(stocks):
        o, h, lo = close * 0.99, close * 1.02, close * 0.98
        rows.append(
            f"{sym},{ser},{o:.2f},{h:.2f},{lo:.2f},{close:.2f},{close:.2f},{o:.2f},{vol},"
            f"{vol * close:.2f},{ts},{10 + i},INE00{i}A01010,"
        )
    return zip_csv("\n".join(rows) + "\n", f"cm{day:%d}{_MON[day.month - 1]}{day:%Y}bhav.csv")


def udiff_zip(day: date, stocks=STOCKS) -> bytes:
    rows = [UDIFF_HEADER]
    for i, (sym, ser, close, vol) in enumerate(stocks):
        o, h, lo = close * 0.99, close * 1.02, close * 0.98
        rows.append(
            f"{day},{day},CM,NSE,STK,{9000 + i},INE00{i}A01010,{sym},{ser},,,,,{sym} LTD,"
            f"{o:.2f},{h:.2f},{lo:.2f},{close:.2f},{close:.2f},{o:.2f},,{close:.2f},,,{vol},"
            f"{vol * close:.2f},{10 + i},F1,1,,,,,"
        )
    return zip_csv("\n".join(rows) + "\n", f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv")


def mto_bytes(day: date, stocks=STOCKS, header_day: date | None = None) -> bytes:
    hd = header_day or day
    lines = [
        "Security Wise Delivery Position - Compulsory Rolling Settlement",
        f"10,MTO,{day:%d%m%Y},123456789,0001561",
        f"Trade Date <{hd:%d}-{_MON[hd.month - 1]}-{hd:%Y}>,Settlement Type <N>,"
        f"Settlement No <2016002>,Settlement Date <06-JAN-2016>",
        "Record Type,Sr No,Name of Security,Quantity Traded,"
        "Deliverable Quantity(gross across client level),"
        "% of Deliverable Quantity to Traded Quantity",
    ]
    for i, (sym, ser, _close, vol) in enumerate(stocks):
        deliv = vol // 2
        lines.append(f"20,{i + 1},{sym},{ser},{vol},{deliv},{100 * deliv / vol:.2f}")
    return ("\r\n".join(lines) + "\r\n").encode()


INDEX_HEADER = (
    "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,"
    "Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield"
)


def index_csv(day: date, names=("Nifty 50", "Nifty 500", "India VIX", "Nifty Bank")) -> bytes:
    d = f"{day:%d-%m-%Y}"
    rows = [INDEX_HEADER]
    for i, n in enumerate(names):
        if n == "India VIX":
            rows.append(f"{n},{d},14.26,17.1,14.03,16.84,2.58,18.06,-,-,-,-,-")
        else:
            c = 1000.0 * (i + 1)
            rows.append(f"{n},{d},{c},{c + 10},{c - 10},{c + 5},5,.5,1000,10.5,20.1,3.1,1.4")
    return ("\n".join(rows) + "\n").encode()


def corp_actions_json(month: date, n: int = 3) -> bytes:
    rows = []
    for i in range(n):
        rows.append(
            {
                "bcEndDate": "-",
                "bcStartDate": "-",
                "caBroadcastDate": None,
                "comp": f"Company {i} Limited",
                "exDate": f"{5 + i:02d}-{month:%b-%Y}",
                "faceVal": "10",
                "ind": "-",
                "isin": f"INE00{i}A01010",
                "ndEndDate": "-",
                "ndStartDate": "-",
                "recDate": f"{6 + i:02d}-{month:%b-%Y}",
                "series": "EQ",
                "subject": [" Bonus 1:2", "Dividend - Rs 2 Per Share", "Face Value Split"][i % 3],
                "symbol": f"SYM{i}",
            }
        )
    return json.dumps(rows).encode()
