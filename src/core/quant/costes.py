"""Modelo de costes de transacción.

Un backtest sin costes no es un backtest optimista: es un backtest de otra
estrategia distinta, que no se puede ejecutar. Por eso `validar_modelo()`
rechaza un modelo con todos los componentes a cero, y el motor de backtest lo
invoca antes de simular nada.

Tarifas verificadas el 2026-09-02, no recordadas:

| Mercado | Maker | Taker | Fuente |
|---|---|---|---|
| Kraken perpetuos (EEE) | 0,0200 % | 0,0500 % | Documentación EEE y API `/feeschedules` |
| Kraken spot | 0,25 % | 0,40 % | Fee schedule público |
| IBKR acciones (tiered) | 0,0035 USD/acción, mínimo 0,35 USD, máximo 1 % | | Página de comisiones |

La diferencia entre perpetuos y spot decide qué se puede operar: 0,10 % de ida y
vuelta frente a 0,80 %. Sobre un objetivo del 3,5 %, el primero consume el 2,9 %
del beneficio y el segundo el 23 %, por encima del límite de rechazo del 15 %.
Es la razón cuantitativa de que el camino crítico sean los perpetuos.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import polars as pl

#: Spread por defecto en puntos básicos. Medido el 2026-09-02 en Kraken:
#: 0,13 bps en PF_XBTUSD y 0,41 bps en PF_ETHUSD, ambos de un solo tick.
#: Se usa un valor deliberadamente peor porque esa medición es de un momento
#: de calma, y el spread se ensancha justo cuando más operaciones se disparan.
#: Es una **suposición**, no una medición histórica: el backtest debe incluir
#: análisis de sensibilidad sobre este parámetro.
SPREAD_BPS_POR_DEFECTO = 2.0

#: Coeficiente de impacto de mercado en el modelo de raíz cuadrada.
#: Con capital pequeño frente al volumen de BTC el término es despreciable;
#: se mantiene para que deje de serlo si el capital crece.
IMPACTO_K = 0.1


class CostesError(ValueError):
    """Modelo de costes mal configurado."""


@dataclass(frozen=True)
class Desglose:
    """Costes de una operación completa, en unidades monetarias."""

    comision_entrada: float = 0.0
    comision_salida: float = 0.0
    slippage_entrada: float = 0.0
    slippage_salida: float = 0.0
    funding: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.comision_entrada
            + self.comision_salida
            + self.slippage_entrada
            + self.slippage_salida
            + self.funding
        )

    def como_pct(self, nocional: float) -> float:
        if nocional <= 0:
            raise CostesError("nocional debe ser positivo")
        return self.total / nocional

    def __str__(self) -> str:
        return (
            f"comisiones={self.comision_entrada + self.comision_salida:.4f} "
            f"slippage={self.slippage_entrada + self.slippage_salida:.4f} "
            f"funding={self.funding:+.4f} total={self.total:.4f}"
        )


class ModeloCostes(ABC):
    """Contrato común. Todo backtest recibe una implementación explícita."""

    @abstractmethod
    def comision(self, nocional: float, *, maker: bool) -> float:
        """Comisión de una ejecución, en unidades monetarias."""

    @abstractmethod
    def slippage(self, nocional: float, *, volumen_vela: float = 0.0) -> float:
        """Deslizamiento estimado de una ejecución."""

    #: ¿El spread de este modelo se midió, o se supuso? Lo declara quien
    #: construye el modelo; el juez pregunta en vez de confiar en que quien
    #: llama haya elegido bien.
    #:
    #: Medido el 2026-09-16 con nocional 10.000 y RR 2: el supuesto de 2,0 bps
    #: se queda CORTO en cinco de los siete perpetuos con datos —hasta un 53 %
    #: en BNB, 18,34 $ reales frente a 12,00 supuestos—. Y no es contabilidad
    #: fina: con stops de 0,41 % a 0,61 %, que es lo que dan 1,0-1,5 ATR en
    #: velas de 15m, el coste REAL pasa del 15 % del objetivo y refuta la
    #: estrategia mientras el supuesto la deja pasar. Puede fabricar un falso
    #: positivo, que es el error que este proyecto existe para no cometer.
    spread_medido: bool = False
    origen_spread: str = "supuesto"

    def componentes_no_nulos(self) -> bool:
        """Si es falso, el modelo no cobra nada y el backtest debe rechazarlo."""
        referencia = 10_000.0
        return (
            self.comision(referencia, maker=False) > 0
            or self.comision(referencia, maker=True) > 0
            or self.slippage(referencia) > 0
        )

    def operacion(
        self,
        nocional: float,
        *,
        entrada_maker: bool = False,
        salida_maker: bool = False,
        funding: float = 0.0,
        volumen_vela: float = 0.0,
    ) -> Desglose:
        """Coste de ida y vuelta, incluido el funding ya calculado."""
        if nocional <= 0:
            raise CostesError("nocional debe ser positivo")
        return Desglose(
            comision_entrada=self.comision(nocional, maker=entrada_maker),
            comision_salida=self.comision(nocional, maker=salida_maker),
            slippage_entrada=self.slippage(nocional, volumen_vela=volumen_vela),
            slippage_salida=self.slippage(nocional, volumen_vela=volumen_vela),
            funding=funding,
        )


@dataclass(frozen=True)
class CostesPorcentuales(ModeloCostes):
    """Comisión proporcional al nocional: cripto, spot y perpetuos."""

    maker_pct: float
    taker_pct: float
    spread_bps: float = SPREAD_BPS_POR_DEFECTO
    impacto_k: float = IMPACTO_K
    spread_medido: bool = False
    origen_spread: str = "supuesto"

    def __post_init__(self) -> None:
        if self.maker_pct < 0 or self.taker_pct < 0:
            raise CostesError("las comisiones no pueden ser negativas en este modelo")
        if self.spread_bps < 0:
            raise CostesError("el spread no puede ser negativo")

    def comision(self, nocional: float, *, maker: bool) -> float:
        return nocional * (self.maker_pct if maker else self.taker_pct)

    def slippage(self, nocional: float, *, volumen_vela: float = 0.0) -> float:
        # Se cruza medio spread al ejecutar contra el libro.
        coste = nocional * (self.spread_bps / 2.0) / 10_000.0
        # Impacto de mercado: modelo de raíz cuadrada sobre la fracción del
        # volumen de la vela que representa la orden.
        if volumen_vela > 0:
            coste += nocional * self.impacto_k * (nocional / volumen_vela) ** 0.5
        return coste


#: Perpetuos de Kraken para clientes del EEE, tramo base.
KRAKEN_PERPETUOS = CostesPorcentuales(maker_pct=0.000200, taker_pct=0.000500)

#: Spot de Kraken, tramo base. Ocho veces más caro que los perpetuos.
KRAKEN_SPOT = CostesPorcentuales(maker_pct=0.0025, taker_pct=0.0040, spread_bps=5.0)


@dataclass(frozen=True)
class CostesPorAccion(ModeloCostes):
    """Comisión por acción con mínimo y tope: estructura de IBKR.

    El mínimo por orden es lo que hace inviable operar acciones con capital
    pequeño: con posiciones de 100 EUR, 0,35 USD de ida y vuelta se lleva el
    47 % del objetivo. Ver C1 en `docs/03-REVISION-DISENO.md`.
    """

    por_accion: float = 0.0035
    minimo_orden: float = 0.35
    maximo_pct: float = 0.01
    spread_bps: float = 3.0
    impacto_k: float = IMPACTO_K
    precio_referencia: float = 100.0
    spread_medido: bool = False
    origen_spread: str = "supuesto"

    def __post_init__(self) -> None:
        if self.por_accion < 0 or self.minimo_orden < 0:
            raise CostesError("comisiones negativas no tienen sentido aquí")
        if self.precio_referencia <= 0:
            raise CostesError("precio_referencia debe ser positivo")

    def comision(self, nocional: float, *, maker: bool = False) -> float:
        acciones = nocional / self.precio_referencia
        bruta = max(acciones * self.por_accion, self.minimo_orden)
        return min(bruta, nocional * self.maximo_pct)

    def slippage(self, nocional: float, *, volumen_vela: float = 0.0) -> float:
        coste = nocional * (self.spread_bps / 2.0) / 10_000.0
        if volumen_vela > 0:
            coste += nocional * self.impacto_k * (nocional / volumen_vela) ** 0.5
        return coste


IBKR_TIERED = CostesPorAccion()
IBKR_FIXED = CostesPorAccion(por_accion=0.005, minimo_orden=1.0)


# =============================================================================
# Impacto calibrado contra la profundidad real del libro
#
# `CostesPorcentuales` cobra impacto como k·(N/volumen_vela)^0.5, y tiene un
# defecto que no se ve hasta que se escribe: **la fracción depende de la
# longitud de la barra**. La misma orden de 10.000 USD sobre SOLUSDT paga 4,7
# USD en velas diarias, 25,4 en horarias y 53,2 en velas de 15 minutos, porque
# el denominador se encoge con la vela. El impacto de una orden no depende de
# qué gráfico mire quien la manda: ese modelo no mide impacto, mide la
# resolución del backtest.
#
# El dump `bookDepth` de Binance publica desde 2023-01 la profundidad acumulada
# hasta ±1..5 % del punto medio. Con eso el impacto deja de ser un parámetro
# inventado y pasa a salir del libro:
#
#     desplazamiento% = (N / D1) ^ exponente        D1 = notional dentro del 1 %
#
# El exponente es lo único que queda por decidir, y hay dos respuestas
# defendibles. Por eso es un parámetro y no una constante:
#
#   EXPONENTE_UNIFORME = 1.0   Supone el libro repartido de forma uniforme
#                              dentro de la banda. Es la cuenta más simple y es
#                              PESIMISTA para órdenes pequeñas, porque los
#                              libros reales están más cargados cerca del medio.
#                              Es el valor por defecto: cuando hay duda, el
#                              coste se sobreestima, no al revés.
#
#   EXPONENTE_AJUSTADO = 1.434 Medido. Ajustando δ% = a·N^b sobre los cinco
#                              niveles de profundidad, en tres días de 2023,
#                              2024 y 2026, sale 1,43 (rango 1,02–1,68 por
#                              activo) con R² de 0,89 a 0,997. Nunca 0,5, que
#                              es lo que supone el modelo viejo.
#
# Los dos conviven a propósito, y ninguno sustituye a `CostesPorcentuales`:
# cambiar el modelo sin más habría movido en silencio los resultados de todo lo
# medido hasta ahora. Lo que se quiere es poder correr un veredicto con los tres
# y ver cuánto depende de esta elección.
#
# LÍMITES, que deciden cómo leerlo:
#
#   1. Calibrado entre el 1 % y el 5 % de desplazamiento, que es donde hay
#      datos. Una orden normal mueve el precio mucho menos del 1 %, así que ahí
#      el modelo EXTRAPOLA. Es justo el rango en que se opera: el número para
#      nocionales pequeños es una extensión de la curva, no una medición.
#   2. `bookDepth` empieza en 2023-01: no cubre el epoch A (2021-2022), que fue
#      más fino que lo medido.
#   3. Es Binance. La ejecución es en Kraken, cuyo libro es otro y casi con
#      seguridad peor. Sigue siendo trabajo de `crosscheck`.
# =============================================================================

#: Libro repartido de forma uniforme dentro de la banda. Pesimista y por
#: defecto: ante la duda, el coste se sobreestima.
EXPONENTE_UNIFORME = 1.0

#: Ajustado sobre los cinco niveles de `bookDepth`. Se usa uno solo para todos
#: los activos y no el de cada uno: siete exponentes ajustados sobre cinco
#: puntos cada uno es más sobreajuste que precisión.
EXPONENTE_AJUSTADO = 1.434

#: Nocional en USD dentro del 1 % del punto medio, por activo. Lo produce
#: `scripts/medir_profundidad.py`, que toma el lado FINO de cada instantánea
#: —una orden se ejecuta contra un solo lado— y se queda con el percentil 10 y
#: no con la mediana, porque los días de libro fino son justo los días en que
#: estas estrategias disparan.
#:
#: Medido el 2026-09-05 sobre seis días repartidos entre 2023 y 2026. Eran
#: siete: el 2025-10-11 se descarta entero porque sus dumps traen la
#: profundidad congelada, y colarlo daba 4.859 USD para ADAUSDT y 820 para
#: HYPEUSDT —cifras imposibles que habrían calibrado el modelo con una liquidez
#: que no existió. Ver `docs/research/impacto-mercado.md`.
PROFUNDIDAD_1PCT: dict[str, float] = {
    "BTCUSDT": 50_554_357.0,
    "ETHUSDT": 22_305_176.0,
    "SOLUSDT": 2_026_864.0,
    "BNBUSDT": 2_054_832.0,
    "DOGEUSDT": 2_095_203.0,
    "XRPUSDT": 1_820_005.0,
    "ADAUSDT": 894_268.0,
    # Un solo día válido: listado en 2025-05 y con el otro día descartado por el
    # control de calidad. Se incluye porque C1 lo opera, pero su cifra descansa
    # sobre una base mucho más fina que la de los demás.
    "HYPEUSDT": 2_905_997.0,
}


@dataclass(frozen=True)
class CostesLibro(ModeloCostes):
    """Comisión proporcional e impacto calibrado con la profundidad del libro.

    El impacto se deriva de barrer el libro, no de una fórmula supuesta. Si
    consumir un nocional `d1` desplaza el precio un 1 %, consumir `N` lo
    desplaza `(N/d1)^b` por ciento.

    Pero la orden no se ejecuta al precio final, sino al medio del recorrido.
    Integrando el desplazamiento a lo largo del barrido, el coste medio es ese
    desplazamiento dividido por `b + 1` —con b = 1,43, unas 2,4 veces menor que
    el desplazamiento final—. Cobrar el precio final sería cobrar de más.
    """

    maker_pct: float
    taker_pct: float
    profundidad_1pct: float
    spread_bps: float = SPREAD_BPS_POR_DEFECTO
    exponente: float = EXPONENTE_UNIFORME
    spread_medido: bool = False
    origen_spread: str = "supuesto"

    def __post_init__(self) -> None:
        if self.maker_pct < 0 or self.taker_pct < 0:
            raise CostesError("las comisiones no pueden ser negativas en este modelo")
        if self.spread_bps < 0:
            raise CostesError("el spread no puede ser negativo")
        if self.profundidad_1pct <= 0:
            raise CostesError(
                "la profundidad al 1 % debe ser positiva: sin ella no hay impacto que calcular")

    def comision(self, nocional: float, *, maker: bool) -> float:
        return nocional * (self.maker_pct if maker else self.taker_pct)

    def slippage(self, nocional: float, *, volumen_vela: float = 0.0) -> float:
        # Medio spread al cruzar, igual que en el modelo porcentual.
        coste = nocional * (self.spread_bps / 2.0) / 10_000.0
        if nocional > 0:
            desplazamiento_pct = (nocional / self.profundidad_1pct) ** self.exponente
            medio_pct = desplazamiento_pct / (self.exponente + 1.0)
            coste += nocional * medio_pct / 100.0
        return coste

    def desplazamiento_pct(self, nocional: float) -> float:
        """Cuánto movería el precio esta orden, en porcentaje. Para diagnóstico."""
        return (nocional / self.profundidad_1pct) ** self.exponente


def kraken_libro(activo: str) -> CostesLibro:
    """Modelo con la profundidad medida de ese activo.

    Falla si el activo no está calibrado, en vez de caer en una profundidad por
    defecto: un impacto calculado con la liquidez de otro activo es peor que no
    calcularlo, porque parece medido.
    """
    if activo not in PROFUNDIDAD_1PCT:
        raise CostesError(
            f"{activo} no tiene profundidad calibrada. Mídela con "
            f"`python scripts/medir_profundidad.py --activos {activo}` y añádela "
            "a PROFUNDIDAD_1PCT; no se usa la de otro activo.")
    return CostesLibro(maker_pct=0.000200, taker_pct=0.000500,
                       profundidad_1pct=PROFUNDIDAD_1PCT[activo])


def validar_modelo(modelo: ModeloCostes | None) -> ModeloCostes:
    """Puerta de entrada al backtest. Sin costes reales no se simula.

    Un backtest con costes a cero mide una estrategia que nadie puede ejecutar,
    y es la forma más rápida de convencerse de que existe un edge inexistente.
    """
    if modelo is None:
        raise CostesError("el backtest exige un modelo de costes explícito")
    if not modelo.componentes_no_nulos():
        raise CostesError(
            "el modelo de costes no cobra nada: un backtest sin costes no es válido"
        )
    return modelo


def coste_funding(
    funding: pl.DataFrame,
    desde,
    hasta,
    *,
    nocional: float,
    es_largo: bool,
) -> float:
    """Funding acumulado durante la vida de una posición en perpetuos.

    Signo: un funding positivo lo **paga** el largo y lo **cobra** el corto.
    Devuelve un coste, así que sale positivo cuando resta al resultado.

    Ignorar esto invalida cualquier backtest de perpetuos: con la mediana
    histórica en +7,2 % anualizado, una posición larga mantenida paga una
    cantidad comparable al objetivo de muchas operaciones.
    """
    if nocional <= 0:
        raise CostesError("nocional debe ser positivo")
    if desde > hasta:
        raise CostesError("el rango temporal está invertido")

    # Intervalo semiabierto (desde, hasta]: se paga el funding liquidado
    # mientras la posición estaba abierta, no el del instante de entrada.
    tramo = funding.filter((pl.col("ts") > desde) & (pl.col("ts") <= hasta))
    if tramo.is_empty():
        return 0.0

    acumulado = float(tramo["rate"].sum())
    return nocional * acumulado * (1.0 if es_largo else -1.0)
