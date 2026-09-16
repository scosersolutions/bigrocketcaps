"""Fundamentales desde SEC EDGAR: la fuente primaria, gratuita y sin clave.

Solo exige una cabecera `User-Agent` identificativa y respetar 10 peticiones por
segundo. No hay tramos de pago ni límites de histórico: es el mejor dato de todo
el proyecto y no cuesta nada.

**La razón por la que este módulo existe y no se usa un agregador:** cada dato
XBRL trae dos fechas distintas, y confundirlas arruina cualquier backtest.

| Campo | Qué es |
|---|---|
| `end` | Fin del periodo contable. Un 10-K de 2024 tiene `end` en diciembre de 2024 |
| `filed` | **Fecha en que se publicó de verdad**, semanas o meses después |

Un backtest que use `end` está operando con cifras que nadie conocía todavía.
Es la forma más pura de look-ahead que existe en datos fundamentales, y muchos
proveedores comerciales solo exponen `end`.

**Aquí se usa siempre `filed`.** Un test lo comprueba.
"""

from __future__ import annotations

import time
from datetime import date, datetime

import httpx
import polars as pl

from core.data.errores import DescargaError
from core.obs.logging import get_logger

log = get_logger(__name__)

BASE = "https://data.sec.gov"
SOURCE = "sec_edgar"

#: Límite publicado por la SEC: 10 peticiones por segundo.
PAUSA_SEGUNDOS = 0.12

#: Conceptos XBRL de interés. Se dan varias alternativas por concepto porque
#: las empresas no usan la misma etiqueta: los ingresos aparecen como
#: `Revenues` en unas y como `RevenueFromContractWithCustomer...` en otras.
CONCEPTOS: dict[str, tuple[str, ...]] = {
    "ingresos": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "beneficio_neto": ("NetIncomeLoss",),
    "activos": ("Assets",),
    "pasivos": ("Liabilities",),
    "patrimonio": ("StockholdersEquity",),
    "flujo_operativo": ("NetCashProvidedByUsedInOperatingActivities",),
    "acciones": (
        "CommonStockSharesOutstanding",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
    ),
}

DDL_FUNDAMENTALES = """
CREATE TABLE IF NOT EXISTS fundamentales (
    activo    VARCHAR NOT NULL,
    cik       VARCHAR NOT NULL,
    concepto  VARCHAR NOT NULL,
    etiqueta  VARCHAR NOT NULL,
    fin       DATE    NOT NULL,
    publicado DATE    NOT NULL,
    formulario VARCHAR,
    valor     DOUBLE,
    unidad    VARCHAR,
    source    VARCHAR NOT NULL,
    PRIMARY KEY (activo, concepto, fin, publicado)
);
"""


def cabeceras(user_agent: str) -> dict[str, str]:
    if not user_agent or "@" not in user_agent:
        raise DescargaError(
            "la SEC exige un User-Agent identificativo con correo de contacto"
        )
    return {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}


def mapa_tickers(user_agent: str, cliente: httpx.Client | None = None) -> dict[str, str]:
    """Ticker -> CIK con ceros a la izquierda, como lo exige la API."""
    propio = cliente is None
    cliente = cliente or httpx.Client(timeout=60, follow_redirects=True)
    try:
        r = cliente.get(f"https://www.sec.gov/files/company_tickers.json",
                        headers=cabeceras(user_agent))
        r.raise_for_status()
        return {
            fila["ticker"].upper(): f"{int(fila['cik_str']):010d}"
            for fila in r.json().values()
        }
    finally:
        if propio:
            cliente.close()


def nombres_tickers(user_agent: str, cliente: httpx.Client | None = None) -> dict[str, str]:
    """Ticker -> nombre de la empresa, del mismo fichero que `mapa_tickers`.

    La base no guarda el nombre en ninguna tabla: `insiders` tiene el del
    directivo y `objetivos` ninguno. El informe por correo lo necesita, porque
    un símbolo suelto no dice de qué empresa se habla, y este fichero ya se
    descarga para resolver los CIK. Es la misma petición, otro campo.
    """
    propio = cliente is None
    cliente = cliente or httpx.Client(timeout=60, follow_redirects=True)
    try:
        r = cliente.get("https://www.sec.gov/files/company_tickers.json",
                        headers=cabeceras(user_agent))
        r.raise_for_status()
        return {fila["ticker"].upper(): fila["title"] for fila in r.json().values()}
    finally:
        if propio:
            cliente.close()


