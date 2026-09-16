"""Generación de señales. Aquí nace la decisión de operar.

Esta capa es determinista y causal por completo: ningún LLM participa. Es lo que
permite backtestearla honestamente, y la razón de que el motor cuantitativo vaya
antes que los agentes en la arquitectura.

Convenio de ejecución, uniforme para todas las estrategias:

- La señal se **evalúa al cierre** de la vela `t`, con datos disponibles hasta
  `t` incluido.
- El campo `entrada` es el cierre de `t`, que es solo un **precio de
  referencia**. La ejecución real ocurre en la apertura de `t+1`; el hueco entre
  ambos es slippage, y el backtest debe cobrarlo.

Suponer que se entra al cierre de la vela que dispara la señal es la forma más
común de inflar un backtest, porque regala un precio que nadie pudo obtener.

Las hipótesis de cada estrategia, con su mecanismo económico y sus criterios de
falsación, están en `docs/04-HIPOTESIS.md`, escritas antes del primer backtest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import polars as pl

from core.quant import indicators as ind
from core.quant.regimen import Regimen

COLUMNAS_SENAL = (
    "ts",
    "activo",
    "estrategia",
    "version",
    "direccion",
    "entrada",
    "stop",
    "objetivo",
    "rr",
    "regimen",
)


class Direccion(StrEnum):
    LONG = "long"
    SHORT = "short"


class EstrategiaError(ValueError):
    """Configuración de estrategia incoherente."""


def alinear_funding(velas: pl.DataFrame, funding: pl.DataFrame) -> pl.DataFrame:
    """Añade a cada vela el último funding conocido y su percentil rodante.

    El percentil se calcula sobre la serie nativa de funding (una observación
    cada 8 h) y solo después se propaga a las velas horarias. Calcularlo sobre
    la serie ya expandida contaría ocho veces cada observación y deformaría la
    distribución.

    El emparejamiento es `join_asof` hacia atrás: cada vela recibe el funding
    vigente en su momento, nunca uno posterior.
    """
    if funding.is_empty():
        raise EstrategiaError("serie de funding vacía")

    ventana = 90 * 3  # 90 días a tres liquidaciones diarias
    fund = (
        funding.sort("ts")
        .with_columns(
            # shift(1): el umbral se calcula con observaciones estrictamente
            # anteriores. Sin él, el valor actual entra en el percentil que
            # decide si el valor actual es extremo, y se compara consigo mismo.
            pl.col("rate")
            .rolling_quantile(quantile=0.95, window_size=ventana)
            .shift(1)
            .alias("funding_p95"),
            pl.col("rate")
            .rolling_quantile(quantile=0.05, window_size=ventana)
            .shift(1)
            .alias("funding_p05"),
        )
        .select(
            "ts",
            pl.col("rate").alias("funding"),
            "funding_p95",
            "funding_p05",
            pl.col("ts").alias("funding_ts"),
        )
    )
    alineado = velas.sort("ts").join_asof(fund, on="ts", strategy="backward")

    # Marca la primera vela de cada liquidación. Sin esto, una tasa extrema
    # queda vigente durante ocho velas horarias y produce ocho señales del
    # mismo evento, multiplicando por ocho el recuento de cualquier estadística.
    return alineado.with_columns(
        (pl.col("funding_ts") != pl.col("funding_ts").shift(1))
        .fill_null(True)
        .alias("funding_nuevo")
    )


def alinear_metrics(velas: pl.DataFrame, metrics: pl.DataFrame) -> pl.DataFrame:
    """Añade a cada vela las métricas de posicionamiento vigentes.

    `join_asof` hacia atrás sobre la serie ya agregada a horaria: cada vela
    recibe la observación de su propia hora, que se conoce al cerrarla. Nunca
    una posterior.
    """
    if metrics.is_empty():
        raise EstrategiaError("serie de métricas vacía")

    columnas = [
        c
        for c in (
            "open_interest",
            "open_interest_usd",
            "top_ls_cuentas",
            "top_ls_posiciones",
            "ls_cuentas",
            "taker_ls_volumen",
        )
        if c in metrics.columns
    ]
    return velas.sort("ts").join_asof(
        metrics.sort("ts").select("ts", *columnas), on="ts", strategy="backward"
    )


def alinear_metrics_al_cierre(
    velas: pl.DataFrame, metrics: pl.DataFrame, minutos: int
) -> pl.DataFrame:
    """Une la última métrica ESTRICTAMENTE anterior al cierre de cada vela.

    `alinear_metrics` une por la apertura de la vela. Eso es correcto cuando la
    métrica ya viene agregada a la misma frecuencia que la vela, que es el caso
    de E1..E4 con series horarias. No lo es con métricas nativas de 5 minutos y
    velas de 15: unir por la apertura descarta tres de cada cuatro observaciones
    y decide con un dato quince minutos más viejo del que había sobre la mesa.

    El corte es `< cierre`, no `<= cierre`. Una observación sellada exactamente
    en el instante del cierre es el caso al filo donde nadie puede demostrar que
    estaba disponible antes de decidir, y en la duda no se usa.
    """
    if metrics.is_empty():
        raise EstrategiaError("serie de métricas vacía")

    columnas = [
        c
        for c in (
            "open_interest",
            "open_interest_usd",
            "top_ls_cuentas",
            "top_ls_posiciones",
            "ls_cuentas",
            "taker_ls_volumen",
        )
        if c in metrics.columns
    ]
    derecha = metrics.sort("ts").select(pl.col("ts").alias("_m_ts"), *columnas)
    return (
        velas.with_columns(
            (pl.col("ts") + pl.duration(minutes=minutos, microseconds=-1)).alias("_antes")
        )
        .sort("_antes")
        .join_asof(derecha, left_on="_antes", right_on="_m_ts", strategy="backward")
        .drop("_antes", "_m_ts")
        .sort("ts")
    )


def _rr(entrada: pl.Expr, stop: pl.Expr, objetivo: pl.Expr) -> pl.Expr:
    riesgo = (entrada - stop).abs()
    return pl.when(riesgo > 0).then((objetivo - entrada).abs() / riesgo).otherwise(None)


def _vacio() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "ts": pl.Datetime("us", "UTC"),
            "activo": pl.String,
            "estrategia": pl.String,
            "version": pl.String,
            "direccion": pl.String,
            "entrada": pl.Float64,
            "stop": pl.Float64,
            "objetivo": pl.Float64,
            "rr": pl.Float64,
            "regimen": pl.String,
        }
    )


@dataclass(frozen=True)
class FundingExtremo:
    """E1 · Posicionamiento saturado como señal contraria.

    Ver `docs/04-HIPOTESIS.md`. Resumen: el funding es dinero que un lado paga
    al otro, así que un extremo indica apalancamiento concentrado y coste de
    acarreo creciente para ese lado.
    """

    version: str = "1.0.0"
    periodos_atr: int = 14
    multiplo_stop: float = 2.0
    rr_objetivo: float = 2.5
    #: Régimen en el que no se opera: ahí el riesgo de evento domina sobre el
    #: de posicionamiento, y el funding extremo puede ser consecuencia de un
    #: movimiento ya en marcha en vez de su antesala.
    regimenes_excluidos: tuple[str, ...] = (
        Regimen.ALTA_VOLATILIDAD.value,
        Regimen.INDEFINIDO.value,
    )

    nombre: str = field(default="E1_funding_extremo", init=False)

    def generar(self, velas: pl.DataFrame, regimenes: pl.Series, activo: str) -> pl.DataFrame:
        if self.rr_objetivo <= 0 or self.multiplo_stop <= 0:
            raise EstrategiaError("rr_objetivo y multiplo_stop deben ser positivos")
        if "funding" not in velas.columns:
            raise EstrategiaError("las velas no traen funding: usa alinear_funding()")

        df = velas.with_columns(
            regimenes.alias("regimen"),
            ind.atr(self.periodos_atr).alias("atr"),
        )

        # Comparación estricta, no `>=`. Un tramo de funding plano empata con su
        # propio percentil, y con `>=` eso disparaba una señal en cada vela:
        # 848 señales espurias en la prueba que destapó el fallo.
        #
        # Funding alto: los largos pagan -> se busca el lado corto, y viceversa.
        direccion = (
            pl.when(pl.col("funding") > pl.col("funding_p95"))
            .then(pl.lit(Direccion.SHORT.value))
            .when(pl.col("funding") < pl.col("funding_p05"))
            .then(pl.lit(Direccion.LONG.value))
            .otherwise(None)
        )

        candidatas = (
            df.with_columns(direccion.alias("direccion"))
            .filter(
                pl.col("direccion").is_not_null()
                & pl.col("atr").is_not_null()
                & pl.col("funding_nuevo")
                & ~pl.col("regimen").is_in(list(self.regimenes_excluidos))
            )
            .with_columns(
                pl.col("close").alias("entrada"),
                pl.when(pl.col("direccion") == Direccion.LONG.value)
                .then(pl.col("close") - self.multiplo_stop * pl.col("atr"))
                .otherwise(pl.col("close") + self.multiplo_stop * pl.col("atr"))
                .alias("stop"),
            )
            .with_columns(
                pl.when(pl.col("direccion") == Direccion.LONG.value)
                .then(pl.col("entrada") + self.rr_objetivo * (pl.col("entrada") - pl.col("stop")))
                .otherwise(pl.col("entrada") - self.rr_objetivo * (pl.col("stop") - pl.col("entrada")))
                .alias("objetivo")
            )
        )
        return _formatear(candidatas, activo, self.nombre, self.version)


@dataclass(frozen=True)
class AsimetriaLiquidaciones:
    """E3 · Solo cortos tras funding extremo positivo.

    Ver `docs/04-HIPOTESIS.md`. No es E1 con el lado largo apagado: es una
    hipótesis distinta sobre la estructura del mercado. El apalancamiento
    minorista es comprador (la mediana del funding es +7,2 % anualizado), las
    liquidaciones largas se retroalimentan a la baja y no existe un exceso
    corto persistente que las equilibre.

    Nace de una observación del backtest de E1, así que **se juzga en activos
    que no participaron en esa observación**. Medirla en BTC y ETH sería
    repetir la observación, no confirmarla.
    """

    version: str = "1.0.0"
    periodos_atr: int = 14
    multiplo_stop: float = 2.0
    rr_objetivo: float = 2.5
    regimenes_excluidos: tuple[str, ...] = (
        Regimen.ALTA_VOLATILIDAD.value,
        Regimen.INDEFINIDO.value,
    )

    nombre: str = field(default="E3_asimetria_liquidaciones", init=False)

    def generar(self, velas: pl.DataFrame, regimenes: pl.Series, activo: str) -> pl.DataFrame:
        if "funding" not in velas.columns:
            raise EstrategiaError("las velas no traen funding: usa alinear_funding()")

        df = velas.with_columns(
            regimenes.alias("regimen"),
            ind.atr(self.periodos_atr).alias("atr"),
        )

        candidatas = (
            df.filter(
                (pl.col("funding") > pl.col("funding_p95"))
                & pl.col("atr").is_not_null()
                & pl.col("funding_nuevo")
                & ~pl.col("regimen").is_in(list(self.regimenes_excluidos))
            )
            .with_columns(
                pl.lit(Direccion.SHORT.value).alias("direccion"),
                pl.col("close").alias("entrada"),
                (pl.col("close") + self.multiplo_stop * pl.col("atr")).alias("stop"),
            )
            .with_columns(
                (
                    pl.col("entrada")
                    - self.rr_objetivo * (pl.col("stop") - pl.col("entrada"))
                ).alias("objetivo")
            )
        )
        return _formatear(candidatas, activo, self.nombre, self.version)


@dataclass(frozen=True)
class DiscrepanciaPosicionamiento:
    """E4 · Distancia entre el posicionamiento de las cuentas grandes y el del conjunto.

    Ver `docs/04-HIPOTESIS.md`. La señal no es el posicionamiento de nadie:
    es la distancia entre ambos. Un funding neutro puede significar que nadie
    tiene opinión, o que dos grupos con información distinta se compensan; el
    funding no los distingue y esto sí.

    Verificado antes de implementarla: corr(D, funding) = -0,004 sobre 35.392
    horas de BTC. Es información distinta, no el funding con otro nombre.
    """

    version: str = "1.0.0"
    periodos_atr: int = 14
    multiplo_stop: float = 2.0
    rr_objetivo: float = 2.5
    #: Ventana del percentil rodante, en velas horarias. 30 días.
    ventana: int = 720
    percentil: float = 0.90
    regimenes_excluidos: tuple[str, ...] = (
        Regimen.ALTA_VOLATILIDAD.value,
        Regimen.INDEFINIDO.value,
    )

    nombre: str = field(default="E4_discrepancia_posicionamiento", init=False)

    def generar(self, velas: pl.DataFrame, regimenes: pl.Series, activo: str) -> pl.DataFrame:
        faltan = {"top_ls_cuentas", "ls_cuentas"} - set(velas.columns)
        if faltan:
            raise EstrategiaError(f"faltan métricas de posicionamiento: {sorted(faltan)}")

        discrepancia = (pl.col("top_ls_cuentas") / pl.col("ls_cuentas")).log()

        df = velas.with_columns(
            regimenes.alias("regimen"),
            ind.atr(self.periodos_atr).alias("atr"),
            discrepancia.alias("D"),
        ).with_columns(
            # shift(1): el umbral se calcula con observaciones estrictamente
            # anteriores, para que el valor actual no participe en decidir si
            # él mismo es extremo. Mismo control que en E1.
            pl.col("D")
            .rolling_quantile(quantile=self.percentil, window_size=self.ventana)
            .shift(1)
            .alias("D_alto"),
            pl.col("D")
            .rolling_quantile(quantile=1 - self.percentil, window_size=self.ventana)
            .shift(1)
            .alias("D_bajo"),
        )

        # Dinero grande más largo que el conjunto -> se sigue al dinero grande.
        direccion = (
            pl.when(pl.col("D") > pl.col("D_alto"))
            .then(pl.lit(Direccion.LONG.value))
            .when(pl.col("D") < pl.col("D_bajo"))
            .then(pl.lit(Direccion.SHORT.value))
            .otherwise(None)
        )

        candidatas = (
            df.with_columns(direccion.alias("direccion"))
            .filter(
                pl.col("direccion").is_not_null()
                & pl.col("atr").is_not_null()
                & ~pl.col("regimen").is_in(list(self.regimenes_excluidos))
            )
            .with_columns(
                pl.col("close").alias("entrada"),
                pl.when(pl.col("direccion") == Direccion.LONG.value)
                .then(pl.col("close") - self.multiplo_stop * pl.col("atr"))
                .otherwise(pl.col("close") + self.multiplo_stop * pl.col("atr"))
                .alias("stop"),
            )
            .with_columns(
                pl.when(pl.col("direccion") == Direccion.LONG.value)
                .then(pl.col("entrada") + self.rr_objetivo * (pl.col("entrada") - pl.col("stop")))
                .otherwise(pl.col("entrada") - self.rr_objetivo * (pl.col("stop") - pl.col("entrada")))
                .alias("objetivo")
            )
        )
        return _formatear(candidatas, activo, self.nombre, self.version)


@dataclass(frozen=True)
class MomentumRuptura:
    """E2 · Ruptura con volumen en tendencia confirmada.

    Ver `docs/04-HIPOTESIS.md`. Solo opera en el sentido de la tendencia: una
    ruptura contra el régimen vigente no es esta hipótesis.
    """

    version: str = "1.0.0"
    periodos_ruptura: int = 24
    periodos_atr: int = 14
    volumen_minimo: float = 1.5
    multiplo_stop: float = 2.0
    rr_objetivo: float = 2.5

    nombre: str = field(default="E2_momentum_ruptura", init=False)

    def generar(self, velas: pl.DataFrame, regimenes: pl.Series, activo: str) -> pl.DataFrame:
        if self.periodos_ruptura < 2:
            raise EstrategiaError("periodos_ruptura debe ser al menos 2")

        df = velas.with_columns(
            regimenes.alias("regimen"),
            ind.atr(self.periodos_atr).alias("atr"),
            ind.volumen_relativo(20).alias("vol_rel"),
            # shift(1) excluye la vela actual del extremo: comparar el cierre
            # con un máximo que ya lo contiene nunca daría ruptura.
            pl.col("high").shift(1).rolling_max(self.periodos_ruptura).alias("techo"),
            pl.col("low").shift(1).rolling_min(self.periodos_ruptura).alias("suelo"),
        )

        rompe_arriba = (pl.col("close") > pl.col("techo")) & (
            pl.col("regimen") == Regimen.TENDENCIA_ALCISTA.value
        )
        rompe_abajo = (pl.col("close") < pl.col("suelo")) & (
            pl.col("regimen") == Regimen.TENDENCIA_BAJISTA.value
        )

        candidatas = (
            df.with_columns(
                pl.when(rompe_arriba)
                .then(pl.lit(Direccion.LONG.value))
                .when(rompe_abajo)
                .then(pl.lit(Direccion.SHORT.value))
                .otherwise(None)
                .alias("direccion")
            )
            .filter(
                pl.col("direccion").is_not_null()
                & pl.col("atr").is_not_null()
                & (pl.col("vol_rel") >= self.volumen_minimo)
            )
            .with_columns(
                pl.col("close").alias("entrada"),
                # Stop al otro lado del rango roto, con un suelo por ATR para
                # que un rango muy estrecho no genere un stop irrisorio.
                pl.when(pl.col("direccion") == Direccion.LONG.value)
                .then(
                    pl.min_horizontal(
                        pl.col("suelo"), pl.col("close") - self.multiplo_stop * pl.col("atr")
                    )
                )
                .otherwise(
                    pl.max_horizontal(
                        pl.col("techo"), pl.col("close") + self.multiplo_stop * pl.col("atr")
                    )
                )
                .alias("stop"),
            )
            .with_columns(
                pl.when(pl.col("direccion") == Direccion.LONG.value)
                .then(pl.col("entrada") + self.rr_objetivo * (pl.col("entrada") - pl.col("stop")))
                .otherwise(pl.col("entrada") - self.rr_objetivo * (pl.col("stop") - pl.col("entrada")))
                .alias("objetivo")
            )
        )
        return _formatear(candidatas, activo, self.nombre, self.version)


@dataclass(frozen=True)
class BarridoLiquidez:
    """C1 · Barrido de un extremo reciente con liquidación forzada.

    Ver `hipotesis/C1-barrido-liquidez.toml`, sellado en la cadena el 2026-09-04
    con sha256 d0425097bc6f9ff34d6413f24d99982828002b8f1187deb79c87f69504e02088.
    Los cinco parámetros de abajo son EXACTAMENTE los de la rejilla
    pre-registrada; no hay ninguno más, y añadir uno exige hipótesis nueva.

    El precio perfora el mínimo de las `periodos_barrido` velas anteriores y lo
    recupera dentro de la misma vela. Eso solo es la forma. Lo que distingue el
    mecanismo de la forma es que el interés abierto CAIGA a la vez: en una
    liquidación forzada se cierran posiciones, mientras que en una venta normal
    se abren cortos y el interés abierto sube.

    `caida_oi = 0` desactiva esa condición y deja el patrón desnudo. No es un
    valor degenerado: es el grupo de control de la hipótesis, y si las celdas
    con condición no superan a las de control, C1 queda refutada.
    """

    version: str = "1.0.0"

    #: N · velas del extremo barrido, excluida la actual.
    periodos_barrido: int = 20
    #: h · fracción mínima del rango que debe quedar del lado de la recuperación.
    cuerpo_minimo: float = 0.66
    #: q · caída relativa de interés abierto exigida. 0 = sin condición (control).
    caida_oi: float = 0.01
    #: m · stop en múltiplos de ATR más allá del extremo barrido.
    multiplo_stop: float = 1.0
    #: rr · objetivo en múltiplos del riesgo.
    rr_objetivo: float = 2.0

    periodos_atr: int = 14
    #: k · ventana de la caída de interés abierto. FIJA en el pre-registro:
    #: 4 velas de 15m = 1 h, que contiene una cascada de liquidaciones.
    velas_oi: int = 4
    #: Derivado del modelo de costes, no elegido: con 12 bps de ida y vuelta el
    #: juez rechaza por encima del 15 % de coste sobre objetivo.
    objetivo_minimo_pct: float = 0.008

    # Sin id de hipótesis en el nombre: la clase implementa el mecanismo, y la
    # misma implementación sirvió a C1 y a C5. Meter "C1" aquí hacía que el
    # informe del juez de C5 dijera «C1_barrido_liquidez». El id vive en el
    # pre-registro, que es donde se puede sellar.
    nombre: str = field(default="barrido_liquidez", init=False)

    def generar(self, velas: pl.DataFrame, regimenes: pl.Series, activo: str) -> pl.DataFrame:
        if self.periodos_barrido < 2:
            raise EstrategiaError("periodos_barrido debe ser al menos 2")
        if not 0.0 <= self.cuerpo_minimo < 1.0:
            raise EstrategiaError("cuerpo_minimo debe estar en [0, 1)")
        if self.caida_oi < 0:
            raise EstrategiaError("caida_oi no puede ser negativa")
        if "open_interest" not in velas.columns:
            raise EstrategiaError("faltan métricas: alinear_metrics antes de generar")

        # Contigüidad. SOL y XRP pierden cinco días de 15m entre febrero y abril
        # de 2022. Sin esta comprobación, el "mínimo de las 20 velas anteriores"
        # de la vela siguiente al hueco compara con precios de tres días antes, y
        # el `shift` del interés abierto cruza el mismo agujero.
        ventana = max(self.periodos_barrido, self.velas_oi) + 1
        salto = pl.col("ts").diff().dt.total_minutes()
        contigua = salto.rolling_max(ventana).fill_null(10**9) <= salto.median()

        df = velas.with_columns(
            regimenes.alias("regimen"),
            ind.atr(self.periodos_atr).alias("atr"),
            contigua.alias("contigua"),
            # shift(1) excluye la vela actual: el extremo con el que se compara
            # no puede contener el barrido que se está evaluando.
            pl.col("low").shift(1).rolling_min(self.periodos_barrido).alias("suelo"),
            pl.col("high").shift(1).rolling_max(self.periodos_barrido).alias("techo"),
            (pl.col("open_interest") / pl.col("open_interest").shift(self.velas_oi) - 1).alias(
                "oi_chg"
            ),
            (pl.col("high") - pl.col("low")).alias("rango"),
        )

        # El pre-registro se contradice en q = 0: la fórmula `oi_chg < -q` exige
        # interés abierto estrictamente decreciente, pero el texto lo define
        # cuatro veces como el grupo de control, "el patrón desnudo". Se resuelve
        # a favor de la intención: con la lectura literal el control no sería un
        # control y `[exito].interno` no podría evaluarse. Aclaración anexada a
        # la cadena el 2026-09-04; el fichero sellado no se toca.
        #
        # En TODAS las celdas se exige que el dato exista. Un hueco de métricas
        # no equivale a "no hubo caída", y mantener la misma muestra es lo que
        # hace comparables las celdas con y sin condición.
        presente = pl.col("oi_chg").is_not_null()
        liquidacion = (
            presente & (pl.col("oi_chg") < -self.caida_oi) if self.caida_oi > 0 else presente
        )

        barre_suelo = (pl.col("low") < pl.col("suelo")) & (pl.col("close") > pl.col("suelo"))
        barre_techo = (pl.col("high") > pl.col("techo")) & (pl.col("close") < pl.col("techo"))

        martillo_largo = (pl.col("close") - pl.col("low")) / pl.col("rango") > self.cuerpo_minimo
        martillo_corto = (pl.col("high") - pl.col("close")) / pl.col("rango") > self.cuerpo_minimo

        largo = barre_suelo & martillo_largo
        corto = barre_techo & martillo_corto

        candidatas = (
            df.filter(
                pl.col("contigua")
                & pl.col("atr").is_not_null()
                & (pl.col("rango") > 0)
                & liquidacion
                & (largo | corto)
            )
            .with_columns(
                pl.when(largo)
                .then(pl.lit(Direccion.LONG.value))
                .otherwise(pl.lit(Direccion.SHORT.value))
                .alias("direccion"),
                pl.col("close").alias("entrada"),
            )
            .with_columns(
                # El stop va al otro lado del extremo barrido, que es justo donde
                # irá el siguiente barrido. Está declarado como riesgo principal
                # en el pre-registro, no descubierto después.
                pl.when(pl.col("direccion") == Direccion.LONG.value)
                .then(pl.col("low") - self.multiplo_stop * pl.col("atr"))
                .otherwise(pl.col("high") + self.multiplo_stop * pl.col("atr"))
                .alias("stop")
            )
            .with_columns(
                pl.when(pl.col("direccion") == Direccion.LONG.value)
                .then(pl.col("entrada") + self.rr_objetivo * (pl.col("entrada") - pl.col("stop")))
                .otherwise(
                    pl.col("entrada") - self.rr_objetivo * (pl.col("stop") - pl.col("entrada"))
                )
                .alias("objetivo")
            )
            # Filtro de coste: un objetivo por debajo del 0,8 % lo devora la
            # fricción. Descartar aquí es más honesto que dejar que el juez lo
            # rechace después, porque esas señales nunca fueron operables.
            .filter(
                (pl.col("objetivo") - pl.col("entrada")).abs() / pl.col("entrada")
                >= self.objetivo_minimo_pct
            )
        )
        return _formatear(candidatas, activo, self.nombre, self.version)


def _formatear(df: pl.DataFrame, activo: str, estrategia: str, version: str) -> pl.DataFrame:
    if df.is_empty():
        return _vacio()
    return (
        df.with_columns(
            pl.lit(activo).alias("activo"),
            pl.lit(estrategia).alias("estrategia"),
            pl.lit(version).alias("version"),
            _rr(pl.col("entrada"), pl.col("stop"), pl.col("objetivo")).alias("rr"),
        )
        .select(COLUMNAS_SENAL)
        .sort("ts")
    )
