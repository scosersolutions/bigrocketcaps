"""Precio objetivo de consenso: el dato que faltaba, y que sí es objetivo.

Durante mucho tiempo la ficha no enseñó ningún precio objetivo. La razón que se
daba mezclaba dos cosas distintas, y solo una era cierta:

- **Cierta**: el sistema no se inventa una dirección. Midió que la sesión de
  resultados multiplica el movimiento pero que su signo es simétrico, y que la
  deriva posterior —E5, PEAD— está refutada. De ahí no sale ningún objetivo.
- **Falsa**: que no hubiera objetivo *ninguno*. Los analistas publican un
  consenso, con su mínimo y su máximo. Eso es un dato de terceros, medible y
  comprobable. No enseñarlo era una laguna de datos disfrazada de postura.

Este módulo trae ese dato. Sigue sin fabricarse uno propio.

## Lo que la fuente no dice

**No declara horizonte.** Ni el JSON ni la página lo publican, así que aquí no
se rellena: `horizonte` queda a NULL y la ficha dice que no viene. Suponer doce
meses porque «es lo habitual» sería exactamente el tipo de relleno que este
proyecto no hace.

## Lo que sí se puede añadir

Lo interesante no es repetir el objetivo, es situarlo. `delivery/ficha.py`
calcula cuánto pide ese objetivo sobre el precio de hoy y con qué frecuencia el
propio valor ha dado esa subida a cada plazo. Los dos puntos de vista se leen
juntos y ninguno se disfraza del otro.

Ojo con un traslado tentador que **no** se hace: `docs/research/analistas.md`
midió que el consenso de BENEFICIO se bate el 67,5 % de las veces y que ese
sesgo crece con la cobertura. Eso no dice nada de un objetivo de PRECIO, y
aplicarlo aquí sería inventar.
"""

from __future__ import annotations

import json
import time

import httpx
import polars as pl

from core.data.binance_dumps import DescargaError
from core.obs.logging import get_logger

log = get_logger(__name__)

URL = "https://api.nasdaq.com/api/analyst/{simbolo}/targetprice"
URL_RATINGS = "https://api.nasdaq.com/api/analyst/{simbolo}/ratings"
SOURCE = "nasdaq_analyst"

_CABECERAS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}

PAUSA_SEGUNDOS = 0.4

#: Columnas añadidas después de la primera versión. `CREATE TABLE IF NOT EXISTS`
#: no las agrega a una tabla que ya existe, y sin esto una base creada antes
#: fallaría al insertar sin decir por qué.
MIGRACIONES = (
    "ALTER TABLE objetivos ADD COLUMN IF NOT EXISTS postura VARCHAR",
    "ALTER TABLE objetivos ADD COLUMN IF NOT EXISTS desde VARCHAR",
    "ALTER TABLE objetivos ADD COLUMN IF NOT EXISTS serie VARCHAR",
)

DDL_OBJETIVOS = """
CREATE TABLE IF NOT EXISTS objetivos (
    activo     VARCHAR NOT NULL,
    consultado DATE    NOT NULL,
    objetivo   DOUBLE,
    minimo     DOUBLE,
    maximo     DOUBLE,
    compra     INTEGER,
    mantener   INTEGER,
    venta      INTEGER,
    casas      INTEGER,
    consenso   VARCHAR,
    horizonte  VARCHAR,
    postura    VARCHAR,
    desde      VARCHAR,
    serie      VARCHAR,
    source     VARCHAR NOT NULL,
    PRIMARY KEY (activo, consultado)
);
"""


def _mes(bruto: str | None) -> str | None:
    """`07/01/2026` -> `2026-07`. Nasdaq fecha estos puntos al día 1 del mes."""
    t = str(bruto or "").strip().split("/")
    if len(t) != 3 or not all(x.isdigit() for x in t):
        return None
    return f"{t[2]}-{int(t[0]):02d}"


