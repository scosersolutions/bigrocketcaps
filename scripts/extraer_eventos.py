"""Saca los eventos societarios del paquete masivo de EDGAR.

Lee `submissions.zip` (1,56 GB) sin descomprimirlo: dentro hay un JSON por
empresa con TODAS sus presentaciones historicas, y descomprimido pasa de diez
gigabytes que no hacen falta en disco.

Se queda solo con lo que puede mover un precio --cambios de directivo, M&A,
resultados, quiebras, posiciones activistas, ampliaciones-- y guarda la HORA de
aceptacion, que es lo que decide si el evento se pudo operar ese dia.

Uso:
    python scripts/descargar_submissions.py      # baja el zip (aparte)
    python scripts/extraer_eventos.py
    python scripts/extraer_eventos.py --prueba 500
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl

from brc.datos.esquema import DDL
from brc.datos.eventos import eventos_de, recorrer_zip

ZIP = Path("./data/submissions.zip")
BD = Path("./data/bigrocketcaps.duckdb")
FUENTE = "sec_submissions_bulk"
LOTE = 2_000


def guardar(con: duckdb.DuckDBPyConnection, df: pl.DataFrame,
            ahora: datetime) -> int:
    if df.is_empty():
        return 0
    con.register("entrada", df.to_arrow())
    con.execute(
        "INSERT OR REPLACE INTO eventos_societarios "
        "SELECT cik, ticker, accession, formulario, clase, items, aceptado, "
        "       presentado, periodo, tras_cierre, ?, ? FROM entrada",
        [ahora, FUENTE])
    con.unregister("entrada")
    return df.height


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--zip", type=Path, default=ZIP)
    p.add_argument("--bd", type=Path, default=BD)
    p.add_argument("--prueba", type=int, default=0,
                   help="solo las N primeras empresas")
    a = p.parse_args()

    if not a.zip.exists():
        print(f"no existe {a.zip}. Bajalo primero.")
        return 1

    con = duckdb.connect(str(a.bd))
    con.execute(DDL)
    ahora = datetime.now(UTC)

    empresas = eventos = 0
    lote: list[pl.DataFrame] = []
    for _, cruda in recorrer_zip(a.zip, limite=a.prueba):
        empresas += 1
        df = eventos_de(cruda)
        if not df.is_empty():
            lote.append(df)
        if len(lote) >= LOTE:
            eventos += guardar(con, pl.concat(lote), ahora)
            lote.clear()
            print(f"  {empresas:>6,} empresas · {eventos:>9,} eventos", flush=True)
    if lote:
        eventos += guardar(con, pl.concat(lote), ahora)

    print(f"\n{empresas:,} empresas leidas · {eventos:,} eventos guardados")
    filas = con.execute("SELECT COUNT(*) FROM eventos_societarios").fetchone()[0]
    print(f"{filas:,} en la tabla")
    print("\npor clase:")
    for clase, n, tras in con.execute(
        "SELECT clase, COUNT(*), ROUND(AVG(CASE WHEN tras_cierre THEN 1.0 ELSE 0 END)*100) "
        "FROM eventos_societarios GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {clase:<24} {n:>9,}   {tras:>3.0f} % tras el cierre")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
