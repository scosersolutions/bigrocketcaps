"""Descarga las acciones en circulación de todas las empresas, trimestre a trimestre.

Es el primer paso del universo point-in-time, y el que decide si el resto del
proyecto se apoya en algo sólido o en la composición de un índice de hoy.

## Lo que hace

Por cada trimestre pide a la API `frames` de XBRL quién declaró acciones en
circulación y cuántas. Verificado el 2026-09-16:

    CY2012Q1I  7.016 empresas      CY2020Q1I  4.774
    CY2015Q1I  6.258 empresas      CY2026Q1I  4.611

La bajada no es pérdida de cobertura: hay menos empresas cotizadas en Estados
Unidos que hace diez años.

## Lo que NO hace

No cruza `accn` con las presentaciones, así que **lo que guarda todavía no es
accionable**: falta saber cuándo se publicó cada dato. Ese cruce es el paso
siguiente y va aparte a propósito — mezclar descarga y fechado hace que un
fallo en el segundo parezca un hueco de datos del primero.

## Reanudable

Lo que ya está en la base no se vuelve a pedir. Una descarga de 60 trimestres
son unas 60 peticiones; cortarla y reanudarla no cuesta nada.

Uso:
    python scripts/descargar_universo.py --desde 2012 --hasta 2026
    python scripts/descargar_universo.py --prueba 2
"""
from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx

from brc.datos.esquema import DDL
from brc.datos.universo import Trimestre, UniversoError, acciones_en_circulacion

BD = Path("./data/bigrocketcaps.duckdb")
FUENTE = "sec_frames_dei"


def _user_agent() -> str:
    ua = os.environ.get("MR_SEC_USER_AGENT", "")
    if "@" not in ua or "ejemplo.com" in ua or "tu-email" in ua:
        raise UniversoError(
            "MR_SEC_USER_AGENT tiene que llevar TU correo real.\n"
            "La SEC lo exige para poder avisar antes de bloquear; con uno "
            "inventado no hay aviso, hay bloqueo.\n"
            "Ponlo en .env:  MR_SEC_USER_AGENT=BigRocketCaps/0.1 (tu@correo)"
        )
    return ua


def ya_descargados(con: duckdb.DuckDBPyConnection) -> set[str]:
    filas = con.execute(
        "SELECT DISTINCT trimestre FROM acciones_circulacion").fetchall()
    return {f[0] for f in filas}


def guardar(con: duckdb.DuckDBPyConnection, df, ahora: datetime) -> int:
    if df.is_empty():
        return 0
    con.register("entrada", df.to_arrow())
    con.execute(
        "INSERT OR REPLACE INTO acciones_circulacion "
        "SELECT cik, nombre, trimestre, fin, accn, acciones, ?, ? FROM entrada",
        [ahora, FUENTE])
    con.unregister("entrada")
    return df.height


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bd", type=Path, default=BD)
    p.add_argument("--desde", type=int, default=2012)
    p.add_argument("--hasta", type=int, default=datetime.now().year)
    p.add_argument("--prueba", type=int, default=0,
                   help="solo los N primeros trimestres, para probar la tubería")
    a = p.parse_args()

    try:
        ua = _user_agent()
    except UniversoError as e:
        print(f"\n{e}\n")
        return 2

    a.bd.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(a.bd))
    con.execute(DDL)

    pendientes = Trimestre.rango(a.desde, a.hasta)
    hechos = ya_descargados(con)
    pendientes = [t for t in pendientes if t.clave not in hechos]
    if a.prueba:
        pendientes = pendientes[: a.prueba]

    print(f"base      {a.bd}")
    print(f"ya en base {len(hechos)} trimestres · pendientes {len(pendientes)}\n")

    total = 0
    with httpx.Client(timeout=60, follow_redirects=True) as cliente:
        for t in pendientes:
            try:
                df = acciones_en_circulacion(t, user_agent=ua, cliente=cliente)
            except UniversoError as e:
                # Un trimestre que falla no tira la descarga entera, pero se
                # dice: un hueco silencioso parecería que no había empresas.
                print(f"  {t.clave}  FALLO: {e}")
                continue
            n = guardar(con, df, datetime.now(UTC))
            total += n
            print(f"  {t.clave}  {n:>6,} empresas" if n else
                  f"  {t.clave}  sin datos")

    filas, empresas = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT cik) FROM acciones_circulacion"
    ).fetchone()
    print(f"\n{total:,} filas nuevas · {filas:,} en total · "
          f"{empresas:,} empresas distintas")
    print("\nOJO: esto todavía NO es accionable. Falta cruzar `accn` con las")
    print("presentaciones para saber CUÁNDO se publicó cada dato.")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
