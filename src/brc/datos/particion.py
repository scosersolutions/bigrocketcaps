"""Quién puede ver qué datos, y por qué la valla es de disco y no de buenos modales.

## El problema que resuelve

Discovery automático y un contador de contrastes compartido son incompatibles.
Con cinco mil pruebas exploratorias, el listón del Deflated Sharpe sube tanto
que nada podría validarse nunca. La salida no es contar menos: es que Discovery
**no pueda mirar** los datos contra los que se valida.

## La partición: por sector, no por fecha

La primera versión del plan partía el histórico en dos mitades temporales. Para
eventos raros —M&A, cambios de directivo— eso tira la mitad de la muestra justo
donde falta, y además mete COVID entero dentro de un lado.

Se parte por **sector**, con las dos mitades viendo 2012-2026 completos:

- `EXPLORACION`: la mitad de los sectores. Ahí se puede hacer lo que sea —ML,
  rejillas, data mining— sin límite de pruebas y sin consumir presupuesto.
- `VALIDACION`: la otra mitad. Solo hipótesis selladas, y cada sello cuesta un
  contraste.
- `HOLDOUT`: todos los sectores desde 2026-01-01. Se abre una vez por hipótesis.

Por sector y no por empresa suelta porque las empresas del mismo sector
comparten shocks: partir por ticker dejaría el mismo evento a los dos lados de
la valla.

**La fuga que queda, declarada**: hay correlación entre sectores, y un shock
macro afecta a los dos lados. Eso no se puede eliminar sin tirar muestra. El
holdout temporal lo acota.

## Tres cercos, y solo el tercero funciona de verdad

1. **Lógico**: esta clase recorta lo que devuelve.
2. **De proceso**: discovery corre con `BRC_VENTANA=exploracion`.
3. **De disco**: el lago se publica en carpetas separadas y el job de discovery
   solo descarga la suya. No es que no deba leer validación: es que no la tiene.

El tercero es el único que no depende de que nadie se acuerde de nada, y es
gratis. En MoonRocket el recorte del holdout vivía dentro de cada script: tres
lo hacían y uno no, y el que no lo hacía juzgó 8.016 velas de dentro sin que
nada avisara.

## El sorteo

Se hace UNA vez, con semilla fija, y se sella antes de mirar ningún resultado.
Cambiarlo después de ver un veredicto sería elegir la partición que conviene,
que es la forma más limpia de engañarse que existe.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import polars as pl


class Ventana(StrEnum):
    EXPLORACION = "exploracion"
    VALIDACION = "validacion"
    HOLDOUT = "holdout"


class PermisoDenegado(Exception):
    """Se han pedido datos de una ventana para la que no hay permiso."""


#: Semilla del sorteo de sectores. Sellada el 2026-09-16, antes de medir nada.
#: Cambiarla reparte otra vez el universo y **invalida todo lo validado**: no
#: es un parámetro, es la definición de qué muestra vio cada estudio.
SEMILLA = "brc-2026-09-16"

#: Desde aquí, holdout para todos los sectores. Coincide con lo declarado en
#: `hipotesis/holdouts.toml`, que es la fuente de esa fecha.
HOLDOUT_DESDE = date(2026, 1, 1)


@dataclass(frozen=True)
class Reparto:
    """Qué sectores caen de cada lado. Reproducible desde la semilla."""

    exploracion: frozenset[str]
    validacion: frozenset[str]
    semilla: str = SEMILLA

    @property
    def sectores(self) -> frozenset[str]:
        return self.exploracion | self.validacion


def sortear(sectores: list[str], *, semilla: str = SEMILLA) -> Reparto:
    """Reparte los sectores en dos mitades, de forma reproducible.

    El sorteo sale de un hash del nombre del sector con la semilla, y no de un
    generador con estado: así añadir un sector nuevo mañana no reordena a los
    que ya estaban. Si el reparto cambiara al crecer el universo, un estudio de
    hace un mes habría visto una muestra distinta de la que dice haber visto.
    """
    if not sectores:
        return Reparto(frozenset(), frozenset(), semilla)
    izquierda, derecha = set(), set()
    for s in sorted(set(sectores)):
        h = hashlib.sha256(f"{semilla}:{s}".encode()).digest()
        (izquierda if h[0] % 2 == 0 else derecha).add(s)
    return Reparto(frozenset(izquierda), frozenset(derecha), semilla)


class Puerta:
    """Devuelve datos solo de la ventana para la que hay permiso.

    No comprueba intenciones ni nombres de variables: recorta las filas. Es lo
    único que no se puede rodear sin querer.
    """

    def __init__(self, permiso: Ventana, reparto: Reparto,
                 *, holdout_desde: date = HOLDOUT_DESDE) -> None:
        self.permiso = permiso
        self.reparto = reparto
        self.holdout_desde = holdout_desde

    def sectores_visibles(self) -> frozenset[str]:
        if self.permiso is Ventana.EXPLORACION:
            return self.reparto.exploracion
        if self.permiso is Ventana.VALIDACION:
            return self.reparto.validacion
        return self.reparto.sectores          # el holdout los ve todos

    def recortar(self, df: pl.DataFrame, *, columna_sector: str = "sector",
                 columna_fecha: str = "fecha") -> pl.DataFrame:
        """Deja solo lo que esta ventana puede ver.

        Fuera del holdout se corta ANTES de `holdout_desde`: explorar y validar
        trabajan con lo que ya pasó y nunca con el periodo reservado. Dentro del
        holdout, solo el periodo reservado: abrirlo no da acceso al resto, da
        acceso a lo que faltaba.
        """
        if df.is_empty():
            return df
        fuera = df
        if columna_sector in df.columns:
            fuera = fuera.filter(
                pl.col(columna_sector).is_in(list(self.sectores_visibles())))
        if columna_fecha in df.columns:
            if self.permiso is Ventana.HOLDOUT:
                fuera = fuera.filter(pl.col(columna_fecha) >= self.holdout_desde)
            else:
                fuera = fuera.filter(pl.col(columna_fecha) < self.holdout_desde)
        return fuera

    def exigir(self, pedida: Ventana) -> None:
        """Falla si se piden datos de otra ventana.

        Existe para que pedir de más sea un error ruidoso y no una lectura
        silenciosa que nadie revisa.
        """
        if pedida is not self.permiso:
            raise PermisoDenegado(
                f"este proceso tiene permiso de {self.permiso} y ha pedido "
                f"{pedida}. Si de verdad hace falta, se lanza otro proceso con "
                f"ese permiso; compartirlos en el mismo es como no tenerlos."
            )
