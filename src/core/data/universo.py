"""Universo point-in-time del S&P 500.

Responde a una pregunta que casi ningún backtest se hace: **qué empresas
estaban en el índice en la fecha que se está simulando**, no cuáles están hoy.

Sin esto, un backtest de acciones solo opera las que sobrevivieron hasta el
presente, que es el sesgo de supervivencia en su forma más cara: las empresas
que quebraron, fueron absorbidas o cayeron del índice desaparecen de la muestra
justo por haber ido mal. En el experimento de cripto este sesgo no se pudo
eliminar y quedó declarado; aquí sí se puede.

**Fuente:** `fja05680/sp500` en GitHub, licencia MIT. Publica la composición
completa del índice para cada fecha de cambio desde 1996, incluidos tickers que
ya no existen (AAMRQ, la antigua American Airlines en concurso, entre otros).

**El sesgo residual sí se mide.** Que el universo incluya una empresa no
garantiza que haya precios para ella: los proveedores gratuitos suelen no
conservar los deslistados. `cobertura()` cuantifica esa diferencia, para poder
declarar la magnitud del sesgo en vez de suponerla.
"""

from __future__ import annotations

import urllib.parse
from datetime import date
from pathlib import Path

import httpx
import polars as pl

from core.data.binance_dumps import DescargaError
from core.obs.logging import get_logger

log = get_logger(__name__)

FICHERO = "S&P 500 Historical Components & Changes (Updated).csv"
URL = "https://raw.githubusercontent.com/fja05680/sp500/master/" + urllib.parse.quote(FICHERO)


def descargar_historico(cache_dir: Path | None = None) -> pl.DataFrame:
    """Composición del índice por fecha. Columnas: `fecha`, `tickers`."""
    destino = None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        destino = cache_dir / "sp500_historico.csv"
        if destino.exists():
            return _parse(destino.read_text(encoding="utf-8"))

    r = httpx.get(URL, timeout=120, follow_redirects=True)
    r.raise_for_status()
    if destino is not None:
        destino.write_text(r.text, encoding="utf-8")
    return _parse(r.text)


def _parse(csv_texto: str) -> pl.DataFrame:
    df = pl.read_csv(csv_texto.encode("utf-8"))
    faltan = {"date", "tickers"} - set(df.columns)
    if faltan:
        raise DescargaError(f"formato inesperado, faltan: {sorted(faltan)}")
    return (
        df.with_columns(
            pl.col("date").str.to_date("%Y-%m-%d").alias("fecha"),
            pl.col("tickers").str.split(",").alias("tickers"),
        )
        .select("fecha", "tickers")
        .sort("fecha")
    )


def constituyentes(historico: pl.DataFrame, fecha: date) -> list[str]:
    """Tickers del índice en `fecha`.

    Toma la composición vigente, es decir, la del último cambio anterior o
    igual a esa fecha. Nunca una posterior: eso sería mirar al futuro.
    """
    vigente = historico.filter(pl.col("fecha") <= fecha)
    if vigente.is_empty():
        return []
    return sorted(vigente["tickers"][-1])


def universo_del_periodo(
    historico: pl.DataFrame, desde: date, hasta: date
) -> list[str]:
    """Todo ticker que perteneció al índice en algún momento del periodo.

    Es el universo que un backtest debe considerar: incluye a los que entraron
    y salieron por el camino, no solo a los que aguantaron hasta el final.
    """
    tramo = historico.filter(
        (pl.col("fecha") >= desde) & (pl.col("fecha") <= hasta)
    )
    if tramo.is_empty():
        return constituyentes(historico, hasta)

    vistos: set[str] = set(constituyentes(historico, desde))
    for lista in tramo["tickers"]:
        vistos.update(lista)
    return sorted(vistos)


def salidas_del_periodo(
    historico: pl.DataFrame, desde: date, hasta: date
) -> list[str]:
    """Tickers que estaban al principio y ya no al final.

    Son exactamente los que un universo construido con la lista de hoy
    perdería, y por tanto la parte del sesgo que se puede nombrar.
    """
    inicio = set(constituyentes(historico, desde))
    final = set(constituyentes(historico, hasta))
    return sorted(inicio - final)


def cobertura(universo: list[str], con_datos: list[str]) -> dict:
    """Cuánto del universo real se puede simular de verdad.

    La diferencia es sesgo de supervivencia residual: se declara con un número
    en vez de suponer que no existe.
    """
    total = set(universo)
    disponibles = total & set(con_datos)
    ausentes = sorted(total - disponibles)
    return {
        "universo": len(total),
        "con_datos": len(disponibles),
        "sin_datos": len(ausentes),
        "cobertura": len(disponibles) / len(total) if total else 0.0,
        "ejemplos_ausentes": ausentes[:15],
    }