def parse_companyfacts(bruto: dict, simbolo: str, cik: str) -> pl.DataFrame:
    """companyfacts XBRL -> filas tipadas, fechadas por `filed`."""
    hechos = (bruto.get("facts") or {}).get("us-gaap") or {}
    if not hechos:
        raise DescargaError(f"{simbolo}: sin hechos us-gaap")

    # Se recorren TODAS las etiquetas de cada concepto, no solo la primera que
    # exista. Una empresa puede cambiar de etiqueta a mitad de su historia:
    # NVIDIA declaró `RevenueFromContractWithCustomerExcludingAssessedTax` de
    # 2017 a 2022 y después volvió a `Revenues`, donde tiene 280 valores hasta
    # 2026. Quedarse con la primera disponible dejaba sus ingresos congelados en
    # 2022 sin avisar, que es la clase de cifra plausible y falsa que más caro
    # sale: nadie mira dos veces un número que parece razonable.
    filas = []
    for concepto, etiquetas in CONCEPTOS.items():
        for orden, etiqueta in enumerate(etiquetas):
            entrada = hechos.get(etiqueta)
            if not entrada:
                continue
            for unidad, valores in (entrada.get("units") or {}).items():
                for v in valores:
                    if not v.get("filed") or v.get("val") is None:
                        continue
                    filas.append(
                        {
                            "activo": simbolo,
                            "cik": cik,
                            "concepto": concepto,
                            "etiqueta": etiqueta,
                            "fin": date.fromisoformat(v["end"]),
                            # `filed` y no `end`: es cuando el dato se hizo
                            # público y, por tanto, utilizable.
                            "publicado": date.fromisoformat(v["filed"]),
                            "formulario": v.get("form"),
                            "valor": float(v["val"]),
                            "unidad": unidad,
                            "source": SOURCE,
                            # Posición en la tupla de alternativas. Cuando dos
                            # etiquetas cubren el mismo periodo manda la de
                            # arriba, que es la preferida; sin esto la elección
                            # dependería del orden en que llegaran las filas.
                            "_orden": orden,
                        }
                    )

    if not filas:
        raise DescargaError(f"{simbolo}: ningún concepto de interés disponible")

    return (
        pl.DataFrame(filas)
        .sort(["activo", "concepto", "fin", "publicado", "_orden"])
        .unique(subset=["activo", "concepto", "fin", "publicado"], keep="first")
        .drop("_orden")
        .sort(["concepto", "publicado"])
    )


def descargar_fundamentales(
    simbolo: str, cik: str, user_agent: str, cliente: httpx.Client | None = None
) -> pl.DataFrame:
    propio = cliente is None
    cliente = cliente or httpx.Client(timeout=90, follow_redirects=True)
    try:
        r = cliente.get(
            f"{BASE}/api/xbrl/companyfacts/CIK{cik}.json", headers=cabeceras(user_agent)
        )
        if r.status_code == 404:
            raise DescargaError(f"{simbolo}: CIK {cik} sin companyfacts")
        r.raise_for_status()
        return parse_companyfacts(r.json(), simbolo, cik)
    finally:
        if propio:
            cliente.close()


def disponible_en(df: pl.DataFrame, fecha: date | datetime) -> pl.DataFrame:
    """Filtra lo que ya estaba publicado en `fecha`.

    Es la función que hace utilizables estos datos en un backtest: en cualquier
    instante solo puede verse lo que la SEC ya había recibido.
    """
    corte = fecha.date() if isinstance(fecha, datetime) else fecha
    return df.filter(pl.col("publicado") <= corte)


def ultimo_valor(df: pl.DataFrame, concepto: str, fecha: date) -> float | None:
    """Último valor conocido de un concepto en una fecha dada."""
    vigentes = (
        disponible_en(df, fecha)
        .filter(pl.col("concepto") == concepto)
        .sort(["publicado", "fin"])
    )
    return None if vigentes.is_empty() else float(vigentes["valor"][-1])


def descargar_varios(
    simbolos: list[str], user_agent: str, *, pausa: float = PAUSA_SEGUNDOS
) -> dict[str, pl.DataFrame]:
    salida, fallos = {}, []
    with httpx.Client(timeout=90, follow_redirects=True) as cliente:
        tickers = mapa_tickers(user_agent, cliente)
        for simbolo in simbolos:
            cik = tickers.get(simbolo.upper())
            if cik is None:
                fallos.append(f"{simbolo}: sin CIK")
                continue
            try:
                salida[simbolo] = descargar_fundamentales(simbolo, cik, user_agent, cliente)
            except (DescargaError, httpx.HTTPError) as e:
                fallos.append(f"{simbolo}: {type(e).__name__}")
            time.sleep(pausa)

    if fallos:
        log.warning("símbolos sin fundamentales", extra={"n": len(fallos), "detalle": fallos[:10]})
    return salida
