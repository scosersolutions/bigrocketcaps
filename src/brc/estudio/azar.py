"""Contraste contra el azar para estudios de eventos.

Mismo principio que `core.backtest.validacion._percentil_montecarlo`: cuantas
carteras aleatorias equivalentes quedan peor que la real. "Equivalente" aqui es
mismo n, mismo horizonte, mismo universo de precios y misma mecanica
(`estudio.eventos.medir`); lo unico que cambia es la fecha de entrada, sorteada
como una fila real de `precios` para que todo sorteo caiga en una combinacion
ticker-fecha que de verdad cotizo.

No se usa `validacion.validar()` para esto: su motor entra siempre en la
apertura de la vela siguiente a la señal y no puede reproducir la entrada al
cierre de la propia sesion que exige un estudio de eventos. Ver
`docs/15-PLAN-BIGROCKETCAPS.md` FASE 6 para los mismos umbrales aplicados alli.
"""
from __future__ import annotations

import random

import polars as pl

from brc.estudio.eventos import EstudioError, medir


def percentil_contra_azar(
    n_eventos: int,
    precios: pl.DataFrame,
    *,
    horizonte: int,
    exceso_real: float,
    direccion: str,
    simulaciones: int = 200,
    semilla: int = 0,
) -> float:
    """Fraccion de conjuntos de eventos aleatorios que quedan peor que el real.

    `direccion` es "corto" (la hipotesis predice exceso negativo: superar el
    azar es ser MAS negativo que el azar) o "largo" (al reves). Las fechas de
    entrada se sortean como filas de `precios`, no como pares ticker/fecha
    independientes: eso garantiza que cada sorteo caiga en una combinacion que
    de verdad existe, en vez de diluirse en descartes silenciosos de un ticker
    pedido en una fecha en la que no cotizaba.
    """
    if direccion not in ("corto", "largo"):
        raise ValueError('direccion debe ser "corto" o "largo"')
    if n_eventos <= 0:
        raise ValueError("n_eventos debe ser positivo")

    filas = precios.select(pl.col("activo"), pl.col("fecha"))
    if filas.is_empty():
        return 0.0

    peor_o_igual = 0
    validos = 0
    for i in range(simulaciones):
        rnd = random.Random(semilla + i)
        indices = [rnd.randrange(filas.height) for _ in range(n_eventos)]
        muestra = filas[indices]
        pseudo = pl.DataFrame({
            "ticker": muestra["activo"],
            "presentado": muestra["fecha"],
            "tras_cierre": [False] * n_eventos,
        })
        try:
            r = medir(pseudo, precios, horizonte=horizonte)
        except EstudioError:
            continue
        validos += 1
        if direccion == "corto":
            peor_o_igual += r.exceso_medio_pct > exceso_real
        else:
            peor_o_igual += r.exceso_medio_pct < exceso_real

    return peor_o_igual / validos * 100 if validos else 0.0
