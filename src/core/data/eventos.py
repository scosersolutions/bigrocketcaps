"""Calendario de eventos corporativos desde SEC EDGAR.

Cada presentación ante la SEC lleva `acceptanceDateTime` con hora exacta, y esa
hora decide **en qué sesión puede reaccionar el mercado**. Es el dato que hace
que este calendario sea utilizable en un backtest.

Verificado en Apple: los 8-K de resultados se aceptan sistemáticamente a las
20:30 UTC, es decir, las 16:30 de Nueva York, **media hora después del cierre**.
Un backtest que suponga reacción el mismo día está operando con información que
todavía no era pública cuando esa sesión cerró.

| Presentación | Hora ET | Sesión que puede reaccionar |
|---|---|---|
| 8-K de resultados | 16:30 | La **siguiente** |
| 10-Q antes de apertura | 06:01 | La de **ese día** |
| Durante la sesión | 9:30-16:00 | La de **ese día** |

La conversión usa el huso de Nueva York, no un desfase fijo: entre marzo y
noviembre el cierre son las 20:00 UTC y el resto del año las 21:00. Un offset
fijo se equivocaría en unas semanas al año, justo en las de transición.

Taxonomía de los 8-K por su campo `items`, que es la clasificación oficial de
la SEC y evita tener que inferir el tipo de evento con un modelo.
"""

from __future__ import annotations

import time
from datetime import date, datetime, time as hora_del_dia
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from core.data.errores import DescargaError
from core.data.edgar import cabeceras
from core.obs.logging import get_logger

log = get_logger(__name__)

BASE = "https://data.sec.gov/submissions"
SOURCE = "sec_submissions"
NUEVA_YORK = ZoneInfo("America/New_York")

APERTURA = hora_del_dia(9, 30)
CIERRE = hora_del_dia(16, 0)

PAUSA_SEGUNDOS = 0.12

#: Formularios que describen un hecho puntual con posible efecto en el precio.
FORMULARIOS = ("8-K", "10-Q", "10-K", "8-K/A")

#: Items de un 8-K, según la clasificación de la SEC. Solo se nombran los que
#: interesan; el resto se agrupa como "otro".
ITEMS: dict[str, str] = {
    "1.01": "acuerdo_material",
    "1.03": "concurso_acreedores",
    "2.01": "adquisicion_o_venta",
    "2.02": "resultados",
    "2.05": "reestructuracion",
    "2.06": "deterioro_activos",
    "3.01": "incumplimiento_cotizacion",
    "4.01": "cambio_auditor",
    "4.02": "reformulacion_cuentas",
    "5.02": "cambio_directivos",
    "5.07": "votacion_accionistas",
    "7.01": "comunicacion_reg_fd",
    "8.01": "otro_evento_material",
}

DDL_EVENTOS = """
CREATE TABLE IF NOT EXISTS eventos (
    activo       VARCHAR     NOT NULL,
    cik          VARCHAR     NOT NULL,
    accession    VARCHAR     NOT NULL,
    formulario   VARCHAR     NOT NULL,
    clase        VARCHAR     NOT NULL,
    items        VARCHAR,
    aceptado     TIMESTAMPTZ NOT NULL,
    presentado   DATE        NOT NULL,
    periodo      DATE,
    tras_cierre  BOOLEAN     NOT NULL,
    source       VARCHAR     NOT NULL,
    PRIMARY KEY (activo, accession)
);
"""


def clasificar_items(items: str | None, formulario: str) -> str:
    """Convierte el campo `items` en una clase de evento.

    Cuando un 8-K trae varios items se toma el primero reconocido: el orden de
    la SEC pone antes el hecho principal.
    """
    if formulario in ("10-Q", "10-K"):
        return "informe_periodico"
    if not items:
        return "otro"
    for bruto in items.split(","):
        clase = ITEMS.get(bruto.strip())
        if clase:
            return clase
    return "otro"


def tras_el_cierre(aceptado: datetime) -> bool:
    """Si la presentación llegó cuando el mercado ya había cerrado.

    Determina si la reacción posible es en la sesión siguiente. Se evalúa en
    hora de Nueva York para que el horario de verano no desplace el corte.
    """
    local = aceptado.astimezone(NUEVA_YORK)
    if local.weekday() >= 5:
        return True
    return local.time() >= CIERRE


