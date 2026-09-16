"""Puente ticker <-> CIK, que es lo que une los eventos con los precios.

EDGAR habla de CIK y los precios de ticker. Sin este puente, 1,6 millones de
eventos y diez millones de velas no se pueden mirar juntos.

La correspondencia NO es estable: los tickers se reciclan cuando una empresa
desaparece y otra hereda sus letras. Por eso se guarda con la fecha en que se
comprobo, y no como si fuera una verdad eterna. Un estudio que cruce por ticker
sin mirar fechas atribuira a una empresa los eventos de otra.

Uso:
    python scripts/descargar_tickers.py
"""
from __future__ import annotations

import argparse
import os
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import httpx
from dotenv import load_dotenv

from brc.datos.esquema import DDL

URL = "https://www.sec.gov/files/company_tickers.json"
BD = Path("./data/bigrocketcaps.duckdb")
FUENTE = "sec_company_tickers"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bd", type=Path, default=BD)
    a = p.parse_args()

    load_dotenv()
    ua = os.environ.get("MR_SEC_USER_AGENT", "")
    if "@" not in ua or "ejemplo.com" in ua:
        print("MR_SEC_USER_AGENT necesita un correo real")
        return 2

    r = httpx.get(URL, headers={"User-Agent": ua}, timeout=60,
                  follow_redirects=True)
    r.raise_for_status()
    filas = list(r.json().values())
    hoy = date.today()

    con = duckdb.connect(str(a.bd))
    con.execute(DDL)
    con.executemany(
        "INSERT OR REPLACE INTO tickers VALUES (?, ?, ?, ?)",
        [(f["ticker"].upper(), int(f["cik_str"]), hoy, FUENTE) for f in filas])

    n, ciks = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT cik) FROM tickers").fetchone()
    print(f"{len(filas):,} pares descargados · {n:,} en la tabla · {ciks:,} CIK")

    # Los tickers que apuntan a mas de un CIK son los reciclados: se cuentan
    # para saber cuanto ruido mete cruzar por ticker sin mirar fechas.
    rec = con.execute(
        "SELECT COUNT(*) FROM (SELECT ticker FROM tickers GROUP BY 1 "
        "HAVING COUNT(DISTINCT cik) > 1)").fetchone()[0]
    print(f"tickers con mas de un CIK: {rec:,}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
