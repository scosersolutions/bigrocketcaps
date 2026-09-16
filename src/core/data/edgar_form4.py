"""Forms 4 del día, leídos del índice diario de EDGAR.

El dataset trimestral de DERA (`insiders.py`) es la fuente para el histórico,
pero **no sirve para seguimiento en vivo**: la SEC lo publica cada tres meses.
Para registrar una predicción antes de que exista su resultado hace falta el
flujo diario, y eso es este módulo.

## Cómo se llega a los datos

1. `https://www.sec.gov/Archives/edgar/daily-index/{año}/QTR{n}/form.{fecha}.idx`
   lista todas las presentaciones del día por tipo. Verificado el 2026-09-02:
   el 2026-08-28 hubo **811 Forms 4**.

2. Cada línea apunta a un `.txt` que es la presentación completa, con el XML
   de titularidad dentro. Una petición por formulario y no hace falta resolver
   rutas de anexos.

El nombre que aparece en el índice es unas veces el del insider y otras el de
la empresa, así que **no se puede filtrar por ahí**: hay que abrir todos y
mirar el código de transacción. Son unos 800 al día.

## Coherencia con el histórico

Devuelve el mismo esquema que `insiders.parsear()` y aplica los mismos
filtros: solo código **P** (compra en mercado abierto), solo adquisiciones, y
descarta lo que no tenga precio o fecha coherente. Si los dos caminos no
produjeran filas idénticas, el seguimiento en papel estaría midiendo una cosa
distinta de la que se validó.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime

import httpx
import polars as pl

from core.data.binance_dumps import DescargaError
from core.data.insiders import CODIGO_COMPRA, SOURCE, _vacio
from core.obs.logging import get_logger

log = get_logger(__name__)

BASE = "https://www.sec.gov/Archives/edgar"

import os

#: La SEC exige identificarse con algo que permita contactar, pero el correo
#: no se incrusta en el código: en un repositorio público acabaría en los
#: rastreadores de spam y quedaría en el historial de git para siempre.
#: Se toma de MR_SEC_USER_AGENT, y el valor por defecto es suficiente para
#: que funcione sin configurar nada.
USER_AGENT = os.getenv("MR_SEC_USER_AGENT", "MoonRocket research contact@example.com")

_CABECERAS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

#: Tipos que hay que leer. El dataset trimestral de DERA cubre Forms 3, 4 y 5,
#: así que ceñirse al 4 mediría menos que el backtest. Contrastado sobre el
#: 2026-02-10: de los tres tickers que faltaban, FAST era un 4/A y ATLO un 5.
#:
#: El Form 3 se excluye a propósito: declara la posición inicial, no
#: transacciones, y no puede contener una compra con código P. Incluirlo serían
#: unos doscientos formularios diarios más para no encontrar nada.
#:
#: Las enmiendas (4/A) pueden repetir una compra ya declarada, pero la señal
#: cuenta **insiders distintos**, así que un duplicado del mismo declarante no
#: infla el grupo.
TIPOS = frozenset({"4", "4/A", "5", "5/A"})

#: Rellenos que usan las empresas sin cotización. Mismos que descarta el
#: camino trimestral en `insiders.parsear()`.
_SIN_SIMBOLO = frozenset({"NONE", "N/A", "NA", "-"})

#: El XML de titularidad viene envuelto en el sobre SGML de la presentación.
_XML = re.compile(r"<ownershipDocument>.*?</ownershipDocument>", re.DOTALL)


def url_indice(f: date) -> str:
    return f"{BASE}/daily-index/{f.year}/QTR{(f.month - 1) // 3 + 1}/form.{f:%Y%m%d}.idx"


#: Un 403 de la SEC significa dos cosas distintas y hay que separarlas.
#:
#: El índice de un día sin presentaciones —fin de semana, festivo— no existe, y
#: el almacén responde 403 con el XML de S3 en vez de 404. Eso es «no hubo
#: nada», no un error.
#:
#: Pero un 403 también es lo que devuelve la SEC cuando bloquea al cliente por
#: no identificarse: ahí llega una página HTML titulada «Your Request Originates
#: from an Undeclared Automated Tool». Tragarse ese segundo caso como si fuera
#: el primero sería lo peor posible: el registro en papel anotaría «hoy no hubo
#: compras» los días en que en realidad no nos dejaron mirar.
_S3_SIN_FICHERO = "<Code>AccessDenied</Code>"


def indice_diario(f: date, cliente: httpx.Client) -> list[str]:
    """Rutas de los Forms 4 presentados ese día.

    Devuelve lista vacía en fines de semana y festivos, que es lo correcto: no
    hubo presentaciones, no es un error. Un bloqueo sí lo es y se propaga.
    """
    r = cliente.get(url_indice(f), headers=_CABECERAS)
    if r.status_code == 404:
        return []
    if r.status_code == 403 and _S3_SIN_FICHERO in r.text:
        return []
    r.raise_for_status()

    rutas = []
    for linea in r.text.splitlines():
        # Formato de ancho fijo: el tipo es el primer campo y la ruta el
        # último. Se compara el campo entero y no un prefijo, porque hay tipos
        # como 40-17F1 o 424B2 que empiezan por los mismos dígitos.
        partes = linea.split()
        if len(partes) >= 2 and partes[0] in TIPOS and partes[-1].endswith(".txt"):
            rutas.append(partes[-1])
    return sorted(set(rutas))


def _valor(nodo: ET.Element | None, *camino: str) -> str | None:
    """Lee un campo que puede venir suelto o envuelto en <value>."""
    for paso in camino:
        if nodo is None:
            return None
        nodo = nodo.find(paso)
    if nodo is None:
        return None
    hijo = nodo.find("value")
    texto = (hijo if hijo is not None else nodo).text
    return texto.strip() if texto else None


def accession_de_ruta(ruta: str) -> str:
    """0001610717-26-000393 a partir de edgar/data/1770787/0001610717-26-000393.txt"""
    return ruta.rsplit("/", 1)[-1].removesuffix(".txt")


def parsear_form4(texto: str, presentado: date, accession: str = "") -> pl.DataFrame:
    """Extrae las compras en mercado abierto de una presentación.

    `accession` viene de la ruta del índice, no del XML: el documento no lo
    lleva dentro y es la clave que evita duplicar una presentación releída.
    """
    m = _XML.search(texto)
    if not m:
        return _vacio()
    try:
        doc = ET.fromstring(m.group(0))
    except ET.ParseError:
        return _vacio()

    simbolo = (_valor(doc, "issuer", "issuerTradingSymbol") or "").strip().upper()
    cik_emp = _valor(doc, "issuer", "issuerCik")
    # Las que no cotizan rellenan el símbolo con un relleno. El camino
    # trimestral ya los descartaba; sin esto los dos caminos no coinciden y
    # se cuela una "empresa" llamada N/A que agrupa insiders de compañías
    # distintas y sin relación entre sí.
    if not simbolo or simbolo in _SIN_SIMBOLO or not cik_emp:
        return _vacio()

    duenos = []
    for d in doc.findall("reportingOwner"):
        cik = _valor(d, "reportingOwnerId", "rptOwnerCik")
        if not cik:
            continue
        rel = d.find("reportingOwnerRelationship")
        etiquetas = [
            n for n, c in (("director", "isDirector"), ("officer", "isOfficer"),
                           ("tenPercentOwner", "isTenPercentOwner"))
            if rel is not None and (rel.findtext(c) or "").strip().lower() in ("1", "true")
        ]
        duenos.append((cik.zfill(10), ",".join(etiquetas),
                       (rel.findtext("officerTitle") or "").strip() if rel is not None else ""))
    if not duenos:
        return _vacio()

    filas = []
    for i, t in enumerate(doc.findall(".//nonDerivativeTransaction")):
        if _valor(t, "transactionCoding", "transactionCode") != CODIGO_COMPRA:
            continue
        if _valor(t, "transactionAmounts", "transactionAcquiredDisposedCode") != "A":
            continue
        try:
            acciones = float(_valor(t, "transactionAmounts", "transactionShares") or "")
            precio = float(_valor(t, "transactionAmounts", "transactionPricePerShare") or "")
            trans = datetime.strptime(
                (_valor(t, "transactionDate") or "")[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if acciones <= 0 or precio <= 0 or trans > presentado:
            continue

        for cik, rel, titulo in duenos:
            filas.append({
                "activo": simbolo,
                "cik_empresa": cik_emp.strip().zfill(10),
                "accession": accession,
                "linea": str(i),
                "cik_insider": cik,
                "relacion": rel,
                "titulo": titulo,
                "presentado": presentado,
                "transaccion": trans,
                "desfase": (presentado - trans).days,
                "acciones": acciones,
                "precio": precio,
                "valor": acciones * precio,
                "source": SOURCE,
            })

    return pl.DataFrame(filas, schema=_vacio().schema) if filas else _vacio()


def compras_del_dia(
    f: date, cliente: httpx.Client | None = None, *, pausa: float = 0.12
) -> pl.DataFrame:
    """Todas las compras en mercado abierto publicadas ese día.

    Deliberadamente secuencial con pausa: la SEC pide no pasar de 10 peticiones
    por segundo, y que te bloqueen la IP es peor que tardar tres minutos.
    """
    import time

    propio = cliente is None
    cliente = cliente or httpx.Client(timeout=60, follow_redirects=True)
    try:
        rutas = indice_diario(f, cliente)
        if not rutas:
            return _vacio()

        trozos, fallos = [], 0
        for ruta in rutas:
            try:
                r = cliente.get(f"https://www.sec.gov/Archives/{ruta}", headers=_CABECERAS)
                r.raise_for_status()
                df = parsear_form4(r.text, f, accession_de_ruta(ruta))
                if not df.is_empty():
                    trozos.append(df)
            except (httpx.HTTPError, DescargaError):
                fallos += 1
            time.sleep(pausa)

        if fallos:
            log.warning("formularios no leídos", extra={"fecha": str(f), "fallos": fallos,
                                                        "total": len(rutas)})
        return pl.concat(trozos) if trozos else _vacio()
    finally:
        if propio:
            cliente.close()
