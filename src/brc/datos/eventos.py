"""Eventos societarios fuertes, con la hora a la que se pudieron saber.

## Por qué estos eventos y no un factor

El diagnóstico del 2026-09-16 sobre 34.352 señales de insiders dejó claro que
ampliar el universo hacia arriba no da potencia: en los dos deciles más grandes
el exceso es negativo. La palanca que queda es la otra — **eventos más raros y
más fuertes**, donde el efecto por evento es grande aunque haya menos.

Todos salen de EDGAR, que es gratis, oficial y no se cae:

| Qué | Dónde | Por qué puede mover el precio |
|---|---|---|
| Cambio de CEO o CFO | 8-K item 5.02 | Cambia quien decide, y el mercado reprecia la gestión |
| Acuerdo relevante / M&A | 8-K item 1.01 | Una compra o fusión reprecia las dos partes |
| Resultados | 8-K item 2.02 | El más frecuente y el más estudiado |
| Quiebra o suspensión | 8-K items 1.03, 3.01 | Eventos terminales |
| Posición activista | SC 13D | Alguien con más del 5 % que declara intención de influir |
| Ampliación / secundaria | 424B, S-1 | Dilución anunciada |

## La hora, que no es un detalle

Medido sobre los 85.598 eventos que ya tenía MoonRocket: **el 58 % se acepta
después del cierre**. Tratar un 8-K de las 20:30 UTC como operable ese mismo
día regala media sesión de ventaja que nadie tuvo, y en un estudio de eventos
esa media sesión ES el resultado.

Por eso `aceptado` lleva hora y existe `tras_cierre`. Un evento tras el cierre
se opera al día siguiente, y punto.

## De dónde salen los datos

`submissions.zip` de EDGAR (1,56 GB), que trae un JSON por empresa con TODAS
sus presentaciones históricas, sus items y su `acceptanceDateTime`.

Se lee del ZIP sin descomprimirlo: descomprimido pasa de diez gigabytes, y no
hace falta ninguno de ellos en disco para quedarse con las filas que importan.

Alternativa descartada: pedir la API por empresa, 14.256 peticiones que además
solo devuelven las 1.000 presentaciones más recientes de cada una. Para el
histórico completo no vale.
"""
from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterator

import polars as pl

from core.data.eventos import tras_el_cierre

#: Items de 8-K que interesan, con su nombre. Lo que no está aquí se descarta:
#: el 8-K item 9.01 («documentos anexos») acompaña a casi todo y no dice nada
#: por sí solo.
ITEMS_8K = {
    "1.01": "acuerdo_relevante",
    "1.03": "quiebra",
    "2.02": "resultados",
    "3.01": "suspension_cotizacion",
    "5.02": "cambio_directivo",
}

#: Formularios que interesan por sí mismos, sin mirar items.
FORMULARIOS = {
    "SC 13D": "posicion_activista",
    "SC 13D/A": "posicion_activista",
    "S-1": "registro_emision",
    "424B4": "emision_precio",
    "424B5": "emision_precio",
    "DEF 14A": "convocatoria_junta",
}

#: El corte lo decide `core.data.eventos.tras_el_cierre`, que ya estaba escrito
#: y lo hace mejor de lo que se escribió aquí primero: evalúa en hora de Nueva
#: York --así el horario de verano no desplaza el corte medio año-- y cuenta el
#: fin de semana como posterior al cierre.
#:
#: La primera versión de este módulo usaba un umbral fijo en UTC y no miraba el
#: día de la semana. Daba un 12-36 % de eventos tras el cierre donde la función
#: buena da un 58 %: la diferencia es media sesión de ventaja regalada, en la
#: mitad de los eventos.


def _fecha(v: str | None):
    return datetime.fromisoformat(v).date() if v else None


def eventos_de(cruda: dict) -> pl.DataFrame:
    """Extrae los eventos interesantes del JSON de una empresa.

    `cruda` es el contenido de CIK##########.json tal como lo trae el ZIP.
    """
    recientes = (cruda.get("filings") or {}).get("recent") or {}
    formularios = recientes.get("form") or []
    if not formularios:
        return _vacio()

    cik = int(cruda.get("cik") or 0)
    tickers = cruda.get("tickers") or []
    ticker = tickers[0] if tickers else ""

    filas = []
    for i, form in enumerate(formularios):
        items = (recientes.get("items") or [""] * len(formularios))[i] or ""
        clases = _clasificar(form, items)
        if not clases:
            continue
        acep = (recientes.get("acceptanceDateTime") or [None] * len(formularios))[i]
        aceptado = datetime.fromisoformat(acep.replace("Z", "+00:00")) if acep else None
        for clase in clases:
            filas.append({
                "cik": cik,
                "ticker": ticker,
                "accession": recientes["accessionNumber"][i],
                "formulario": form,
                "clase": clase,
                "items": items,
                "aceptado": aceptado,
                "presentado": _fecha((recientes.get("filingDate") or [None])[i]),
                "periodo": _fecha((recientes.get("reportDate") or [None])[i] or None),
                # Sin hora no se puede afirmar que fuera operable ese día, así
                # que se asume que no: es el supuesto que no regala ventaja.
                "tras_cierre": tras_el_cierre(aceptado) if aceptado else True,
            })
    return pl.DataFrame(filas, schema=_esquema()) if filas else _vacio()


def _clasificar(formulario: str, items: str) -> list[str]:
    """Qué eventos representa una presentación. Puede ser más de uno."""
    if formulario in FORMULARIOS:
        return [FORMULARIOS[formulario]]
    if formulario.startswith("8-K"):
        sueltos = [x.strip() for x in items.split(",") if x.strip()]
        return [ITEMS_8K[x] for x in sueltos if x in ITEMS_8K]
    return []


def _esquema() -> dict:
    return {
        "cik": pl.Int64, "ticker": pl.Utf8, "accession": pl.Utf8,
        "formulario": pl.Utf8, "clase": pl.Utf8, "items": pl.Utf8,
        "aceptado": pl.Datetime(time_zone="UTC"), "presentado": pl.Date,
        "periodo": pl.Date, "tras_cierre": pl.Boolean,
    }


def _vacio() -> pl.DataFrame:
    return pl.DataFrame(schema=_esquema())


def recorrer_zip(ruta: Path, *, limite: int = 0) -> Iterator[tuple[str, dict]]:
    """Va sacando (nombre, json) de cada empresa sin descomprimir el ZIP entero.

    Solo los `CIK##########.json` de primer nivel: el ZIP trae además ficheros
    de continuación (`CIK...-submissions-001.json`) con el histórico más viejo
    de las empresas que presentan mucho, y se tratan aparte para no mezclar dos
    formatos en el mismo bucle.
    """
    with zipfile.ZipFile(ruta) as z:
        n = 0
        for nombre in z.namelist():
            if not nombre.startswith("CIK") or not nombre.endswith(".json"):
                continue
            if "-submissions-" in nombre:
                continue
            try:
                with z.open(nombre) as f:
                    yield nombre, json.load(f)
            except (json.JSONDecodeError, KeyError, zipfile.BadZipFile):
                # Una empresa con el JSON roto no puede tirar la extracción
                # entera, pero tampoco desaparecer en silencio: quien llama
                # cuenta cuántas salieron y compara con cuántas hay.
                continue
            n += 1
            if limite and n >= limite:
                return
