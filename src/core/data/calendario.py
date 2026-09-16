"""Calendario de resultados de Nasdaq: fechas futuras y sorpresas pasadas.

Resuelve el problema que dejaba sin sentido a la alerta de eventos: SEC EDGAR
solo publica lo que **ya ocurrió**, y una alerta necesita saber qué va a pasar.

Predecir la fecha desde el histórico no sirve. Medido sobre 13.784 casos con la
mediana de los intervalos previos: **error mediano de 5 días**, y solo el 42 %
de las predicciones caen a menos de dos días. Avisar con esa imprecisión
obligaría a marcar una ventana de diez días cada trimestre.

Este endpoint es público y no exige clave. Da dos cosas:

**Hacia delante** (unos 14 días útiles): qué empresas publican y **cuándo**
dentro de la sesión —`time-pre-market`, `time-after-hours`— que es justo lo que
decide en qué sesión reacciona el mercado.

**Hacia atrás** (verificado hasta 2022): `epsForecast` (consenso), `eps` (real)
y `surprise` (diferencia porcentual). Es el dato que permitiría medir si la
sorpresa predice la dirección, algo que la clase de evento por sí sola no hace.

**Advertencia no verificable.** No hay forma de comprobar desde fuera si
`epsForecast` es el consenso que había *antes* del anuncio o uno revisado
después. Si fuera lo segundo, cualquier estudio de sorpresa tendría look-ahead.
La evidencia apunta a lo primero —las sorpresas van de −33 % a +27 % y no
convergen al valor real— pero es un supuesto, no un hecho comprobado, y así
debe figurar en cualquier conclusión que dependa de él.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import httpx
import polars as pl

from core.data.binance_dumps import DescargaError
from core.obs.logging import get_logger

log = get_logger(__name__)

URL = "https://api.nasdaq.com/api/calendar/earnings"
SOURCE = "nasdaq_calendar"

_CABECERAS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}

PAUSA_SEGUNDOS = 0.4

#: Momento del anuncio dentro de la sesión, tal como lo etiqueta Nasdaq.
MOMENTOS = {
    "time-pre-market": "antes_apertura",
    "time-after-hours": "tras_cierre",
    "time-not-supplied": "sin_especificar",
}

DDL_CALENDARIO = """
CREATE TABLE IF NOT EXISTS calendario (
    activo       VARCHAR NOT NULL,
    fecha        DATE    NOT NULL,
    momento      VARCHAR NOT NULL,
    eps_previsto DOUBLE,
    eps_real     DOUBLE,
    sorpresa_pct DOUBLE,
    n_estimaciones INTEGER,
    capitalizacion DOUBLE,
    source       VARCHAR NOT NULL,
    PRIMARY KEY (activo, fecha)
);
"""


def _numero(bruto: str | None) -> float | None:
    """Convierte '$3.68', '(0.15)' o '21.1' en float. Devuelve None si no hay dato."""
    if bruto in (None, "", "N/A"):
        return None
    texto = str(bruto).strip().replace("$", "").replace(",", "").replace("%", "")
    negativo = texto.startswith("(") and texto.endswith(")")
    if negativo:
        texto = texto[1:-1]
    try:
        valor = float(texto)
    except ValueError:
        return None
    return -valor if negativo else valor


def parse_dia(bruto: dict, fecha: date) -> pl.DataFrame:
    """Respuesta del calendario -> filas tipadas. Un día sin resultados da vacío."""
    filas_bruto = ((bruto.get("data") or {}) or {}).get("rows") or []
    filas = []
    for x in filas_bruto:
        simbolo = (x.get("symbol") or "").strip().upper()
        if not simbolo:
            continue
        filas.append(
            {
                "activo": simbolo,
                "fecha": fecha,
                "momento": MOMENTOS.get(x.get("time"), "sin_especificar"),
                "eps_previsto": _numero(x.get("epsForecast")),
                "eps_real": _numero(x.get("eps")),
                "sorpresa_pct": _numero(x.get("surprise")),
                "n_estimaciones": int(_numero(x.get("noOfEsts")) or 0),
                "capitalizacion": _numero(x.get("marketCap")),
                "source": SOURCE,
            }
        )
    if not filas:
        return pl.DataFrame(
            schema={
                "activo": pl.String, "fecha": pl.Date, "momento": pl.String,
                "eps_previsto": pl.Float64, "eps_real": pl.Float64,
                "sorpresa_pct": pl.Float64, "n_estimaciones": pl.Int32,
                "capitalizacion": pl.Float64, "source": pl.String,
            }
        )
    return pl.DataFrame(filas).unique(subset=["activo", "fecha"], keep="first")


def descargar_dia(fecha: date, cliente: httpx.Client | None = None) -> pl.DataFrame:
    propio = cliente is None
    cliente = cliente or httpx.Client(timeout=45, follow_redirects=True)
    try:
        r = cliente.get(URL, params={"date": str(fecha)}, headers=_CABECERAS)
        if r.status_code != 200:
            raise DescargaError(f"calendario {fecha}: HTTP {r.status_code}")
        return parse_dia(r.json(), fecha)
    finally:
        if propio:
            cliente.close()


def descargar_rango(
    desde: date, hasta: date, *, pausa: float = PAUSA_SEGUNDOS
) -> pl.DataFrame:
    """Descarga día a día. Los fines de semana devuelven vacío y no molestan."""
    trozos, fallos = [], 0
    with httpx.Client(timeout=45, follow_redirects=True) as cliente:
        dia = desde
        while dia <= hasta:
            try:
                df = descargar_dia(dia, cliente)
                if not df.is_empty():
                    trozos.append(df)
            except (DescargaError, httpx.HTTPError):
                fallos += 1
            time.sleep(pausa)
            dia += timedelta(days=1)

    if fallos:
        log.warning("días sin calendario", extra={"fallos": fallos})
    if not trozos:
        return parse_dia({}, desde)
    return pl.concat(trozos).sort(["fecha", "activo"])


def proximos_eventos(
    calendario: pl.DataFrame, activos: list[str], hoy: date, dias: int = 14
) -> pl.DataFrame:
    """Eventos futuros de una lista de activos dentro de la ventana."""
    limite = hoy + timedelta(days=dias)
    return (
        calendario.filter(
            pl.col("activo").is_in(activos)
            & (pl.col("fecha") >= hoy)
            & (pl.col("fecha") <= limite)
        )
        .sort(["fecha", "activo"])
    )
