"""Acceso a DuckDB. Valida antes de escribir: sin ts con zona horaria, no entra."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from types import TracebackType
from typing import Self

import duckdb
import polars as pl

from core.store.schema import DDL, COLUMNAS_OHLCV


class ValidacionError(ValueError):
    """El dato no cumple el contrato del esquema."""


def _validar_ohlcv(df: pl.DataFrame) -> pl.DataFrame:
    faltan = set(COLUMNAS_OHLCV) - set(df.columns)
    if faltan:
        raise ValidacionError(f"faltan columnas: {sorted(faltan)}")

    dtype = df.schema["ts"]
    if not isinstance(dtype, pl.Datetime):
        raise ValidacionError(f"ts debe ser Datetime, es {dtype}")
    if dtype.time_zone is None:
        raise ValidacionError("ts sin zona horaria: el esquema exige UTC explícito")
    if dtype.time_zone != "UTC":
        df = df.with_columns(pl.col("ts").dt.convert_time_zone("UTC"))

    if df.is_empty():
        return df.select(COLUMNAS_OHLCV)

    clave = ["mercado", "activo", "timeframe", "ts"]
    if df.select(clave).is_duplicated().any():
        raise ValidacionError("filas duplicadas para (mercado, activo, timeframe, ts)")

    invalidas = df.filter(
        (pl.col("high") < pl.col("low"))
        | (pl.col("high") < pl.col("open"))
        | (pl.col("high") < pl.col("close"))
        | (pl.col("low") > pl.col("open"))
        | (pl.col("low") > pl.col("close"))
        | (pl.col("volume") < 0)
    )
    if not invalidas.is_empty():
        raise ValidacionError(f"{invalidas.height} velas con OHLC incoherente")

    return df.select(COLUMNAS_OHLCV)


class Store:
    """Acceso a la base. `solo_lectura` permite varios procesos a la vez.

    DuckDB admite un único escritor pero muchos lectores simultáneos. Sin esa
    distinción, un test que solo consulta bloquea la base entera y falla en
    cuanto otra sesión está descargando o corriendo un barrido —que es
    exactamente lo que pasa cuando se trabaja en dos sitios a la vez—. El
    fallo, además, no se parece en nada a su causa: sale un IOException de
    fichero en uso en mitad de un test de indicadores.

    En solo lectura no se aplica el DDL: no hay nada que crear, y `CREATE TABLE
    IF NOT EXISTS` sobre una conexión de lectura falla aunque las tablas ya
    existan.
    """

    def __init__(self, ruta: Path | str, *, solo_lectura: bool = False) -> None:
        self.ruta = Path(ruta)
        self.solo_lectura = solo_lectura
        if solo_lectura:
            if not self.ruta.exists():
                raise FileNotFoundError(
                    f"{self.ruta} no existe y se pidió en solo lectura: en ese modo "
                    "la base no se crea.")
            self.con = duckdb.connect(str(self.ruta), read_only=True)
            self.con.execute("SET TimeZone='UTC'")
            return
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.ruta))
        self.con.execute("SET TimeZone='UTC'")
        self.con.execute(DDL)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.con.close()

    def escribir_ohlcv(self, df: pl.DataFrame) -> int:
        """Inserta velas. Idempotente: repetir la misma carga no duplica filas."""
        datos = _validar_ohlcv(df)
        if datos.is_empty():
            return 0
        self.con.register("_lote_ohlcv", datos.to_arrow())
        try:
            self.con.execute("INSERT OR IGNORE INTO ohlcv SELECT * FROM _lote_ohlcv")
        finally:
            self.con.unregister("_lote_ohlcv")
        return datos.height

    def leer_ohlcv(
        self,
        mercado: str,
        activo: str,
        timeframe: str,
        desde: object | None = None,
        hasta: object | None = None,
    ) -> pl.DataFrame:
        sql = """
            SELECT * FROM ohlcv
            WHERE mercado = ? AND activo = ? AND timeframe = ?
        """
        params: list[object] = [mercado, activo, timeframe]
        if desde is not None:
            sql += " AND ts >= ?"
            params.append(desde)
        if hasta is not None:
            sql += " AND ts <= ?"
            params.append(hasta)
        sql += " ORDER BY ts"
        return self.con.execute(sql, params).pl()

    def escribir_funding(self, df: pl.DataFrame) -> int:
        """Inserta tasas de funding. Idempotente por (activo, ts)."""
        faltan = {"activo", "ts", "rate", "source"} - set(df.columns)
        if faltan:
            raise ValidacionError(f"faltan columnas: {sorted(faltan)}")
        dtype = df.schema["ts"]
        if not isinstance(dtype, pl.Datetime) or dtype.time_zone is None:
            raise ValidacionError("ts sin zona horaria: el esquema exige UTC explícito")
        if dtype.time_zone != "UTC":
            df = df.with_columns(pl.col("ts").dt.convert_time_zone("UTC"))

        datos = df.select("activo", "ts", "rate", "source")
        if datos.is_empty():
            return 0
        if datos.select(["activo", "ts"]).is_duplicated().any():
            raise ValidacionError("filas duplicadas para (activo, ts)")

        self.con.register("_lote_funding", datos.to_arrow())
        try:
            self.con.execute("INSERT OR IGNORE INTO funding SELECT * FROM _lote_funding")
        finally:
            self.con.unregister("_lote_funding")
        return datos.height

    def leer_funding(self, activo: str) -> pl.DataFrame:
        """Devuelve un DataFrame vacío con el esquema correcto si no hay datos.

        `pl.from_arrow()` no puede inferir el esquema de un resultado sin filas
        y lanzaba una excepción. Solo apareció al ampliar el universo a 110
        activos, donde algunos no tienen funding.
        """
        return self.con.execute(
            "SELECT * FROM funding WHERE activo = ? ORDER BY ts", [activo]
        ).pl()

    def escribir_metrics(self, df: pl.DataFrame) -> int:
        """Inserta métricas de posicionamiento. Idempotente por (activo, ts)."""
        obligatorias = {"activo", "ts", "source"}
        if obligatorias - set(df.columns):
            raise ValidacionError(f"faltan columnas: {sorted(obligatorias - set(df.columns))}")
        dtype = df.schema["ts"]
        if not isinstance(dtype, pl.Datetime) or dtype.time_zone is None:
            raise ValidacionError("ts sin zona horaria: el esquema exige UTC explícito")
        if df.is_empty():
            return 0
        if df.select(["activo", "ts"]).is_duplicated().any():
            raise ValidacionError("filas duplicadas para (activo, ts)")

        columnas = [
            "activo", "ts", "open_interest", "open_interest_usd",
            "top_ls_cuentas", "top_ls_posiciones", "ls_cuentas",
            "taker_ls_volumen", "source",
        ]
        self.con.register("_lote_metrics", df.select(columnas).to_arrow())
        try:
            self.con.execute("INSERT OR IGNORE INTO metrics SELECT * FROM _lote_metrics")
        finally:
            self.con.unregister("_lote_metrics")
        return df.height

    def escribir_book_depth(self, df: pl.DataFrame) -> int:
        """Inserta instantaneas del libro. Idempotente por (activo, ts)."""
        obligatorias = {"activo", "ts", "source"}
        if obligatorias - set(df.columns):
            raise ValidacionError(f"faltan columnas: {sorted(obligatorias - set(df.columns))}")
        dtype = df.schema["ts"]
        if not isinstance(dtype, pl.Datetime) or dtype.time_zone is None:
            raise ValidacionError("ts sin zona horaria: el esquema exige UTC explicito")
        if df.is_empty():
            return 0
        # El dump repite alguna instantanea suelta. Se deduplica aqui en vez de
        # fallar: un duplicado exacto no es una inconsistencia del dato.
        df = df.unique(subset=["activo", "ts"], keep="first").sort("ts")

        columnas = ["activo", "ts"] + [
            f"{lado}_{b}" for lado in ("bid", "ask") for b in (1, 2, 3, 4, 5)
        ] + ["source"]
        self.con.register("_lote_book", df.select(columnas).to_arrow())
        try:
            self.con.execute(
                "INSERT OR IGNORE INTO book_depth SELECT * FROM _lote_book")
        finally:
            self.con.unregister("_lote_book")
        return df.height

    def leer_book_depth(self, activo: str, desde=None, hasta=None) -> pl.DataFrame:
        sql = "SELECT * FROM book_depth WHERE activo = ?"
        params: list[object] = [activo]
        if desde is not None:
            sql += " AND ts >= ?"
            params.append(desde)
        if hasta is not None:
            sql += " AND ts <= ?"
            params.append(hasta)
        return self.con.execute(sql + " ORDER BY ts", params).pl()

    def leer_metrics(self, activo: str) -> pl.DataFrame:
        return self.con.execute(
            "SELECT * FROM metrics WHERE activo = ? ORDER BY ts", [activo]
        ).pl()

    def escribir_fundamentales(self, df: pl.DataFrame) -> int:
        obligatorias = {"activo", "concepto", "fin", "publicado", "valor"}
        if obligatorias - set(df.columns):
            raise ValidacionError(
                f"faltan columnas: {sorted(obligatorias - set(df.columns))}"
            )
        if df.is_empty():
            return 0
        columnas = ["activo", "cik", "concepto", "etiqueta", "fin", "publicado",
                    "formulario", "valor", "unidad", "source"]
        self.con.register("_lote_fund", df.select(columnas).to_arrow())
        try:
            self.con.execute("INSERT OR IGNORE INTO fundamentales SELECT * FROM _lote_fund")
        finally:
            self.con.unregister("_lote_fund")
        return df.height

    def leer_fundamentales(self, activo: str) -> pl.DataFrame:
        return self.con.execute(
            "SELECT * FROM fundamentales WHERE activo = ? ORDER BY publicado", [activo]
        ).pl()

    def escribir_eventos(self, df: pl.DataFrame) -> int:
        if df.is_empty():
            return 0
        columnas = ["activo", "cik", "accession", "formulario", "clase", "items",
                    "aceptado", "presentado", "periodo", "tras_cierre", "source"]
        faltan = set(columnas) - set(df.columns)
        if faltan:
            raise ValidacionError(f"faltan columnas: {sorted(faltan)}")
        self.con.register("_lote_ev", df.select(columnas).to_arrow())
        try:
            self.con.execute("INSERT OR IGNORE INTO eventos SELECT * FROM _lote_ev")
        finally:
            self.con.unregister("_lote_ev")
        return df.height

    def leer_eventos(self, activo: str | None = None, clase: str | None = None) -> pl.DataFrame:
        sql, params = "SELECT * FROM eventos WHERE 1=1", []
        if activo:
            sql += " AND activo = ?"; params.append(activo)
        if clase:
            sql += " AND clase = ?"; params.append(clase)
        return self.con.execute(sql + " ORDER BY aceptado", params).pl()

    def escribir_insiders(self, df: pl.DataFrame) -> int:
        if df.is_empty():
            return 0
        columnas = ["activo", "cik_empresa", "accession", "linea", "cik_insider",
                    "relacion", "titulo", "presentado", "transaccion", "desfase",
                    "acciones", "precio", "valor", "source"]
        faltan = set(columnas) - set(df.columns)
        if faltan:
            raise ValidacionError(f"faltan columnas: {sorted(faltan)}")
        self.con.register("_lote_in", df.select(columnas).to_arrow())
        try:
            self.con.execute("INSERT OR IGNORE INTO insiders SELECT * FROM _lote_in")
        finally:
            self.con.unregister("_lote_in")
        return df.height

    def leer_insiders(self, activo: str | None = None) -> pl.DataFrame:
        sql, params = "SELECT * FROM insiders WHERE 1=1", []
        if activo:
            sql += " AND activo = ?"; params.append(activo)
        return self.con.execute(sql + " ORDER BY presentado", params).pl()

    def contar_ohlcv(self, mercado: str, activo: str, timeframe: str) -> int:
        fila = self.con.execute(
            "SELECT count(*) FROM ohlcv WHERE mercado = ? AND activo = ? AND timeframe = ?",
            [mercado, activo, timeframe],
        ).fetchone()
        return int(fila[0]) if fila else 0

    def rango_ohlcv(self, mercado: str, activo: str, timeframe: str) -> tuple[object, object] | None:
        fila = self.con.execute(
            "SELECT min(ts), max(ts) FROM ohlcv WHERE mercado = ? AND activo = ? AND timeframe = ?",
            [mercado, activo, timeframe],
        ).fetchone()
        if not fila or fila[0] is None:
            return None
        return fila[0].astimezone(UTC), fila[1].astimezone(UTC)
