"""Los datos viven en Hugging Face; en local solo hay caché.

## El cambio que hace esto

Antes el `.duckdb` local era el original: 6,15 GB irreemplazables que si se
perdían costaban semanas de descargas con rate limit. Ahora el archivo maestro
está en un dataset privado de Hugging Face y lo de aquí es una copia de
trabajo. Perder el portátil pasa de ser una catástrofe a ser una tarde.

## Qué se puede hacer sin descargar nada

DuckDB lee Parquet remoto por HTTP y solo se trae las columnas y los bloques de
filas que pide la consulta. Medido el 2026-09-16 contra el lago real:

    insiders 2024 (31.350 filas)       contar 1,0 s · agregar 1,8 s
    ohlcv de un año (5.456.999 filas)  contar 1,2 s · agregar 2,5 s

Cinco millones y medio de filas agregadas en dos segundos y medio sin bajar un
byte a disco. Para construir universos, contar eventos, explorar o comprobar
cobertura, esto sobra.

## Qué NO se debe hacer así

Un backtest que recorre vela a vela en Python miles de veces necesita los datos
en memoria local. Pedirlos por HTTP en cada pasada sería absurdo. Para eso está
`materializar()`, que baja a disco solo las tablas y los años que ese estudio
usa, y deja constancia de cuánto ocupa.

La regla, entonces: **explorar en remoto, medir en local**. Y lo local es
siempre borrable, porque se rebaja.
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb

#: Dataset privado con el archivo maestro. Privado no es una preferencia: el
#: lago lleva precios derivados de Yahoo, cuyos términos prohíben
#: redistribuirlos.
REPO = os.environ.get("BRC_LAGO", "scoser/moonrocket-lago")

#: Las tablas propias de bigrocketcaps van a OTRO dataset, tambien privado.
#: No es orden: un MANIFIESTO describe UNA exportacion de UNA base, con su
#: origen y su huella, y meterlas en el lago de MoonRocket obligaria a
#: elegir entre machacar ese manifiesto -y dejar 2,4 GB sin poder
#: restaurar- o subir ficheros que el manifiesto no declara, que es justo
#: el tipo de incoherencia silenciosa que `exportar_lago` se esfuerza en
#: impedir. Ademas salen de EDGAR, que es dominio publico, asi que algun
#: dia pueden abrirse sin arrastrar los precios de Yahoo con ellas.
REPO_PROPIO = os.environ.get("BRC_LAGO_PROPIO", "scoser/bigrocketcaps-lago")

#: Que tablas viven en `REPO_PROPIO`. Las demas, en `REPO`.
TABLAS_PROPIAS = frozenset({"eventos_societarios", "sector_empresa"})

#: Dónde se deja lo materializado. Fuera del repositorio y fuera de OneDrive:
#: son gigabytes que cambian y sincronizarlos no aporta nada.
CACHE = Path(os.environ.get("BRC_CACHE", Path.home() / ".cache" / "brc-lago"))


class LagoError(RuntimeError):
    """No se pudo llegar al archivo maestro."""


def _token() -> str:
    tok = os.environ.get("HF_TOKEN", "")
    if not tok:
        # `hf auth login` lo deja aquí; se lee en vez de exigir la variable,
        # para que funcione igual en una terminal recién abierta.
        fichero = Path.home() / ".cache" / "huggingface" / "token"
        tok = fichero.read_text(encoding="utf-8").strip() if fichero.exists() else ""
    if not tok:
        raise LagoError(
            "no hay credencial de Hugging Face. `hf auth login`, o HF_TOKEN "
            "en el .env. El dataset es privado y sin token no se ve.")
    return tok


def conectar(bd: str | Path = ":memory:") -> duckdb.DuckDBPyConnection:
    """Conexión de DuckDB capaz de leer el lago remoto."""
    con = duckdb.connect(str(bd))
    # La barra de progreso escribe miles de lineas de escape cuando la salida
    # no es una terminal, y se come el informe que venga detras.
    con.execute("SET enable_progress_bar = false")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"CREATE OR REPLACE SECRET hf (TYPE huggingface, TOKEN '{_token()}')")
    return con


def repo_de(tabla: str) -> str:
    """En que dataset vive esa tabla."""
    return REPO_PROPIO if tabla in TABLAS_PROPIAS else REPO


def ruta(tabla: str, anyo: int | str) -> str:
    """La URL de un trozo del lago, tal como la entiende DuckDB."""
    return f"hf://datasets/{repo_de(tabla)}/{tabla}/{anyo}.parquet"


def leer(con: duckdb.DuckDBPyConnection, tabla: str,
         anyos: list[int] | None = None) -> str:
    """Devuelve la expresión SQL que lee esa tabla, remota o cacheada.

    Se devuelve la EXPRESIÓN y no los datos a propósito: así quien consulta
    escribe su SQL normal y decide qué columnas y filas quiere, que es
    justamente lo que hace que leer en remoto salga barato.
    """
    trozos = []
    for anyo in (anyos or []):
        local = CACHE / tabla / f"{anyo}.parquet"
        trozos.append(f"'{local.as_posix()}'" if local.exists()
                      else f"'{ruta(tabla, anyo)}'")
    if not trozos:
        # Sin años, todo lo que haya: el comodín funciona igual en las dos
        # puntas, y si hay caché local se prefiere.
        local = CACHE / tabla
        if local.exists() and any(local.glob("*.parquet")):
            return f"read_parquet('{(local / '*.parquet').as_posix()}')"
        # Por `ruta` y no a mano: el comodin tiene que ir al MISMO dataset
        # que un año suelto, o pedir la tabla entera y pedir un año darian
        # sitios distintos.
        return f"read_parquet('{ruta(tabla, '*')}')"
    return f"read_parquet([{', '.join(trozos)}])"


def materializar(tabla: str, anyos: list[int], *,
                 con: duckdb.DuckDBPyConnection | None = None) -> Path:
    """Baja esos años a disco para poder machacarlos sin pedir nada por red.

    Para un backtest. Para mirar datos NO hace falta: leer en remoto cuesta
    segundos y no deja nada que limpiar después.
    """
    propio = con is None
    con = con or conectar()
    destino = CACHE / tabla
    destino.mkdir(parents=True, exist_ok=True)
    try:
        for anyo in anyos:
            fichero = destino / f"{anyo}.parquet"
            if fichero.exists():
                continue
            con.execute(
                f"COPY (SELECT * FROM read_parquet('{ruta(tabla, anyo)}')) "
                f"TO '{fichero.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    finally:
        if propio:
            con.close()
    return destino


def ocupado() -> int:
    """Bytes de la caché. Lo que se puede borrar sin perder nada."""
    if not CACHE.exists():
        return 0
    return sum(f.stat().st_size for f in CACHE.rglob("*.parquet"))


def vaciar(tabla: str | None = None) -> int:
    """Borra la caché y devuelve los bytes liberados.

    Es seguro por construcción: todo lo que hay aquí está en el archivo
    maestro y se rebaja solo cuando haga falta.
    """
    objetivo = CACHE / tabla if tabla else CACHE
    if not objetivo.exists():
        return 0
    liberados = sum(f.stat().st_size for f in objetivo.rglob("*.parquet"))
    for f in objetivo.rglob("*.parquet"):
        f.unlink()
    return liberados
