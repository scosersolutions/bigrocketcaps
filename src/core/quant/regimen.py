"""Clasificación del régimen de mercado por reglas.

Sin aprendizaje automático en v1, a propósito: un clasificador entrenado sobre
la misma serie con la que luego se backtestea es una fuente de sobreajuste
difícil de auditar. Reglas explícitas con umbrales visibles se pueden discutir,
testear y falsar.

Tres decisiones que importan:

- **Percentiles rodantes, nunca globales.** Comparar el ATR de hoy con el
  percentil de toda la serie mete el futuro en el pasado. Se usa la ventana
  previa y solo la previa. Consecuencia deliberada: el umbral **se adapta**, así
  que "alta volatilidad" siempre significa *alta respecto al pasado reciente*.
  Un periodo turbulento que se prolonga más que la ventana deja de señalarse
  como excepcional, porque para entonces ya es la nueva normalidad.

- **Histéresis de umbral (banda muerta).** Entrar en tendencia exige ADX > 25,
  pero salir exige caer por debajo de 20. Con un único umbral, el clasificador
  parpadea cada vez que el indicador roza la frontera: sobre BTC en 1h eso daba
  1.385 transiciones en 5 años, la mayoría rango↔tendencia sin que cambiara
  nada del mercado.

- **Histéresis temporal.** Además, un régimen candidato debe sostenerse varias
  velas antes de sustituir al vigente. Las dos histéresis atacan problemas
  distintos: la de umbral evita el parpadeo en la frontera, la temporal evita
  reaccionar a una vela aislada.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import polars as pl

from core.quant import indicators as ind


class Regimen(StrEnum):
    TENDENCIA_ALCISTA = "tendencia_alcista"
    TENDENCIA_BAJISTA = "tendencia_bajista"
    RANGO = "rango"
    ALTA_VOLATILIDAD = "alta_volatilidad"
    INDEFINIDO = "indefinido"


#: ADX para declarar tendencia. Umbral clásico de Wilder.
ADX_ENTRADA = 25.0

#: ADX por debajo del cual se abandona la tendencia. La distancia con el de
#: entrada es la banda muerta que evita el parpadeo.
ADX_SALIDA = 20.0

#: Percentil del ATR relativo que marca la alta volatilidad.
PERCENTIL_VOLATILIDAD = 0.85

#: Margen sobre el umbral de volatilidad. Sin él, una serie de volatilidad
#: constante empata con su propio percentil y el resultado lo decide el
#: redondeo en coma flotante.
MARGEN_VOLATILIDAD = 0.10

#: Ventana de los percentiles rodantes. 30 días en velas horarias.
VENTANA_PERCENTIL = 720

#: Velas que un régimen candidato debe sostenerse para reemplazar al vigente.
#: Calibrado sobre BTCUSDT 1h, 5 años, con un criterio estructural y no de
#: rentabilidad: que un régimen dure más que la operación típica. Medido:
#: 6 velas -> 43 de duración media (parpadeo), 12 -> 61, **24 -> 111 (~4,6
#: días)**, 48 -> 310 (demasiado lento para reaccionar a un cambio real).
#: El precio de la estabilidad es el retraso: ante un giro brusco, el
#: clasificador va hasta un día por detrás. Las estrategias deben contar con ello.
VELAS_CONFIRMACION = 24


def para_timeframe(velas_por_dia: int) -> "Parametros":
    """Parámetros equivalentes en tiempo real para otro timeframe.

    Regla mecánica, fijada en `docs/07-EXPERIMENTO-HORIZONTE.md`: todo
    parámetro expresado en velas se convierte al número de velas que cubre el
    mismo tiempo. Así el reajuste no puede usarse para elegir los valores que
    funcionan, que es el riesgo evidente al cambiar de horizonte.

    No todo se convierte. El ADX 14, el ATR 14 y la EMA 50 son parámetros de
    indicador, con valores canónicos que significan lo mismo en cualquier
    timeframe: una EMA 50 es una EMA 50 en horario y en diario. Convertirlos
    sería confundir la escala del indicador con la del tiempo.
    """
    return Parametros(
        ventana_percentil=max(30, 30 * velas_por_dia),
        velas_confirmacion=max(1, velas_por_dia),
    )


@dataclass(frozen=True)
class Parametros:
    periodos_adx: int = 14
    periodos_atr: int = 14
    periodos_ema: int = 50
    ventana_percentil: int = VENTANA_PERCENTIL
    adx_entrada: float = ADX_ENTRADA
    adx_salida: float = ADX_SALIDA
    percentil_volatilidad: float = PERCENTIL_VOLATILIDAD
    margen_volatilidad: float = MARGEN_VOLATILIDAD
    velas_confirmacion: int = VELAS_CONFIRMACION


def atr_relativo(periodos: int = 14) -> pl.Expr:
    """ATR como fracción del precio: comparable entre activos y entre épocas."""
    return ind.atr(periodos) / pl.col("close")


def _senales(df: pl.DataFrame, p: Parametros) -> pl.DataFrame:
    """Indicadores causales que alimentan la clasificación."""
    ema = ind.ema(p.periodos_ema)
    atr_rel = atr_relativo(p.periodos_atr)
    return df.select(
        ind.adx(p.periodos_adx).alias("adx"),
        atr_rel.alias("atr_rel"),
        atr_rel.rolling_quantile(
            quantile=p.percentil_volatilidad, window_size=p.ventana_percentil
        ).alias("umbral_vol"),
        (ema - ema.shift(p.periodos_ema)).alias("pendiente"),
    )


def _candidato(
    adx: float,
    atr_rel: float,
    umbral_vol: float,
    pendiente: float,
    vigente: str,
    p: Parametros,
) -> str:
    """Régimen que sugieren los indicadores, con umbrales que dependen del vigente."""
    en_alta_vol = vigente == Regimen.ALTA_VOLATILIDAD.value
    # Cuesta más entrar en alta volatilidad que quedarse en ella.
    factor = (1 - p.margen_volatilidad) if en_alta_vol else (1 + p.margen_volatilidad)
    if atr_rel > umbral_vol * factor:
        return Regimen.ALTA_VOLATILIDAD.value

    en_tendencia = vigente in (
        Regimen.TENDENCIA_ALCISTA.value,
        Regimen.TENDENCIA_BAJISTA.value,
    )
    umbral_adx = p.adx_salida if en_tendencia else p.adx_entrada
    if adx > umbral_adx:
        if pendiente > 0:
            return Regimen.TENDENCIA_ALCISTA.value
        if pendiente < 0:
            return Regimen.TENDENCIA_BAJISTA.value
    return Regimen.RANGO.value


def clasificar(df: pl.DataFrame, parametros: Parametros | None = None, **ajustes) -> pl.Series:
    """Devuelve la serie de regímenes, alineada con `df`.

    Recorre la serie una sola vez y hacia delante, así que es causal por
    construcción: el régimen en `t` no puede depender de `t+1`.
    """
    p = parametros or Parametros(**ajustes)
    senales = _senales(df, p)

    vigente = Regimen.INDEFINIDO.value
    candidato: str | None = None
    repeticiones = 0
    salida: list[str] = []

    for fila in senales.iter_rows(named=True):
        if any(
            fila[c] is None for c in ("adx", "atr_rel", "umbral_vol", "pendiente")
        ):
            salida.append(Regimen.INDEFINIDO.value)
            continue

        actual = _candidato(
            fila["adx"], fila["atr_rel"], fila["umbral_vol"], fila["pendiente"], vigente, p
        )

        if vigente == Regimen.INDEFINIDO.value:
            # Salir de INDEFINIDO no exige confirmación: no es un régimen, es la
            # ausencia de datos suficientes para clasificar.
            vigente, candidato, repeticiones = actual, None, 0
        elif actual == vigente:
            candidato, repeticiones = None, 0
        elif actual == candidato:
            repeticiones += 1
            if repeticiones >= p.velas_confirmacion:
                vigente, candidato, repeticiones = actual, None, 0
        else:
            candidato, repeticiones = actual, 1

        salida.append(vigente)

    return pl.Series("regimen", salida)


def resumen(regimenes: pl.Series) -> dict[str, float]:
    """Reparto porcentual de cada régimen. Sirve para comprobar que el
    clasificador no colapsa en una sola etiqueta."""
    total = regimenes.len()
    if total == 0:
        return {}
    conteo = regimenes.value_counts().sort("count", descending=True)
    return {fila["regimen"]: fila["count"] / total for fila in conteo.iter_rows(named=True)}


def cambios(regimenes: pl.Series) -> int:
    """Número de transiciones. Si es enorme, la histéresis no está funcionando."""
    if regimenes.len() < 2:
        return 0
    return int((regimenes != regimenes.shift(1)).sum())


def duracion_media(regimenes: pl.Series) -> float:
    """Velas que dura un régimen de media. Un valor bajo delata parpadeo."""
    n = cambios(regimenes)
    return float(regimenes.len()) if n == 0 else regimenes.len() / n
