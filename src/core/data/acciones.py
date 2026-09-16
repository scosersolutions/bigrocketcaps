"""OHLCV diario de acciones US.

**Aviso sobre la fuente.** Se usa el endpoint público de gráficos de Yahoo, que
**no es una API oficial**: puede cambiar de formato o dejar de responder sin
previo aviso, y es el mismo motivo por el que se descartó `yfinance` para
producción en `docs/research/01-hallazgos.md`.

Se usa igualmente por una razón concreta: aquí solo hace falta **una descarga
histórica**, que se guarda en la base local y no vuelve a pedirse. El backtest
depende de DuckDB, no del endpoint. Si mañana deja de funcionar, lo que se
pierde es la capacidad de actualizar, no los datos ya cargados.

Para operar en vivo haría falta otra fuente. Esa decisión se toma si alguna
hipótesis sobrevive, no antes.

Verificado el 2026-09-02 con datos reales:

- `close` **ya viene ajustado por splits**: el 10:1 de NVDA del 2024-06-10 no
  produce ningún salto en la serie. Sin ese ajuste, cada split parecería un
  desplome del 90 % y generaría señales falsas.
- `adjclose` añade el ajuste por dividendos. **No se usa para operar**: al
  comprar una acción se paga el precio real, y el dividendo se cobra aparte.
  Mezclarlos daría precios de entrada que nadie pudo pagar.
- 25 símbolos consecutivos sin un solo fallo, a 0,61 s por símbolo.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
import polars as pl

from core.data.binance_dumps import DescargaError
from core.obs.logging import get_logger
from core.store.schema import Mercado, Timeframe

log = get_logger(__name__)

BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
SOURCE = "yahoo_chart"

#: Espera entre peticiones. Medido: 0,61 s/símbolo sin fallos en 25 seguidos.
#: Se mantiene deliberadamente holgado: el histórico se descarga una vez y no
#: compensa arriesgar un bloqueo por terminar unos minutos antes.
PAUSA_SEGUNDOS = 0.35

_CABECERAS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def normalizar(simbolo: str) -> str:
    """Convierte el ticker al formato de Yahoo.

    Las clases de acción se escriben con punto en los índices (BRK.B, BF.B) y
    con guion en Yahoo (BRK-B, BF-B). Sin esta conversión, Berkshire Hathaway
    aparecía como "sin datos" en el recuento de cobertura, inflando el sesgo de
    supervivencia aparente con empresas que sí existen.
    """
    return simbolo.replace(".", "-").upper()


def _pedir(
    simbolo: str,
    rango: str,
    cliente: httpx.Client,
    desde: datetime | None = None,
    hasta: datetime | None = None,
) -> dict:
    # `range` y `period1/period2` son excluyentes. Con fechas explícitas se usa
    # el segundo, que es el único que devuelve históricos largos: verificado
    # que `range=max` devuelve solo 169 velas de AAPL, mientras que
    # period1=2009 devuelve 4.442. El valor "max" es engañoso.
    if desde is not None:
        params = {
            "period1": int(desde.timestamp()),
            "period2": int((hasta or datetime.now(UTC)).timestamp()),
        }
    else:
        params = {"range": rango}
    params |= {"interval": "1d", "events": "div,split"}

    r = cliente.get(f"{BASE}/{normalizar(simbolo)}", params=params, headers=_CABECERAS)
    if r.status_code == 404:
        raise DescargaError(f"símbolo desconocido: {simbolo}")
    if r.status_code == 429:
        raise DescargaError(f"rate limit alcanzado en {simbolo}")
    r.raise_for_status()

    cuerpo = r.json().get("chart") or {}
    if cuerpo.get("error"):
        raise DescargaError(f"{simbolo}: {cuerpo['error']}")
    resultados = cuerpo.get("result") or []
    if not resultados:
        raise DescargaError(f"{simbolo}: respuesta sin datos")
    return resultados[0]


def parse_chart(bruto: dict, simbolo: str) -> pl.DataFrame:
    """Respuesta del endpoint -> esquema OHLCV interno."""
    marcas = bruto.get("timestamp") or []
    if not marcas:
        raise DescargaError(f"{simbolo}: sin marcas temporales")

    cotiza = (bruto.get("indicators", {}).get("quote") or [{}])[0]
    columnas = {c: cotiza.get(c) for c in ("open", "high", "low", "close", "volume")}
    if any(v is None for v in columnas.values()):
        raise DescargaError(f"{simbolo}: faltan columnas OHLCV")

    df = pl.DataFrame(
        {
            "ts": [datetime.fromtimestamp(t, UTC) for t in marcas],
            **{c: columnas[c] for c in ("open", "high", "low", "close")},
            "volume": [float(v) if v is not None else None for v in columnas["volume"]],
        },
        schema_overrides={"ts": pl.Datetime("us", "UTC")},
    )

    # Las sesiones sin cierre son festivos o huecos del proveedor: se omiten en
    # vez de rellenarse, para que el validador las cuente como lo que son.
    df = df.drop_nulls(["open", "high", "low", "close"])

    # Yahoo publica alguna vela con OHLC imposible. Caso real verificado: APH
    # el 2023-06-05 trae open=38,81 por encima de high=38,665, una diferencia
    # de 0,145 que no es redondeo sino un dato mal grabado. Es rarísimo (1 de
    # 1.222 velas) pero basta para que el validador rechace el activo entero.
    #
    # Se descartan aquí, en el adaptador de esta fuente, en lugar de relajar el
    # validador: el dato está mal de verdad y el resto del sistema debe seguir
    # exigiendo coherencia.
    antes = df.height
    df = df.filter(
        (pl.col("high") >= pl.col("low"))
        & (pl.col("high") >= pl.col("open"))
        & (pl.col("high") >= pl.col("close"))
        & (pl.col("low") <= pl.col("open"))
        & (pl.col("low") <= pl.col("close"))
    )
    if df.height < antes:
        log.warning(
            "velas con OHLC imposible descartadas",
            extra={"activo": simbolo, "descartadas": antes - df.height, "total": antes},
        )

    df = _tramo_vigente(df, simbolo)

    return (
        df.with_columns(
            # El timestamp marca la apertura de sesión en hora de Nueva York.
            # Se normaliza a medianoche UTC para que la clave sea la fecha de
            # negociación y no la hora, que cambia con el horario de verano.
            pl.col("ts").dt.truncate("1d").alias("ts"),
            pl.lit(Mercado.STOCK_US.value).alias("mercado"),
            pl.lit(simbolo).alias("activo"),
            pl.lit(Timeframe.D1.value).alias("timeframe"),
            pl.lit(None, dtype=pl.Int64).alias("trades"),
            pl.lit(SOURCE).alias("source"),
            pl.col("volume").fill_null(0.0),
        )
        .unique(subset=["ts"], keep="last")
        .select(
            "mercado", "activo", "timeframe", "ts",
            "open", "high", "low", "close", "volume", "trades", "source",
        )
        .sort("ts")
    )


#: Salto diario a partir del cual la serie no es de la misma empresa. Una
#: acción del S&P 500 no multiplica por cuatro ni pierde tres cuartas partes
#: en una sesión; un salto así significa que el ticker cambió de dueño.
SALTO_IMPOSIBLE = 4.0


def _tramo_vigente(df: pl.DataFrame, simbolo: str) -> pl.DataFrame:
    """Se queda con el tramo posterior al último salto imposible.

    Los tickers se reutilizan: uno deslistado se reasigna años después a otra
    empresa, y la fuente devuelve las dos series concatenadas como si fueran
    una. Caso real verificado: CBE pasa de 0,005 a 170,00 dólares de un día
    para otro (+3.399.900 %), porque une Cooper Industries, deslistada en 2012,
    con la empresa que heredó el ticker. PTV llega a 1.300.000 dólares.

    Sin este corte, esos saltos entran en el análisis como rentabilidades
    mensuales de miles por ciento y contaminan cualquier media: en el estudio
    de momentum, el decil peor aparecía con un +213 % mensual.

    El validador no puede detectarlo porque cada vela es coherente consigo
    misma; la corrupción está en la unión de dos series distintas.
    """
    if df.height < 2:
        return df

    cierres = df["close"].to_list()
    ultimo_corte = 0
    for i in range(1, len(cierres)):
        previo, actual = cierres[i - 1], cierres[i]
        if previo and actual:
            razon = actual / previo
            if razon >= SALTO_IMPOSIBLE or razon <= 1 / SALTO_IMPOSIBLE:
                ultimo_corte = i

    if ultimo_corte:
        log.warning(
            "serie partida por salto imposible: el ticker cambió de empresa",
            extra={"activo": simbolo, "velas_descartadas": ultimo_corte,
                   "de": df["ts"][0], "a": df["ts"][ultimo_corte]},
        )
        return df.slice(ultimo_corte)
    return df


def descargar(
    simbolo: str,
    *,
    rango: str = "5y",
    cliente: httpx.Client | None = None,
    desde: datetime | None = None,
    hasta: datetime | None = None,
) -> pl.DataFrame:
    """Descarga el histórico. Con `desde` se usan fechas explícitas.

    Para históricos largos hay que pasar `desde`: los valores de `range` topan
    mucho antes de lo que su nombre sugiere.
    """
    propio = cliente is None
    cliente = cliente or httpx.Client(timeout=45, follow_redirects=True)
    try:
        return parse_chart(_pedir(simbolo, rango, cliente, desde, hasta), simbolo)
    finally:
        if propio:
            cliente.close()


def descargar_varios(
    simbolos: list[str], *, rango: str = "5y", pausa: float = PAUSA_SEGUNDOS
) -> dict[str, pl.DataFrame]:
    """Descarga secuencial con pausa. Los fallos se registran y no abortan.

    Deliberadamente secuencial: paralelizar contra un endpoint no oficial es la
    forma más rápida de que deje de responder.
    """
    salida, fallos = {}, []
    with httpx.Client(timeout=45, follow_redirects=True) as cliente:
        for simbolo in simbolos:
            try:
                salida[simbolo] = descargar(simbolo, rango=rango, cliente=cliente)
            except (DescargaError, httpx.HTTPError) as e:
                fallos.append(f"{simbolo}: {type(e).__name__}")
            time.sleep(pausa)

    if fallos:
        log.warning("símbolos sin datos", extra={"n": len(fallos), "detalle": fallos[:10]})
    return salida
