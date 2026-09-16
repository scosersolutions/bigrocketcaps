"""Estimación de la horquilla efectiva a partir de máximos y mínimos diarios.

Método de Corwin y Schultz (2012), *A Simple Way to Estimate Bid-Ask Spreads
from Daily High and Low Prices*, Journal of Finance 67(2).

## Por qué hace falta esto

Para el S&P 500 se puede suponer una horquilla de 5 puntos básicos y acertar.
Fuera del índice esa cifra es una fantasía: una microcap puede tener una
horquilla del 2 %, y un backtest que asuma 5 bps convertirá pérdidas en
ganancias por pura contabilidad.

Suponer un número sería inventarlo. Este estimador lo **deriva de los datos que
ya tengo**, sin necesidad de datos de libro de órdenes que no son gratis.

## La idea

El rango máximo-mínimo de un día contiene dos cosas: la volatilidad real del
activo y la horquilla. La volatilidad escala con el tiempo; la horquilla no —
está presente igual en un día que en dos. Comparando el rango de un día con el
de dos días consecutivos, ambas componentes se separan.

## Limitaciones, que son reales

- El estimador supone que el precio no salta entre sesiones. Con huecos de
  apertura grandes sobreestima, y las small caps tienen huecos grandes.
  **Medido en `tests/test_horquilla.py`**: sobre una serie sintética con
  horquilla real de cero y volatilidad diaria del 2 %, devuelve **0,65 %**.
  Ese es el suelo espurio, y crece con la volatilidad.
- Da estimaciones negativas cuando el ruido domina. Corwin y Schultz proponen
  truncarlas a cero antes de promediar, que es lo que se hace aquí.

## AVISO: el nivel absoluto NO es utilizable. Calibración fallida.

Contrastado el 2026-09-02 contra valores cuya horquilla real se conoce y ronda
1-2 puntos básicos:

    AAPL  10.540 M$/día   estimado 0,44 %   sobreestima x29
    MSFT   8.804 M$/día   estimado 0,39 %   sobreestima x26
    JPM    1.927 M$/día   estimado 0,42 %   sobreestima x28
    KO       917 M$/día   estimado 0,34 %   sobreestima x22

Y algo peor que el nivel: aplicado al universo de small caps asignó al quintil
**más ilíquido** la horquilla **más estrecha** (0,51 % frente a 1,00 % del
segundo). Eso está invertido. No está midiendo liquidez.

La conclusión es que sobre datos diarios con huecos —que es lo que hay fuera
del índice— el estimador captura sobre todo volatilidad nocturna, no horquilla.

**No usar para calcular costes.** En su lugar se reporta el *umbral de coste*
que anularía una ventaja, y se compara con el tamaño de posición frente al
volumen diario, que sí son magnitudes medibles sin suponer nada.

Se conserva el módulo por dos razones: la calibración que lo invalida es un
resultado que conviene no volver a descubrir desde cero, y el test que la fija
impide que alguien lo reintroduzca sin darse cuenta.
"""

from __future__ import annotations

import math

import polars as pl

#: Constante del artículo: 3 - 2·√2.
_K = 3 - 2 * math.sqrt(2)


def corwin_schultz(df: pl.DataFrame) -> float | None:
    """Horquilla efectiva media, en porcentaje, de una serie diaria OHLC.

    Espera columnas `high` y `low` ordenadas por fecha. Devuelve `None` si no
    hay pares de sesiones suficientes para estimar.
    """
    if df.height < 2:
        return None

    altos = df["high"].to_list()
    bajos = df["low"].to_list()

    estimaciones = []
    for i in range(len(altos) - 1):
        h1, l1, h2, l2 = altos[i], bajos[i], altos[i + 1], bajos[i + 1]
        if not all(x and x > 0 for x in (h1, l1, h2, l2)):
            continue

        # β: suma de los rangos logarítmicos de dos días por separado.
        beta = math.log(h1 / l1) ** 2 + math.log(h2 / l2) ** 2
        # γ: rango logarítmico del bloque de dos días tomado como uno solo.
        gamma = math.log(max(h1, h2) / min(l1, l2)) ** 2

        alfa = (math.sqrt(2 * beta) - math.sqrt(beta)) / _K - math.sqrt(gamma / _K)
        s = 2 * (math.exp(alfa) - 1) / (1 + math.exp(alfa))

        # Las negativas son ruido, no horquillas negativas. El artículo las
        # trunca a cero en vez de descartarlas: descartarlas dejaría solo las
        # sobreestimadas y sesgaría la media al alza.
        estimaciones.append(max(s, 0.0))

    if not estimaciones:
        return None
    return sum(estimaciones) / len(estimaciones) * 100


def coste_ida_y_vuelta(horquilla_pct: float, comision_pct: float = 0.01) -> float:
    """Coste total de abrir y cerrar, en porcentaje.

    Se cruza media horquilla al entrar y media al salir —una horquilla
    completa— más la comisión de cada lado.
    """
    return horquilla_pct + comision_pct * 2
