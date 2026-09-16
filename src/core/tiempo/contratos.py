"""Cuándo estuvo disponible cada tabla, declarado y no supuesto.

Cada fuente fecha sus filas a su manera, y ninguna de esas fechas es la que
importa para decidir. Aquí se escribe, por tabla, cómo se pasa de la fecha que
trae el dato a la fecha en que nosotros lo habríamos tenido.

Si una tabla no está en `CONTRATOS`, no se puede unir con `unir_causal`. Es
deliberado: añadir una fuente obliga a contestar cuándo se supo, y esa pregunta
es la que nadie se hace cuando escribe un `join`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import polars as pl


class FuturoEnLosDatos(Exception):
    """Una fila estaría disponible después del instante en que se usa."""


@dataclass(frozen=True)
class Contrato:
    """Cómo se calcula el `availability_time` de una tabla.

    `desplazamiento` se SUMA a la columna de fecha. Es cero cuando la fecha ya
    es el instante de disponibilidad, y positivo cuando la fecha etiqueta el
    principio de un intervalo cuyo dato no está hasta el final.
    """

    tabla: str
    columna_fecha: str
    desplazamiento: timedelta
    porque: str

    def aplicar(self, df: pl.DataFrame) -> pl.DataFrame:
        """Añade `availability_time` según este contrato."""
        if self.columna_fecha not in df.columns:
            raise KeyError(
                f"`{self.tabla}` declara su fecha en `{self.columna_fecha}` y esa "
                f"columna no está. Columnas: {df.columns}")
        return df.with_columns(
            (pl.col(self.columna_fecha) + self.desplazamiento).alias("availability_time"))


#: El contrato de cada tabla. Cada entrada nace de un caso concreto, no de una
#: regla general aplicada por simetría.
CONTRATOS: dict[str, Contrato] = {
    # `ts` es la APERTURA de la vela. El cierre —y con él el dato— llega al
    # final. Usar la vela de t para decidir en t es leakage de una vela entera,
    # que es el error más caro y el más fácil de cometer.
    "ohlcv_1h": Contrato(
        tabla="ohlcv_1h",
        columna_fecha="ts",
        desplazamiento=timedelta(hours=1),
        porque="`ts` es la apertura; la vela no está cerrada hasta una hora después",
    ),
    "ohlcv_15m": Contrato(
        tabla="ohlcv_15m",
        columna_fecha="ts",
        desplazamiento=timedelta(minutes=15),
        porque="`ts` es la apertura de la vela de 15 minutos",
    ),
    "ohlcv_1d": Contrato(
        tabla="ohlcv_1d",
        columna_fecha="ts",
        desplazamiento=timedelta(days=1),
        porque="`ts` es la apertura de la sesión",
    ),

    # `create_time` ES el instante de la observación: no hay que desplazar nada.
    "metrics_5m": Contrato(
        tabla="metrics_5m",
        columna_fecha="ts",
        desplazamiento=timedelta(0),
        porque="`create_time` es el instante en que Binance publica la observación",
    ),

    # EL CASO QUE MOTIVÓ EL MÓDULO. `a_horario()` agrega con
    # `group_by_dynamic(every="1h")`, que etiqueta el grupo con el INICIO de la
    # ventana. La observación de las 09:55 sale con ts = 09:00 y solo está
    # disponible a las 09:55. Se desplaza una hora entera y no 55 minutos
    # porque el valor agregado es «el último de la hora», y cuál sea ese último
    # no se sabe hasta que la hora termina.
    "metrics_1h": Contrato(
        tabla="metrics_1h",
        columna_fecha="ts",
        desplazamiento=timedelta(hours=1),
        porque="el agregado etiqueta con el inicio de la ventana; el último valor "
               "de la hora no se conoce hasta que la hora acaba",
    ),

    # El funding se liquida en `ts`. El *predicted funding* sí sería anterior,
    # pero no está en los dumps y no se usa.
    "funding": Contrato(
        tabla="funding",
        columna_fecha="ts",
        desplazamiento=timedelta(0),
        porque="`ts` es el instante de liquidación",
    ),

    # Nunca la fecha del artículo, que se edita. Solo el instante en que nuestro
    # colector lo capturó, que es lo único que no puede reescribir un tercero.
    "noticias": Contrato(
        tabla="noticias",
        columna_fecha="ts_captura",
        desplazamiento=timedelta(0),
        porque="`ts_captura` es nuestro y es incorruptible; la fecha del artículo no",
    ),

    # Saber que un símbolo existirá el mes que viene es leakage de universo: la
    # presencia de un mes solo se conoce cuando el mes ha pasado.
    "universo": Contrato(
        tabla="universo",
        columna_fecha="mes",
        desplazamiento=timedelta(days=31),
        porque="la presencia de un mes no se sabe hasta que el mes termina",
    ),
}


def disponible_en(df: pl.DataFrame, tabla: str) -> pl.DataFrame:
    """Añade `availability_time` a `df` según el contrato de `tabla`."""
    if tabla not in CONTRATOS:
        raise KeyError(
            f"`{tabla}` no tiene contrato temporal. Añádelo a CONTRATOS diciendo "
            f"cuándo se supo su dato; sin eso no se puede unir sin mirar al futuro. "
            f"Declaradas: {sorted(CONTRATOS)}")
    return CONTRATOS[tabla].aplicar(df)
