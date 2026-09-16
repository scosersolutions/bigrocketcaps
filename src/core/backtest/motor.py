"""Motor de backtest event-driven.

Por qué propio y no vectorbt, que era lo previsto en el plan: las señales de
MoonRocket llevan stop y objetivo propios de cada operación, el funding se
acumula por posición y el resultado tiene que separarse en PnL de precio y PnL
de funding. Encajar eso en `from_signals` sería gimnasia sobre una abstracción
que no está pensada para ello, y añadiría suposiciones ajenas justo en la capa
que debe ser auditable. Con 43.000 velas y unas 700 señales, un bucle explícito
tarda segundos. vectorbt sigue siendo la herramienta correcta para los barridos
masivos de parámetros de F5.

Suposiciones de ejecución, todas conservadoras y todas explícitas:

1. **La entrada ocurre en la apertura de la vela siguiente** a la señal. Entrar
   al cierre de la vela que dispara la señal regala un precio que nadie pudo
   obtener y es la forma más común de inflar un backtest.

2. **Si una vela toca stop y objetivo, se asume el stop.** Sin datos de tick no
   se puede saber cuál llegó antes, y suponer lo contrario convierte cada vela
   ancha en un acierto.

3. **Los huecos se ejecutan al precio de apertura, no al del nivel.** Si el
   mercado abre más allá del stop, la pérdida es mayor que la planificada. Esto
   es lo que de verdad ocurre y lo que un backtest ingenuo se salta.

4. **Una posición abierta por estrategia y activo.** Las señales que llegan con
   una posición viva se descartan y se cuentan, en vez de solaparse.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum

import polars as pl

from core.quant.costes import ModeloCostes, coste_funding, validar_modelo
from core.quant.estrategias import Direccion


class MotivoSalida(StrEnum):
    OBJETIVO = "objetivo"
    STOP = "stop"
    TIEMPO = "tiempo"
    FIN_DATOS = "fin_datos"
    BREAK_EVEN = "break_even"
    TRAILING = "trailing"


@dataclass(frozen=True)
class Gestion:
    """Reglas de gestión de la posición abierta.

    Existen porque las cuatro primeras hipótesis compartían una mecánica fija
    que resultó dominar el resultado: con stop a 2 ATR y objetivo a 2,5 R, el
    win rate quedaba en torno al 30 % viniera la entrada de donde viniera.

    Los valores por defecto reproducen exactamente esa mecánica, para que S0
    sirva de referencia comparable.
    """

    #: Al alcanzar este múltiplo de R, el stop sube al precio de entrada.
    break_even_en_r: float | None = None

    #: Al alcanzar este múltiplo de R, se activa el trailing.
    trailing_desde_r: float | None = None

    #: Distancia del trailing, en múltiplos de R. Con el stop a 2 ATR, 0,75 R
    #: equivale a 1,5 ATR; se expresa en R para no depender de otra columna.
    trailing_r: float = 0.75

    #: Si es falso, no hay objetivo fijo: se sale por stop, trailing o tiempo.
    usar_objetivo: bool = True

    max_barras: int = 24 * 14

    #: Posiciones simultáneas por estrategia y activo.
    max_posiciones: int = 1

    def __post_init__(self) -> None:
        for campo in ("break_even_en_r", "trailing_desde_r"):
            valor = getattr(self, campo)
            if valor is not None and valor <= 0:
                raise BacktestError(f"{campo} debe ser positivo")
        if self.max_barras < 1:
            raise BacktestError("max_barras debe ser al menos 1")
        if self.max_posiciones < 1:
            raise BacktestError("max_posiciones debe ser al menos 1")
        if not self.usar_objetivo and self.trailing_desde_r is None:
            raise BacktestError(
                "sin objetivo y sin trailing la posición solo saldría por stop o tiempo"
            )


class BacktestError(ValueError):
    """Configuración o datos incompatibles con la simulación."""


@dataclass(frozen=True)
class Operacion:
    activo: str
    estrategia: str
    direccion: str
    ts_senal: datetime
    ts_entrada: datetime
    ts_salida: datetime
    precio_entrada: float
    precio_salida: float
    stop: float
    objetivo: float
    nocional: float
    motivo: str
    barras: int
    pnl_precio: float
    pnl_funding: float
    costes: float

    @property
    def pnl_neto(self) -> float:
        return self.pnl_precio - self.pnl_funding - self.costes

    @property
    def pnl_sin_funding(self) -> float:
        """PnL ignorando el funding. Si el resultado solo existe con funding,
        la estrategia es de carry, no direccional."""
        return self.pnl_precio - self.costes

    @property
    def riesgo_planificado(self) -> float:
        return abs(self.precio_entrada - self.stop) * self.nocional / self.precio_entrada

    @property
    def r_multiple(self) -> float:
        r = self.riesgo_planificado
        return self.pnl_neto / r if r > 0 else 0.0


@dataclass
class Resultado:
    operaciones: list[Operacion] = field(default_factory=list)
    senales_descartadas: int = 0
    senales_totales: int = 0

    @property
    def n(self) -> int:
        return len(self.operaciones)

    def a_dataframe(self) -> pl.DataFrame:
        if not self.operaciones:
            return pl.DataFrame(
                schema={
                    "activo": pl.String,
                    "estrategia": pl.String,
                    "direccion": pl.String,
                    "ts_senal": pl.Datetime("us", "UTC"),
                    "ts_entrada": pl.Datetime("us", "UTC"),
                    "ts_salida": pl.Datetime("us", "UTC"),
                    "precio_entrada": pl.Float64,
                    "precio_salida": pl.Float64,
                    "nocional": pl.Float64,
                    "motivo": pl.String,
                    "barras": pl.Int64,
                    "pnl_precio": pl.Float64,
                    "pnl_funding": pl.Float64,
                    "costes": pl.Float64,
                    "pnl_neto": pl.Float64,
                    "pnl_sin_funding": pl.Float64,
                    "r_multiple": pl.Float64,
                }
            )
        return pl.DataFrame(
            [
                {
                    "activo": o.activo,
                    "estrategia": o.estrategia,
                    "direccion": o.direccion,
                    "ts_senal": o.ts_senal,
                    "ts_entrada": o.ts_entrada,
                    "ts_salida": o.ts_salida,
                    "precio_entrada": o.precio_entrada,
                    "precio_salida": o.precio_salida,
                    "nocional": o.nocional,
                    "motivo": o.motivo,
                    "barras": o.barras,
                    "pnl_precio": o.pnl_precio,
                    "pnl_funding": o.pnl_funding,
                    "costes": o.costes,
                    "pnl_neto": o.pnl_neto,
                    "pnl_sin_funding": o.pnl_sin_funding,
                    "r_multiple": o.r_multiple,
                }
                for o in self.operaciones
            ]
        )


def _tocado(direccion: str, nivel: float, alto: float, bajo: float, *, es_stop: bool) -> bool:
    if direccion == Direccion.LONG.value:
        return bajo <= nivel if es_stop else alto >= nivel
    return alto >= nivel if es_stop else bajo <= nivel


def _precio_ejecucion(direccion: str, nivel: float, apertura: float, *, es_stop: bool) -> float:
    """Precio real de salida, respetando los huecos de apertura.

    Si el mercado abre más allá del nivel, se ejecuta en la apertura: peor para
    un stop, mejor para un objetivo. Suponer que siempre se sale justo en el
    nivel regala precio en los huecos, que es justo donde más duele.
    """
    if direccion == Direccion.LONG.value:
        return min(apertura, nivel) if es_stop else max(apertura, nivel)
    return max(apertura, nivel) if es_stop else min(apertura, nivel)


def ejecutar(
    velas: pl.DataFrame,
    senales: pl.DataFrame,
    *,
    costes: ModeloCostes | None,
    funding: pl.DataFrame | None = None,
    nocional: float = 10_000.0,
    max_barras: int | None = None,
    gestion: Gestion | None = None,
) -> Resultado:
    """Simula las señales sobre las velas. Devuelve las operaciones cerradas.

    `gestion` describe qué se hace con la posición una vez abierta. Sin ella se
    usa la mecánica original: stop fijo, objetivo fijo y una sola posición.

    `max_barras` se mantiene por compatibilidad y, si se pasa, prevalece sobre
    el valor de `gestion`.
    """
    g = gestion or Gestion()
    if max_barras is not None:
        g = replace(g, max_barras=max_barras)
    modelo = validar_modelo(costes)
    if nocional <= 0:
        raise BacktestError("nocional debe ser positivo")
    if velas.is_empty():
        raise BacktestError("no hay velas sobre las que simular")

    resultado = Resultado(senales_totales=senales.height)
    if senales.is_empty():
        return resultado

    velas = velas.sort("ts")
    ts = velas["ts"].to_list()
    apertura = velas["open"].to_list()
    alto = velas["high"].to_list()
    bajo = velas["low"].to_list()
    volumen = velas["volume"].to_list()
    indice = {t: i for i, t in enumerate(ts)}

    # Índices de salida de las posiciones vivas. Su longitud es cuántas hay
    # abiertas, que es lo que limita `max_posiciones`.
    abiertas: list[int] = []

    for senal in senales.sort("ts").iter_rows(named=True):
        i_senal = indice.get(senal["ts"])
        if i_senal is None:
            raise BacktestError(f"señal en {senal['ts']} sin vela correspondiente")

        # Suposición 1: se entra en la apertura de la vela siguiente.
        i_entrada = i_senal + 1
        if i_entrada >= len(ts):
            resultado.senales_descartadas += 1
            continue

        abiertas = [i for i in abiertas if i >= i_entrada]
        if len(abiertas) >= g.max_posiciones:
            resultado.senales_descartadas += 1
            continue

        direccion = senal["direccion"]
        es_largo = direccion == Direccion.LONG.value
        precio_entrada = apertura[i_entrada]
        stop_inicial, objetivo = senal["stop"], senal["objetivo"]

        # Un hueco de apertura puede dejar la entrada ya pasada de su propio
        # stop o de su propio objetivo. En ambos casos la operación nace muerta
        # y no se abre.
        #
        # El caso del objetivo es menos obvio y más traicionero: si el precio ya
        # llegó donde íbamos, la oportunidad ha pasado, y abrir ahí para cerrar
        # de inmediato en el objetivo produce una pérdida garantizada.
        nace_muerta = _tocado(
            direccion, stop_inicial, precio_entrada, precio_entrada, es_stop=True
        )
        if g.usar_objetivo:
            nace_muerta = nace_muerta or _tocado(
                direccion, objetivo, precio_entrada, precio_entrada, es_stop=False
            )
        if nace_muerta:
            resultado.senales_descartadas += 1
            continue

        riesgo = abs(precio_entrada - stop_inicial)
        if riesgo <= 0:
            resultado.senales_descartadas += 1
            continue

        signo = 1.0 if es_largo else -1.0
        stop = stop_inicial
        motivo_stop = MotivoSalida.STOP.value
        mejor_r = 0.0

        i_salida, motivo, precio_salida = None, None, None
        limite = min(i_entrada + g.max_barras, len(ts) - 1)

        for i in range(i_entrada, limite + 1):
            if _tocado(direccion, stop, alto[i], bajo[i], es_stop=True):
                i_salida, motivo = i, motivo_stop
                precio_salida = _precio_ejecucion(direccion, stop, apertura[i], es_stop=True)
                break

            if g.usar_objetivo and _tocado(
                direccion, objetivo, alto[i], bajo[i], es_stop=False
            ):
                i_salida, motivo = i, MotivoSalida.OBJETIVO.value
                precio_salida = _precio_ejecucion(
                    direccion, objetivo, apertura[i], es_stop=False
                )
                break

            # Gestión de la posición viva. Se evalúa DESPUÉS de comprobar la
            # salida: mover el stop con la misma vela que lo habría tocado
            # regalaría una operación que en realidad se cerró.
            extremo = alto[i] if es_largo else bajo[i]
            mejor_r = max(mejor_r, (extremo - precio_entrada) * signo / riesgo)

            if g.break_even_en_r is not None and mejor_r >= g.break_even_en_r:
                nuevo = precio_entrada
                if (nuevo > stop) if es_largo else (nuevo < stop):
                    stop, motivo_stop = nuevo, MotivoSalida.BREAK_EVEN.value

            if g.trailing_desde_r is not None and mejor_r >= g.trailing_desde_r:
                nuevo = extremo - signo * g.trailing_r * riesgo
                if (nuevo > stop) if es_largo else (nuevo < stop):
                    stop, motivo_stop = nuevo, MotivoSalida.TRAILING.value

        if i_salida is None:
            i_salida = limite
            motivo = (
                MotivoSalida.TIEMPO.value
                if limite == i_entrada + g.max_barras
                else MotivoSalida.FIN_DATOS.value
            )
            precio_salida = velas["close"][i_salida]

        unidades = nocional / precio_entrada
        pnl_precio = (precio_salida - precio_entrada) * unidades * signo

        pnl_funding = 0.0
        if funding is not None and not funding.is_empty():
            pnl_funding = coste_funding(
                funding, ts[i_entrada], ts[i_salida], nocional=nocional, es_largo=es_largo
            )

        desglose = modelo.operacion(nocional, volumen_vela=volumen[i_entrada] * precio_entrada)

        resultado.operaciones.append(
            Operacion(
                activo=senal["activo"],
                estrategia=senal["estrategia"],
                direccion=direccion,
                ts_senal=senal["ts"],
                ts_entrada=ts[i_entrada],
                ts_salida=ts[i_salida],
                precio_entrada=precio_entrada,
                precio_salida=precio_salida,
                stop=stop_inicial,
                objetivo=objetivo,
                nocional=nocional,
                motivo=motivo,
                barras=i_salida - i_entrada,
                pnl_precio=pnl_precio,
                pnl_funding=pnl_funding,
                costes=desglose.total,
            )
        )
        abiertas.append(i_salida)

    return resultado
