"""Universo point-in-time por capitalización reconstruida.

## Por qué no se usan los constituyentes del índice

No hay fuente gratuita y fiable de qué empresas formaban el S&P 500 o el 400 en
una fecha pasada. Las listas que circulan son la composición de HOY, y usarlas
para el pasado es survivorship bias en su forma más pura: las que quebraron o
fueron absorbidas no aparecen, y son justo las que habrían arrastrado a la baja
cualquier estrategia que ordene el corte transversal.

Ese error ya se cometió una vez en MoonRocket, con una lista fija de 110
símbolos elegidos «por tener historia desde 2021-09»: de los 110, habían muerto
CERO, cuando el universo point-in-time real tenía 136 deslistados. El sesgo lo
había metido el descargador, no el mercado.

## Qué se usa en su lugar

Capitalización reconstruida: acciones en circulación por precio de cierre, con
los deciles recalculados cada trimestre usando **solo datos disponibles esa
fecha**. Una empresa que quebró en 2018 está en el universo de 2015 con su
tamaño de entonces, como debe ser.

Las acciones salen de la API `frames` de XBRL, verificada el 2026-09-16:

    CY2012Q1I  7.016 empresas      CY2020Q1I  4.774
    CY2015Q1I  6.258 empresas      CY2026Q1I  4.611

La caída no es pérdida de cobertura: es la reducción real del número de
empresas cotizadas en Estados Unidos.

## La fecha que importa

Cada fila trae `accn`, el número de la presentación de donde sale el dato. Esa
es la pieza que hace honesto el point-in-time: `end` dice a qué fecha se
refiere el dato, y la presentación dice **cuándo se pudo saber**. Se opera
siempre con la segunda. Un dato de cierre de trimestre no está disponible el
último día del trimestre, sino semanas después.

## Límites

- 10 peticiones por segundo, y la SEC lo hace cumplir por IP.
- El User-Agent tiene que llevar un correo real. Con uno inventado, el primer
  aviso es el bloqueo.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date

import httpx
import polars as pl

FRAMES = ("https://data.sec.gov/api/xbrl/frames/dei/"
          "EntityCommonStockSharesOutstanding/shares/{periodo}.json")

#: La SEC limita a 10 peticiones por segundo CONTANDO todas las máquinas de un
#: mismo usuario. Se deja margen: el proyecto no tiene ninguna prisa y que le
#: corten el acceso costaría más que esperar.
PAUSA_S = 0.2


class UniversoError(RuntimeError):
    """No se pudo construir el universo, y no se devuelve uno a medias."""


@dataclass(frozen=True)
class Trimestre:
    anyo: int
    q: int

    @property
    def clave(self) -> str:
        """`CY2024Q1I`: la I final es «instantáneo», no acumulado del periodo."""
        return f"CY{self.anyo}Q{self.q}I"

    @staticmethod
    def rango(desde: int, hasta: int) -> list["Trimestre"]:
        return [Trimestre(a, q) for a in range(desde, hasta + 1) for q in (1, 2, 3, 4)]


def acciones_en_circulacion(
    t: Trimestre, *, user_agent: str, cliente: httpx.Client | None = None
) -> pl.DataFrame:
    """Acciones en circulación declaradas en ese trimestre, por empresa.

    Devuelve cik, nombre, `fin` (a qué fecha se refiere), `accn` (de qué
    presentación sale) y el valor. **No** devuelve la fecha de publicación: esa
    se obtiene cruzando `accn` con las presentaciones, y hasta entonces el dato
    no es accionable.
    """
    if "@" not in user_agent:
        raise UniversoError(
            "la SEC exige un User-Agent con correo de contacto REAL. Con uno "
            "inventado no hay aviso previo: hay bloqueo.")
    propio = cliente is None
    cliente = cliente or httpx.Client(timeout=60, follow_redirects=True)
    try:
        r = cliente.get(FRAMES.format(periodo=t.clave),
                        headers={"User-Agent": user_agent,
                                 "Accept-Encoding": "gzip, deflate"})
        if r.status_code == 404:
            # Un trimestre sin datos es un hueco, no un error: los primeros
            # años de XBRL tienen cobertura irregular.
            return _vacio()
        r.raise_for_status()
        filas = r.json().get("data", [])
    except httpx.HTTPError as e:
        raise UniversoError(f"{t.clave}: {type(e).__name__}: {e}") from e
    finally:
        if propio:
            cliente.close()
        time.sleep(PAUSA_S)

    if not filas:
        return _vacio()
    return pl.DataFrame(
        {
            "cik": [int(f["cik"]) for f in filas],
            "nombre": [str(f.get("entityName", "")).strip() for f in filas],
            "fin": [date.fromisoformat(f["end"]) for f in filas],
            "accn": [str(f.get("accn", "")) for f in filas],
            "acciones": [float(f["val"]) for f in filas],
            "trimestre": [t.clave] * len(filas),
        }
    ).filter(pl.col("acciones") > 0)


def _vacio() -> pl.DataFrame:
    return pl.DataFrame(
        schema={"cik": pl.Int64, "nombre": pl.Utf8, "fin": pl.Date,
                "accn": pl.Utf8, "acciones": pl.Float64, "trimestre": pl.Utf8})


def deciles(
    historico: pl.DataFrame, precios: pl.DataFrame, *, n: int = 10
) -> pl.DataFrame:
    """Capitalización y decil por empresa, con el último dato PUBLICADO.

    `historico` es (cik, publicado, acciones); `precios` es (cik, fecha, close).

    ## Por qué no se usa "el trimestre correspondiente"

    Medido sobre los 60 trimestres descargados: Q1 trae 5.401 empresas de
    media, Q2 5.206, Q3 4.921 y **Q4 solo 2.911**, un 44 % menos. No es un
    hueco de la descarga: `EntityCommonStockSharesOutstanding` se declara en la
    portada del 10-Q y del 10-K, y las empresas con año fiscal en diciembre
    presentan el 10-K en el Q1 siguiente, no en el Q4.

    Construir el universo por trimestre daría, cada cuarto trimestre, un
    universo formado casi solo por empresas de calendario fiscal atípico. Eso
    es un sesgo de selección que no se ve en ninguna métrica agregada y que
    contamina cualquier estudio transversal que caiga en esas fechas.

    Así que a cada fecha se usa el último dato **publicado antes de ella**,
    venga del trimestre que venga. Es lo correcto point-in-time y de paso
    elimina el hueco: una empresa que declaró en Q3 sigue en el universo de
    diciembre con esa cifra, que es exactamente lo que se sabía entonces.

    El decil se calcula **dentro de cada fecha**, nunca sobre el histórico
    entero: si no, una empresa parecería grande en 2012 por lo que valía el
    mercado en 2026. El decil 10 es el más grande. Las empresas sin precio ese
    día se quedan fuera, que es lo que significa no cotizar.
    """
    vacio = pl.DataFrame(schema={"cik": pl.Int64, "fecha": pl.Date,
                                 "capitalizacion": pl.Float64,
                                 "decil": pl.Int32})
    if historico.is_empty() or precios.is_empty():
        return vacio

    # join_asof exige las dos partes ordenadas por la clave temporal.
    izq = precios.sort("fecha")
    der = historico.select("cik", "publicado", "acciones").sort("publicado")
    j = izq.join_asof(
        der, left_on="fecha", right_on="publicado", by="cik",
        strategy="backward",      # el ultimo publicado ANTES de la fecha
    ).filter(pl.col("acciones").is_not_null())
    if j.is_empty():
        return vacio

    return (
        j.with_columns((pl.col("close") * pl.col("acciones")).alias("capitalizacion"))
        .filter(pl.col("capitalizacion") > 0)
        .with_columns(
            pl.col("capitalizacion").rank("ordinal").over("fecha").alias("_puesto"),
            pl.len().over("fecha").alias("_vivas"),
        )
        .with_columns(
            (((pl.col("_puesto") - 1) * n) // pl.col("_vivas") + 1)
            .cast(pl.Int32).alias("decil")
        )
        .select("cik", "fecha", "capitalizacion", "decil")
        .sort("fecha", "capitalizacion", descending=[False, True])
    )
