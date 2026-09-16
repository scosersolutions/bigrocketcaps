"""Cuándo estuvo disponible cada dato, y cómo unir sin mirar al futuro.

## El problema

Un backtest que usa un dato antes de que existiera no mide una estrategia:
mide el futuro. Y el error no se ve, porque el resultado sale mejor.

El caso concreto que motivó este módulo vive en `binance_metrics.a_horario()`.
Agrega las observaciones de 5 minutos a velas horarias tomando la última de
cada hora, que es lo correcto. Pero `group_by_dynamic` etiqueta cada grupo con
el **inicio** de la ventana, así que la observación de las **09:55** sale con
`ts = 09:00`.

Unirla por `ts` a la vela de precio que CIERRA a las 09:00 usa un dato de las
09:55 para decidir a las 09:00: **55 minutos de futuro**, y el `join` no se
queja. Unirla a la que cierra a las 10:00 es correcto. Nada en el tipo de dato
distingue las dos cosas.

## La regla

    availability_time <= observation_time < execution_time

`availability_time` es cuándo lo habríamos tenido nosotros, no cuándo ocurrió
el hecho ni cuándo lo fechó la fuente. Es columna obligatoria, no opcional.

## Cómo se usa

`unir_causal` es el único camino de unión permitido para features. Hace
`join_asof` hacia atrás sobre `availability_time` y comprueba el resultado; un
`join` normal por `ts` está prohibido porque no puede saber si el `ts` de la
derecha es un instante de disponibilidad o una etiqueta de ventana.
"""

from core.tiempo.contratos import (
    Contrato,
    CONTRATOS,
    FuturoEnLosDatos,
    disponible_en,
)
from core.tiempo.union import unir_causal

__all__ = [
    "Contrato",
    "CONTRATOS",
    "FuturoEnLosDatos",
    "disponible_en",
    "unir_causal",
]
