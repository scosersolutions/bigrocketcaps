"""Rellena `sector_empresa` para las empresas que hacen falta ahora mismo.

No se piden las 14.256 del universo: solo las que tienen un evento medible
-ticker conocido y al menos una fila de precio en el lago-. Pedir el SIC de
las que nunca se van a usar en un estudio son peticiones a la SEC que no
sirven para nada. Ver `brc.datos.sector` para el porque del cruce con
Fama-French en vez de GICS.

Uso:
    python scripts/obtener_sectores.py --clase emision_precio \\
        --desde 2018-01-01 --hasta 2024-12-31
"""
from __future__ import annotations

import argparse
import os

import duckdb
from dotenv import load_dotenv

from brc.datos import esquema, lago
from brc.datos.sector import obtener_sic


def universo_medible(
    con_local: duckdb.DuckDBPyConnection,
    con_lago: duckdb.DuckDBPyConnection,
    *,
    clase: str,
    desde: str,
    hasta: str,
    anyos: list[int],
) -> list[int]:
    """CIK con evento de esa clase en el rango Y al menos una vela en el lago.

    Sin precio no hay nada que medir, y pedir su SIC no sirve para nada: es
    el mismo filtro "precio cruzable por ticker" que exige el universo de B1,
    aplicado aquí para no gastar peticiones en empresas que `medir()` iba a
    descartar de todas formas.
    """
    tickers_evento = con_local.execute(
        "SELECT DISTINCT ticker FROM eventos_societarios "
        "WHERE clase = ? AND presentado BETWEEN ? AND ? "
        "AND ticker IS NOT NULL AND ticker != ''",
        [clase, desde, hasta],
    ).pl()["ticker"].to_list()
    if not tickers_evento:
        return []

    expr = lago.leer(con_lago, "ohlcv", anyos)
    con_lago.execute(
        f"CREATE OR REPLACE TEMP TABLE _precios AS "
        f"SELECT DISTINCT activo FROM {expr} "
        f"WHERE mercado = 'stock_us' AND timeframe = '1d'"
    )
    con_lago.execute("CREATE OR REPLACE TEMP TABLE _tickers_evento(ticker VARCHAR)")
    con_lago.executemany("INSERT INTO _tickers_evento VALUES (?)",
                         [[t] for t in tickers_evento])
    con_tickers = con_lago.execute(
        "SELECT t.ticker FROM _tickers_evento t JOIN _precios p ON p.activo = t.ticker"
    ).pl()["ticker"].to_list()
    if not con_tickers:
        return []

    marcadores = ",".join("?" * len(con_tickers))
    return con_local.execute(
        f"SELECT DISTINCT cik FROM eventos_societarios WHERE ticker IN ({marcadores})",
        con_tickers,
    ).pl()["cik"].to_list()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default="data/bigrocketcaps.duckdb")
    p.add_argument("--clase", default="emision_precio")
    p.add_argument("--desde", default="2018-01-01")
    p.add_argument("--hasta", default="2024-12-31")
    p.add_argument("--anyo-desde", type=int, default=2017)
    p.add_argument("--anyo-hasta", type=int, default=2025)
    a = p.parse_args()

    load_dotenv()
    user_agent = os.environ.get("MR_SEC_USER_AGENT", "")
    if not user_agent:
        print("falta MR_SEC_USER_AGENT en el .env")
        return 2

    con_local = duckdb.connect(a.base)
    con_local.execute(esquema.DDL)
    con_lago = lago.conectar()

    todos = universo_medible(
        con_local, con_lago, clase=a.clase, desde=a.desde, hasta=a.hasta,
        anyos=list(range(a.anyo_desde, a.anyo_hasta + 1)),
    )
    ya = set(con_local.execute("SELECT cik FROM sector_empresa").pl()["cik"].to_list())
    pendientes = [c for c in todos if c not in ya]
    print(f"{len(todos)} empresas medibles, {len(ya)} ya tenian sector, "
          f"{len(pendientes)} por pedir")
    if not pendientes:
        return 0

    df = obtener_sic(pendientes, user_agent)
    print(f"{df.height} resueltas de {len(pendientes)} pedidas "
          f"({len(pendientes) - df.height} sin respuesta o 404)")
    if df.height:
        con_local.execute("INSERT OR REPLACE INTO sector_empresa SELECT * FROM df")
        con_local.commit()

    resumen = con_local.execute(
        "SELECT sector, COUNT(*) FROM sector_empresa GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    print("reparto por sector:", resumen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
