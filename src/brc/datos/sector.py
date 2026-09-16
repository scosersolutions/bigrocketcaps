"""Sector de cada empresa, para la valla de `brc.datos.particion`.

## Por que Fama-French y no GICS

El plan (FASE 2.2) habla de "sectores GICS", pero GICS es una taxonomia de
pago de MSCI/S&P: no hay forma de conseguirla a coste 0 (decision D6). EDGAR sí
publica gratis el SIC de 4 digitos de cada empresa -mismo JSON que ya lee
`brc.datos.eventos` para construir `eventos_societarios`-, y la clasificacion
de 12 industrias de Fama-French es un cruce SIC->sector publico, citable y
estandar en la literatura academica: el sustituto de coste 0 mas defendible
que hay, en el mismo espiritu que TASK 2.2 sustituye el indice real por
capitalizacion reconstruida.

La sustitucion queda declarada aqui, no escondida: el `sector` que se guarda
es el codigo corto de Fama-French (NoDur, BusEq, Money...), no un nombre GICS.

## La fuente, verbatim

Kenneth R. French Data Library, "12 Industry Portfolios", definiciones de SIC
descargadas el 2026-09-16 de
https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Siccodes12.zip
(enlazado desde det_12_ind_port.html). Los rangos de abajo son ese fichero
`Siccodes12.txt` transcrito sin alterar ni un limite.

## Por que solo 1.759 empresas y no las 14.256 del universo

`sortear()` reparte NOMBRES de sector (hay 12, fijos, citados arriba), no
empresas: el reparto es el mismo lo mire quien lo mire. Lo unico que hace
falta por empresa es a que sector pertenece, y eso solo hace falta para las
que de verdad se van a mirar. Pedir el SIC de las 14.256 sin necesitarlo
today son ~14.000 peticiones de mas a la SEC para un dato que nadie usa
todavia; se amplia el dia que otra hipotesis lo necesite.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
import polars as pl

from core.data.edgar import cabeceras

#: (codigo, descripcion, [(desde, hasta), ...]) tal como los publica Kenneth
#: French. El orden importa para nada semantico, pero se conserva el original.
SICCODES12: tuple[tuple[str, str, tuple[tuple[int, int], ...]], ...] = (
    ("NoDur", "Consumer Nondurables -- Food, Tobacco, Textiles, Apparel, Leather, Toys",
     ((100, 999), (2000, 2399), (2700, 2749), (2770, 2799), (3100, 3199), (3940, 3989))),
    ("Durbl", "Consumer Durables -- Cars, TVs, Furniture, Household Appliances",
     ((2500, 2519), (2590, 2599), (3630, 3659), (3710, 3711), (3714, 3714),
      (3716, 3716), (3750, 3751), (3792, 3792), (3900, 3939), (3990, 3999))),
    ("Manuf", "Manufacturing -- Machinery, Trucks, Planes, Off Furn, Paper, Com Printing",
     ((2520, 2589), (2600, 2699), (2750, 2769), (3000, 3099), (3200, 3569),
      (3580, 3629), (3700, 3709), (3712, 3713), (3715, 3715), (3717, 3749),
      (3752, 3791), (3793, 3799), (3830, 3839), (3860, 3899))),
    ("Enrgy", "Oil, Gas, and Coal Extraction and Products",
     ((1200, 1399), (2900, 2999))),
    ("Chems", "Chemicals and Allied Products",
     ((2800, 2829), (2840, 2899))),
    ("BusEq", "Business Equipment -- Computers, Software, and Electronic Equipment",
     ((3570, 3579), (3660, 3692), (3694, 3699), (3810, 3829), (7370, 7379))),
    ("Telcm", "Telephone and Television Transmission",
     ((4800, 4899),)),
    ("Utils", "Utilities",
     ((4900, 4949),)),
    ("Shops", "Wholesale, Retail, and Some Services (Laundries, Repair Shops)",
     ((5000, 5999), (7200, 7299), (7600, 7699))),
    ("Hlth", "Healthcare, Medical Equipment, and Drugs",
     ((2830, 2839), (3693, 3693), (3840, 3859), (8000, 8099))),
    ("Money", "Finance",
     ((6000, 6999),)),
)

#: Todo lo que no cae en ningun rango de arriba. Es un cubo explicito de
#: Fama-French, no un "no se sabe": Mines, Constr, BldMt, Trans, Hotels, Bus
#: Serv, Entertainment.
OTRO = "Other"

#: Los 12 nombres, en el orden en que hay que pasarselos a `particion.sortear`.
#: Fijo: cambiar este conjunto reparte otra vez el universo.
SECTORES = tuple(codigo for codigo, _, _ in SICCODES12) + (OTRO,)


def sector_de_sic(sic: int | None) -> str:
    """SIC de 4 digitos -> codigo de industria de Fama-French.

    Sin SIC (empresa sin clasificar en EDGAR) tambien cae en `OTRO`: no hay
    sector que inventar, y `OTRO` ya es donde caen los casos que no encajan.
    """
    if sic is None:
        return OTRO
    for codigo, _, rangos in SICCODES12:
        if any(desde <= sic <= hasta for desde, hasta in rangos):
            return codigo
    return OTRO


def _extraer_sic(cruda: dict) -> tuple[int, int | None] | None:
    """CIK y SIC del JSON de `data.sec.gov/submissions/CIK##########.json`.

    Funcion pura para poder probarla sin red: `obtener_sic` solo hace el
    bucle HTTP y le pasa lo que baja.
    """
    cik = cruda.get("cik")
    if cik is None:
        return None
    sic = cruda.get("sic")
    return int(cik), (int(sic) if sic not in (None, "") else None)


def obtener_sic(
    ciks: list[int],
    user_agent: str,
    *,
    cliente: httpx.Client | None = None,
    pausa: float = 0.11,
) -> pl.DataFrame:
    """SIC y sector de cada CIK, uno por uno contra `data.sec.gov`.

    Un solo campo estatico por empresa: no hace falta el ZIP masivo de
    `submissions.zip` que usa `brc.datos.eventos` para el historial completo
    de presentaciones, y evita bajar 1,56 GB para leer un numero de cuatro
    cifras. `pausa=0.11` deja margen bajo el limite de 10 peticiones/segundo
    de la SEC.
    """
    propio = cliente is None
    cliente = cliente or httpx.Client(timeout=30, follow_redirects=True)
    filas = []
    ahora = datetime.now(UTC)
    try:
        for cik in ciks:
            url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
            r = cliente.get(url, headers=cabeceras(user_agent))
            time.sleep(pausa)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            extraido = _extraer_sic(r.json())
            if extraido is None:
                continue
            cik_real, sic = extraido
            filas.append({
                "cik": cik_real, "sic": sic, "sector": sector_de_sic(sic),
                "ingerido": ahora, "source": "sec.gov/submissions",
            })
    finally:
        if propio:
            cliente.close()
    if not filas:
        return pl.DataFrame(schema={
            "cik": pl.Int64, "sic": pl.Int64, "sector": pl.Utf8,
            "ingerido": pl.Datetime(time_zone="UTC"), "source": pl.Utf8,
        })
    return pl.DataFrame(filas)