def serie_consenso(bruto: dict) -> dict:
    """Desde cuándo dicen lo que dicen, y cómo se ha movido el objetivo.

    `historicalConsensus` trae trece puntos mensuales con el reparto de casas de
    ese mes, su postura, y en `y` el objetivo que había entonces. Que `y` es el
    objetivo —y no el precio— se comprueba en el último punto: coincide exacto
    con `consensusOverview.priceTarget` en todos los valores probados.

    Contesta lo que la ficha no contestaba: un «Buy» no dice nada si no se sabe
    si es de esta semana o lleva año y medio ahí.

    Del campo `latest` no se usa nada. Da un {high, avg, low} que en NVDA vale
    217,76 frente a los 325,23 del consenso general, y no hay forma de saber
    desde fuera qué mide esa diferencia. Un dato que no se entiende no se enseña.
    """
    h = ((bruto.get("data") or {}) or {}).get("historicalConsensus") or []
    pts = []
    for x in h:
        z = x.get("z") or {}
        m = _mes(z.get("date"))
        if not m:
            continue
        y = x.get("y")
        pts.append((m, float(y) if isinstance(y, (int, float)) else None, z.get("consensus")))
    if not pts:
        return {}
    # Hacia atrás mientras la postura sea la misma. El primer mes que difiere
    # corta: lo de antes ya era otra cosa aunque luego volviera a coincidir.
    i = len(pts) - 1
    while i > 0 and pts[i - 1][2] == pts[-1][2]:
        i -= 1
    return {
        "postura": pts[-1][2],
        "desde": pts[i][0],
        "serie": json.dumps([[m, None if o is None else round(o, 2)] for m, o, _ in pts[-13:]]),
    }


def parse(simbolo: str, bruto: dict, ratings: dict | None, dia) -> pl.DataFrame:
    """Respuesta de Nasdaq -> una fila. Sin consenso publicado devuelve vacío."""
    c = ((bruto.get("data") or {}) or {}).get("consensusOverview") or {}
    if c.get("priceTarget") is None:
        return pl.DataFrame(schema=_ESQUEMA)
    d = (ratings or {}).get("data") or {}
    hist = serie_consenso(bruto)
    return pl.DataFrame(
        [
            {
                "activo": simbolo.upper(),
                "consultado": dia,
                "objetivo": float(c["priceTarget"]),
                "minimo": _f(c.get("lowPriceTarget")),
                "maximo": _f(c.get("highPriceTarget")),
                "compra": int(c.get("buy") or 0),
                "mantener": int(c.get("hold") or 0),
                "venta": int(c.get("sell") or 0),
                "casas": len(d.get("brokerNames") or []) or None,
                "consenso": d.get("meanRatingType"),
                # La fuente no lo publica. Queda a NULL a propósito.
                "horizonte": None,
                "postura": hist.get("postura"),
                "desde": hist.get("desde"),
                "serie": hist.get("serie"),
                "source": SOURCE,
            }
        ],
        schema=_ESQUEMA,
    )


_ESQUEMA = {
    "activo": pl.Utf8, "consultado": pl.Date, "objetivo": pl.Float64,
    "minimo": pl.Float64, "maximo": pl.Float64, "compra": pl.Int32,
    "mantener": pl.Int32, "venta": pl.Int32, "casas": pl.Int32,
    "consenso": pl.Utf8, "horizonte": pl.Utf8, "postura": pl.Utf8,
    "desde": pl.Utf8, "serie": pl.Utf8, "source": pl.Utf8,
}


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _pedir(cliente: httpx.Client, url: str) -> dict | None:
    r = cliente.get(url, headers=_CABECERAS)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise DescargaError(f"{url} -> {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise DescargaError(f"{url} -> respuesta no es JSON") from e


def descargar(
    simbolos: list[str], dia, cliente: httpx.Client | None = None
) -> pl.DataFrame:
    """Un símbolo que falla no tumba al resto: se anota y se sigue.

    Son cientos de peticiones y basta con que una empresa esté deslistada para
    que el endpoint devuelva algo raro. Perder toda la pasada por eso sería
    peor que perder una fila.
    """
    propio = cliente is None
    cliente = cliente or httpx.Client(timeout=30, follow_redirects=True)
    trozos = []
    try:
        for i, s in enumerate(simbolos):
            try:
                bruto = _pedir(cliente, URL.format(simbolo=s))
                if bruto is None:
                    continue
                ratings = _pedir(cliente, URL_RATINGS.format(simbolo=s))
                fila = parse(s, bruto, ratings, dia)
                if not fila.is_empty():
                    trozos.append(fila)
            except (DescargaError, httpx.HTTPError) as e:
                log.warning("objetivo_fallido", simbolo=s, error=str(e))
            if i + 1 < len(simbolos):
                time.sleep(PAUSA_SEGUNDOS)
    finally:
        if propio:
            cliente.close()
    return pl.concat(trozos) if trozos else pl.DataFrame(schema=_ESQUEMA)