def sesion_de_impacto(aceptado: datetime, sesiones: list[date]) -> date | None:
    """Primera sesión en la que el mercado puede reaccionar.

    `sesiones` son las fechas con cotización, ya ordenadas. Buscar en el
    calendario real y no sumar un día evita atribuir la reacción a un fin de
    semana o a un festivo, que es un error silencioso: la operación aparecería
    con precio de una sesión que no existió.
    """
    local = aceptado.astimezone(NUEVA_YORK)
    dia = local.date()
    if tras_el_cierre(aceptado):
        candidatas = [s for s in sesiones if s > dia]
    else:
        candidatas = [s for s in sesiones if s >= dia]
    return candidatas[0] if candidatas else None


def parse_submissions(bruto: dict, simbolo: str, cik: str) -> pl.DataFrame:
    """Respuesta de `submissions` -> eventos tipados."""
    presentaciones = (bruto.get("filings") or {}).get("recent") or {}
    formularios = presentaciones.get("form")
    if not formularios:
        raise DescargaError(f"{simbolo}: sin presentaciones")

    filas = []
    for i, formulario in enumerate(formularios):
        if formulario not in FORMULARIOS:
            continue
        aceptado_bruto = presentaciones["acceptanceDateTime"][i]
        if not aceptado_bruto:
            continue
        aceptado = datetime.fromisoformat(aceptado_bruto.replace("Z", "+00:00"))
        periodo = presentaciones["reportDate"][i] or None
        items = presentaciones.get("items", [None] * len(formularios))[i]
        filas.append(
            {
                "activo": simbolo,
                "cik": cik,
                "accession": presentaciones["accessionNumber"][i],
                "formulario": formulario,
                "clase": clasificar_items(items, formulario),
                "items": items or None,
                "aceptado": aceptado,
                "presentado": date.fromisoformat(presentaciones["filingDate"][i]),
                "periodo": date.fromisoformat(periodo) if periodo else None,
                "tras_cierre": tras_el_cierre(aceptado),
                "source": SOURCE,
            }
        )

    if not filas:
        raise DescargaError(f"{simbolo}: ninguna presentación de interés")

    return pl.DataFrame(
        filas,
        schema_overrides={"aceptado": pl.Datetime("us", "UTC")},
    ).unique(subset=["activo", "accession"], keep="first").sort("aceptado")


def descargar_eventos(
    simbolo: str, cik: str, user_agent: str, cliente: httpx.Client | None = None
) -> pl.DataFrame:
    propio = cliente is None
    cliente = cliente or httpx.Client(timeout=60, follow_redirects=True)
    try:
        r = cliente.get(f"{BASE}/CIK{cik}.json", headers=cabeceras(user_agent))
        if r.status_code == 404:
            raise DescargaError(f"{simbolo}: CIK {cik} sin presentaciones")
        r.raise_for_status()
        return parse_submissions(r.json(), simbolo, cik)
    finally:
        if propio:
            cliente.close()


def con_sesion_de_impacto(eventos: pl.DataFrame, sesiones: list[date]) -> pl.DataFrame:
    """Añade la sesión en la que cada evento puede afectar al precio."""
    if eventos.is_empty():
        return eventos.with_columns(pl.lit(None, dtype=pl.Date).alias("sesion"))
    ordenadas = sorted(sesiones)
    return eventos.with_columns(
        pl.Series(
            "sesion",
            [sesion_de_impacto(a, ordenadas) for a in eventos["aceptado"]],
            dtype=pl.Date,
        )
    )


def descargar_varios(
    simbolos_cik: dict[str, str], user_agent: str, *, pausa: float = PAUSA_SEGUNDOS
) -> dict[str, pl.DataFrame]:
    salida, fallos = {}, []
    with httpx.Client(timeout=60, follow_redirects=True) as cliente:
        for simbolo, cik in simbolos_cik.items():
            try:
                salida[simbolo] = descargar_eventos(simbolo, cik, user_agent, cliente)
            except (DescargaError, httpx.HTTPError) as e:
                fallos.append(f"{simbolo}: {type(e).__name__}")
            time.sleep(pausa)
    if fallos:
        log.warning("símbolos sin eventos", extra={"n": len(fallos), "detalle": fallos[:10]})
    return salida
