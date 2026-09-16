"""Indicadores técnicos deterministas y causales.

Dos invariantes que el resto del sistema da por supuestos:

1. **Causalidad.** El valor en el índice `t` depende solo de datos hasta `t`.
   Nada de centrados, rellenos hacia atrás ni normalizaciones sobre la serie
   completa. `tests/test_indicators.py` lo comprueba automáticamente corrompiendo
   el futuro y verificando que el pasado no se mueve. Es el fallo que más
   backtests infla y el más difícil de ver leyendo el código.

2. **Periodo de calentamiento explícito.** Mientras el indicador no está formado,
   el valor es `null`, no una aproximación. Un RSI calculado con tres velas no es
   un RSI malo: es un número sin significado, y debe ser imposible operar con él.

Los suavizados siguen el método de Wilder (alpha = 1/n), que es lo que usan las
definiciones originales de RSI, ATR y ADX.
"""

from __future__ import annotations

import polars as pl

__all__ = [
    "sma",
    "ema",
    "wilder",
    "true_range",
    "atr",
    "rsi",
    "adx",
    "vwap_rolling",
    "volumen_relativo",
    "volatilidad_realizada",
    "retornos_log",
]


def _invalidar_calentamiento(expr: pl.Expr, periodos: int) -> pl.Expr:
    """Deja en null los valores previos a que el indicador esté formado."""
    return pl.when(pl.int_range(pl.len()) >= periodos).then(expr).otherwise(None)


def retornos_log(col: str = "close") -> pl.Expr:
    return pl.col(col).log() - pl.col(col).shift(1).log()


def sma(periodos: int, col: str = "close") -> pl.Expr:
    return pl.col(col).rolling_mean(window_size=periodos)


def ema(periodos: int, col: str = "close") -> pl.Expr:
    """EMA clásica (alpha = 2/(n+1)), sin corrección de sesgo inicial."""
    return _invalidar_calentamiento(
        pl.col(col).ewm_mean(span=periodos, adjust=False), periodos - 1
    )


def wilder(expr: pl.Expr, periodos: int) -> pl.Expr:
    """Suavizado de Wilder: alpha = 1/n. No es lo mismo que una EMA de n."""
    return expr.ewm_mean(alpha=1.0 / periodos, adjust=False)


def true_range() -> pl.Expr:
    cierre_previo = pl.col("close").shift(1)
    return pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - cierre_previo).abs(),
        (pl.col("low") - cierre_previo).abs(),
    )


def atr(periodos: int = 14) -> pl.Expr:
    return _invalidar_calentamiento(wilder(true_range(), periodos), periodos)


def rsi(periodos: int = 14, col: str = "close") -> pl.Expr:
    delta = pl.col(col).diff()
    ganancia = wilder(pl.when(delta > 0).then(delta).otherwise(0.0), periodos)
    perdida = wilder(pl.when(delta < 0).then(-delta).otherwise(0.0), periodos)
    # Sin pérdidas en la ventana el RSI es 100 por definición; evita dividir por cero.
    valor = (
        pl.when(perdida == 0)
        .then(100.0)
        .otherwise(100.0 - 100.0 / (1.0 + ganancia / perdida))
    )
    return _invalidar_calentamiento(valor, periodos)


def _movimiento_direccional() -> tuple[pl.Expr, pl.Expr]:
    subida = pl.col("high") - pl.col("high").shift(1)
    bajada = pl.col("low").shift(1) - pl.col("low")
    mas = pl.when((subida > bajada) & (subida > 0)).then(subida).otherwise(0.0)
    menos = pl.when((bajada > subida) & (bajada > 0)).then(bajada).otherwise(0.0)
    return mas, menos


def adx(periodos: int = 14) -> pl.Expr:
    """Fuerza de la tendencia, sin dirección. Alimenta el detector de régimen."""
    mas_dm, menos_dm = _movimiento_direccional()
    atr_suave = wilder(true_range(), periodos)
    mas_di = 100.0 * wilder(mas_dm, periodos) / atr_suave
    menos_di = 100.0 * wilder(menos_dm, periodos) / atr_suave
    suma = mas_di + menos_di
    dx = pl.when(suma == 0).then(0.0).otherwise(100.0 * (mas_di - menos_di).abs() / suma)
    # ADX necesita dos ventanas: una para el DX y otra para suavizarlo.
    return _invalidar_calentamiento(wilder(dx, periodos), periodos * 2)


def vwap_rolling(periodos: int = 24) -> pl.Expr:
    """VWAP sobre ventana móvil.

    En cripto no hay sesión diaria que reiniciar, así que un VWAP anclado a la
    apertura no tiene sentido. La ventana móvil sí.
    """
    tipico = (pl.col("high") + pl.col("low") + pl.col("close")) / 3.0
    return _invalidar_calentamiento(
        (tipico * pl.col("volume")).rolling_sum(window_size=periodos)
        / pl.col("volume").rolling_sum(window_size=periodos),
        periodos - 1,
    )


def volumen_relativo(periodos: int = 20) -> pl.Expr:
    """Volumen actual frente a su media. Sustituye al sentimiento social:
    mide flujo real en vez de opiniones."""
    media = pl.col("volume").rolling_mean(window_size=periodos)
    return _invalidar_calentamiento(
        pl.when(media == 0).then(None).otherwise(pl.col("volume") / media), periodos - 1
    )


def volatilidad_realizada(periodos: int = 24, velas_por_anio: int = 24 * 365) -> pl.Expr:
    """Desviación típica de retornos log, anualizada.

    `velas_por_anio` debe corresponder al timeframe: 24*365 en velas horarias de
    cripto, 365 en diarias de cripto, 252 en diarias de acciones.
    """
    return _invalidar_calentamiento(
        retornos_log().rolling_std(window_size=periodos) * (velas_por_anio**0.5),
        periodos,
    )
