"""Hechos relevantes: los 8-K que una empresa está obligada a presentar.

Es la única forma de «noticia» que este proyecto puede usar sin contradecirse.
Un titular de prensa no es verificable, no viene fechado de forma fiable y no
dice de dónde sale. Un 8-K sí: la empresa está **obligada** a presentarlo cuando
ocurre algo material, lleva la fecha en que se presentó, y su código de item dice
de qué se trata sin que nadie lo interprete.

## Lo que no se hace

No se convierte un hecho en una dirección. El proyecto midió que la sesión de
resultados multiplica el movimiento y que su signo es simétrico; de un «acuerdo
relevante firmado» no sale un pronóstico. Se marcan como adversos únicamente los
items que el **propio formulario** define así —concurso, exclusión de cotización,
cuentas anteriores no fiables, deterioro de activos— y nada más.

## Por qué se descarga en vez de pedirse al vuelo

El Worker sabe traer los 8-K de un valor cuando alguien lo mira. Pero el listado
de contraste compara *todos* los valores del cuaderno a la vez, y eso serían
doscientas peticiones desde el navegador de cada lector. Bajándolos en la pasada,
el criterio viaja dentro del HTML como los otros tres.

Réplica de `hechos()` en `worker/ficha.js`. Si divergieran, el mismo valor diría
cosas distintas según se mirara en el listado o en la ficha viva.
"""

from __future__ import annotations

import time
from datetime import date

import httpx
import polars as pl

from core.data.binance_dumps import DescargaError
from core.data.edgar import cabeceras
from core.obs.logging import get_logger

log = get_logger(__name__)

URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SOURCE = "sec_8k"

PAUSA_SEGUNDOS = 0.15

#: Qué significa cada código de item de un 8-K. Los `True` son adversos por
#: definición del formulario, no por interpretación nuestra.
ITEMS: dict[str, tuple[str, bool]] = {
    "1.01": ("Acuerdo relevante firmado", False),
    "1.02": ("Acuerdo relevante terminado", False),
    "1.03": ("Concurso o quiebra", True),
    "2.01": ("Compra o venta de activos", False),
    "2.02": ("Resultados del trimestre", False),
    "2.03": ("Nueva deuda u obligación", False),
    "2.04": ("Vencimiento anticipado de deuda", True),
    "2.05": ("Reestructuración con coste", True),
    "2.06": ("Deterioro de activos", True),
    "3.01": ("Aviso de exclusión de cotización", True),
    "3.02": ("Venta de acciones no registrada", False),
    "3.03": ("Cambio en derechos de accionistas", False),
    "4.01": ("Cambio de auditor", True),
    "4.02": ("Cuentas anteriores no fiables", True),
    "5.01": ("Cambio de control", False),
    "5.02": ("Cambios en consejo o dirección", False),
    "5.03": ("Cambio de estatutos o ejercicio", False),
    "5.07": ("Resultados de la junta", False),
    "7.01": ("Información divulgada (Reg FD)", False),
    "8.01": ("Otros hechos relevantes", False),
    "9.01": ("Documentos adjuntos", False),
}

#: El 9.01 solo dice «lleva adjuntos». No es un hecho y ensucia la lista.
ADMINISTRATIVOS = {"9.01"}

DDL_HECHOS = """
CREATE TABLE IF NOT EXISTS hechos (
    activo     VARCHAR NOT NULL,
    fecha      DATE    NOT NULL,
    accesion   VARCHAR NOT NULL,
    items      VARCHAR,
    adverso    BOOLEAN NOT NULL,
    source     VARCHAR NOT NULL,
    PRIMARY KEY (activo, accesion)
);
"""

_ESQUEMA = {
    "activo": pl.Utf8, "fecha": pl.Date, "accesion": pl.Utf8,
    "items": pl.Utf8, "adverso": pl.Boolean, "source": pl.Utf8,
}


def parse(simbolo: str, bruto: dict, tope: int = 12) -> pl.DataFrame:
    """Submissions de la SEC -> un 8-K por fila, del más reciente hacia atrás."""
    r = ((bruto.get("filings") or {}).get("recent")) or {}
    formas = r.get("form") or []
    filas = []
    for i, forma in enumerate(formas):
        if forma != "8-K" or len(filas) >= tope:
            continue
        cods = [
            c.strip()
            for c in str((r.get("items") or [""] * len(formas))[i] or "").split(",")
            if c.strip() and c.strip() not in ADMINISTRATIVOS
        ]
        filas.append({
            "activo": simbolo.upper(),
            "fecha": date.fromisoformat(r["filingDate"][i]),
            # La accesión es lo que hace única a una presentación: dos 8-K del
            # mismo día son dos hechos, y sin ella uno pisaría al otro.
            "accesion": r["accessionNumber"][i],
            "items": ",".join(cods) or None,
            "adverso": any(ITEMS.get(c, ("", False))[1] for c in cods),
            "source": SOURCE,
        })
    return pl.DataFrame(filas, schema=_ESQUEMA)


def descargar(
    simbolos: dict[str, str], user_agent: str, cliente: httpx.Client | None = None
) -> pl.DataFrame:
    """`simbolos` es {ticker: cik}. Un valor que falla se anota y se sigue."""
    propio = cliente is None
    cliente = cliente or httpx.Client(timeout=45, follow_redirects=True)
    trozos = []
    try:
        for i, (s, cik) in enumerate(simbolos.items()):
            try:
                resp = cliente.get(URL.format(cik=cik), headers=cabeceras(user_agent))
                if resp.status_code == 404:
                    continue
                if resp.status_code != 200:
                    raise DescargaError(f"{s} -> {resp.status_code}")
                df = parse(s, resp.json())
                if not df.is_empty():
                    trozos.append(df)
            except (DescargaError, httpx.HTTPError, ValueError, KeyError) as e:
                log.warning("hechos_fallidos", simbolo=s, error=str(e))
            if i + 1 < len(simbolos):
                time.sleep(PAUSA_SEGUNDOS)
    finally:
        if propio:
            cliente.close()
    return pl.concat(trozos) if trozos else pl.DataFrame(schema=_ESQUEMA)
