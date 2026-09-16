"""Exporta la base a un lago de Parquet con manifiesto verificable.

La base son 6,15 GB que no están en git y no se pueden reconstruir sin semanas
de descargas sujetas a rate limit. Esto es la TASK 0.1 del plan: convertirla en
algo copiable, verificable y restaurable.

## Por qué Parquet por año y no un volcado

Un `.duckdb` de 6 GB es un fichero binario opaco: si se corrompe un byte no hay
manera de saber cuál ni de recuperar el resto. Un Parquet por tabla y año es
verificable pieza a pieza, se sube incrementalmente —solo lo que cambió— y se
lee sin DuckDB si algún día hiciera falta.

## Dos huellas por fichero, y no sobra ninguna

- `sha256`: del fichero. Detecta una descarga corrupta.
- `contenido`: XOR de los hashes de fila. Es independiente del orden y de los
  metadatos que Parquet escriba, así que sobrevive a que DuckDB cambie de
  versión. Es el que dice si los DATOS son los mismos, que es lo que importa
  al restaurar.

El destino va **fuera de OneDrive** por defecto: son gigabytes que cambian a
diario y sincronizarlos no aporta nada.

Uso:
    python scripts/exportar_lago.py
    python scripts/exportar_lago.py --destino D:/lago --solo insiders,ohlcv
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import duckdb

BD = Path("data/moonrocket.duckdb")
DESTINO = Path.home() / "moonrocket-lago"

#: Columna por la que se parte cada tabla en años. Es la fecha ACCIONABLE, no
#: la del hecho: en `insiders` se parte por `presentado` y no por `transaccion`
#: porque la segunda no era pública, y el mismo criterio ordena el resto.
COLUMNA_TIEMPO = {
    "book_depth": "ts",
    "calendario": "fecha",
    "decisions": "ts",
    "eventos": "aceptado",
    "experimentos": "ts",
    "fundamentales": "publicado",
    "funding": "ts",
    "hechos": "fecha",
    "insiders": "presentado",
    "metrics": "ts",
    "objetivos": "consultado",
    "ohlcv": "ts",
    "open_interest": "ts",
    "outcomes": "ts_cierre",
    "precios_insider": "fecha",
    "universo_presencia": "mes",
}

VERSION_MANIFIESTO = 1


def _sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with ruta.open("rb") as f:
        for trozo in iter(lambda: f.read(1024 * 1024), b""):
            h.update(trozo)
    return h.hexdigest()


def _columnas(con: duckdb.DuckDBPyConnection, tabla: str) -> list[str]:
    filas = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ? ORDER BY ordinal_position", [tabla]
    ).fetchall()
    return [f[0] for f in filas]


def _huella_contenido(con: duckdb.DuckDBPyConnection, tabla: str,
                      columna: str, anyo: int) -> int:
    """XOR de los hashes de fila: no depende del orden ni del formato."""
    cols = ", ".join(f'"{c}"' for c in _columnas(con, tabla))
    fila = con.execute(
        f"SELECT COALESCE(BIT_XOR(hash({cols})), 0) FROM \"{tabla}\" "
        f'WHERE EXTRACT(year FROM "{columna}") = ?', [anyo]
    ).fetchone()
    return int(fila[0]) if fila and fila[0] is not None else 0


def _anyos(con: duckdb.DuckDBPyConnection, tabla: str, columna: str) -> list[int]:
    filas = con.execute(
        f'SELECT DISTINCT EXTRACT(year FROM "{columna}")::INTEGER AS a '
        f'FROM "{tabla}" WHERE "{columna}" IS NOT NULL ORDER BY a'
    ).fetchall()
    return [f[0] for f in filas]


def _estado(bd: Path) -> tuple[int, int]:
    st = bd.stat()
    return st.st_size, st.st_mtime_ns


def exportar(bd: Path, destino: Path, solo: set[str] | None = None) -> dict:
    # Exportar 136 ficheros lleva minutos y NO es atómico. Si el Programador de
    # tareas lanza una descarga a mitad, unas tablas quedarían del estado viejo
    # y otras del nuevo, y la copia sería incoherente sin que nada lo dijera.
    # No se puede impedir desde aquí, pero sí detectar: se compara el estado del
    # fichero al empezar y al terminar.
    antes = _estado(bd)
    con = duckdb.connect(str(bd), read_only=True)
    tablas = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    if solo:
        tablas = [t for t in tablas if t in solo]

    manifiesto: dict = {
        "version": VERSION_MANIFIESTO,
        "origen": bd.name,
        "tablas": {},
    }

    for tabla in tablas:
        columna = COLUMNA_TIEMPO.get(tabla)
        if columna is None:
            raise SystemExit(
                f"tabla '{tabla}' sin columna de tiempo declarada. Añádela a "
                f"COLUMNA_TIEMPO: exportar sin partición no es reanudable."
            )
        total = con.execute(f'SELECT COUNT(*) FROM "{tabla}"').fetchone()[0]
        # El DDL viaja con los datos. Cinco de las dieciséis tablas no están en
        # store/schema.py —las crearon scripts de descarga—, así que reconstruir
        # el esquema desde el código dejaría fuera calendario, hechos, objetivos,
        # precios_insider y universo_presencia.
        ddl = con.execute(
            "SELECT sql FROM duckdb_tables() WHERE table_name = ?", [tabla]
        ).fetchone()
        info: dict = {"columna_tiempo": columna, "filas": total,
                      "ddl": ddl[0] if ddl else None, "ficheros": {}}

        if total == 0:
            # Una tabla vacía se anota: al restaurar hay que recrear el esquema
            # igualmente, y un hueco silencioso parecería una pérdida de datos.
            info["vacia"] = True
            manifiesto["tablas"][tabla] = info
            print(f"  {tabla:<20} vacía")
            continue

        carpeta = destino / tabla
        carpeta.mkdir(parents=True, exist_ok=True)
        cols = ", ".join(f'"{c}"' for c in _columnas(con, tabla))

        for anyo in _anyos(con, tabla, columna):
            ruta = carpeta / f"{anyo}.parquet"
            # ORDER BY para que el fichero no dependa del plan de ejecución.
            con.execute(
                f'COPY (SELECT * FROM "{tabla}" '
                f'      WHERE EXTRACT(year FROM "{columna}") = {anyo} '
                f'      ORDER BY {cols}) '
                f"TO '{ruta.as_posix()}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            n = con.execute(
                f'SELECT COUNT(*) FROM "{tabla}" '
                f'WHERE EXTRACT(year FROM "{columna}") = ?', [anyo]
            ).fetchone()[0]
            info["ficheros"][str(anyo)] = {
                "filas": n,
                "bytes": ruta.stat().st_size,
                "sha256": _sha256(ruta),
                "contenido": str(_huella_contenido(con, tabla, columna, anyo)),
            }

        sin_fecha = con.execute(
            f'SELECT COUNT(*) FROM "{tabla}" WHERE "{columna}" IS NULL'
        ).fetchone()[0]
        if sin_fecha:
            # No se tiran en silencio: irían a parar a ningún año.
            info["filas_sin_fecha"] = sin_fecha
            print(f"  ! {tabla}: {sin_fecha:,} filas con {columna} NULL, "
                  f"exportadas aparte")
            ruta = carpeta / "sin_fecha.parquet"
            con.execute(
                f'COPY (SELECT * FROM "{tabla}" WHERE "{columna}" IS NULL '
                f"      ORDER BY {cols}) TO '{ruta.as_posix()}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            info["ficheros"]["sin_fecha"] = {
                "filas": sin_fecha,
                "bytes": ruta.stat().st_size,
                "sha256": _sha256(ruta),
                "contenido": "0",
            }

        exportadas = sum(f["filas"] for f in info["ficheros"].values())
        if exportadas != total:
            raise SystemExit(
                f"{tabla}: exportadas {exportadas:,} de {total:,} filas. "
                f"Una exportación incompleta no es una copia."
            )
        manifiesto["tablas"][tabla] = info
        mb = sum(f["bytes"] for f in info["ficheros"].values()) / 1e6
        print(f"  {tabla:<20} {total:>12,} filas  "
              f"{len(info['ficheros']):>3} ficheros  {mb:>8,.1f} MB")

    con.close()
    if _estado(bd) != antes:
        raise SystemExit(
            "la base CAMBIÓ durante la exportación: unas tablas son del estado "
            "viejo y otras del nuevo. La copia es incoherente y no se firma. "
            "Para la tarea que esté escribiendo y vuelve a exportar."
        )
    return manifiesto


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bd", type=Path, default=BD)
    p.add_argument("--destino", type=Path, default=DESTINO)
    p.add_argument("--solo", type=str, default="",
                   help="tablas separadas por comas")
    args = p.parse_args()

    if not args.bd.exists():
        print(f"no existe {args.bd}")
        return 1

    solo = {t.strip() for t in args.solo.split(",") if t.strip()} or None
    args.destino.mkdir(parents=True, exist_ok=True)
    print(f"origen  {args.bd}  ({args.bd.stat().st_size / 1e9:.2f} GB)")
    print(f"destino {args.destino}\n")

    manifiesto = exportar(args.bd, args.destino, solo)

    # La fecha va al final y FUERA de lo que se compara entre ejecuciones: si
    # formara parte del cuerpo, dos exportaciones idénticas nunca coincidirían.
    cuerpo = json.dumps(manifiesto, indent=2, sort_keys=True, ensure_ascii=False)
    huella = hashlib.sha256(cuerpo.encode()).hexdigest()
    completo = {**manifiesto, "huella": huella,
                "exportado": datetime.now().astimezone().isoformat()}
    (args.destino / "MANIFIESTO.json").write_text(
        json.dumps(completo, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8")

    total_bytes = sum(
        f["bytes"] for t in manifiesto["tablas"].values()
        for f in t["ficheros"].values())
    print(f"\nhuella del lago  {huella}")
    print(f"total            {total_bytes / 1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
