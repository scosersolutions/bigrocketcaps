"""Validación de integridad de series OHLCV.

Ningún dato entra en una decisión sin pasar por aquí. El sistema falla cerrado:
ante duda sobre la calidad, no hay señal.

Distingue dos cosas que se confunden con facilidad:
- **Hueco de datos**: la vela debería existir y no está. Es un fallo de la fuente.
- **Hueco de actividad**: no hubo operaciones en ese intervalo. Es información
  legítima del mercado, no un error. Algunos proveedores (Kraken) omiten esas
  velas por diseño.

**Mercados continuos frente a mercados con sesión.** Dos de los criterios solo
tienen sentido en mercados 24/7:

- En acciones, el fin de semana no es un hueco de datos: la bolsa cierra.
- En acciones, `close[t]` y `open[t+1]` **no** coinciden: entre uno y otro pasan
  17,5 horas con noticias, y el gap de apertura es un fenómeno normal. Medido
  sobre AAPL: mediana del 0,434 % y el 45 % de los días por encima del 0,5 %.

Aplicar a acciones el criterio pensado para cripto marcaba como no fiables las
551 series descargadas. El parámetro `continuo` distingue ambos casos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import polars as pl

from core.store.schema import DURACION_MINUTOS, Timeframe

#: Umbral de outlier en z-score robusto (basado en MAD, no en desviación típica).
#: La desviación típica no sirve aquí: un solo salto enorme infla la sigma lo
#: bastante como para quedar por debajo de su propio umbral y pasar inadvertido.
#: El MAD no se contamina con unos pocos valores extremos.
#: No descarta la vela: la marca para revisión, porque en cripto un ±20 % real existe.
Z_MAD_OUTLIER = 10.0

#: Constante que hace el MAD comparable a una desviación típica en una normal.
_ESCALA_MAD = 0.6745

#: Desfase tolerado entre close[t] y open[t+1]. Por encima, el dato está mal grabado.
TOLERANCIA_CONTINUIDAD = 0.005


@dataclass(frozen=True)
class Hueco:
    desde: datetime
    hasta: datetime
    velas_ausentes: int


@dataclass
class InformeCalidad:
    activo: str
    timeframe: str
    filas: int
    desde: datetime | None = None
    hasta: datetime | None = None
    huecos: list[Hueco] = field(default_factory=list)
    movimientos_extremos: int = 0
    discontinuidades: int = 0
    velas_sinteticas: int = 0
    volumen_cero: int = 0
    duplicados: int = 0
    no_monotono: bool = False
    stale_minutos: float | None = None
    errores: list[str] = field(default_factory=list)

    @property
    def velas_ausentes(self) -> int:
        return sum(h.velas_ausentes for h in self.huecos)

    @property
    def cobertura(self) -> float:
        """Fracción de velas presentes sobre las esperadas en el rango."""
        esperadas = self.filas + self.velas_ausentes
        return 1.0 if esperadas == 0 else self.filas / esperadas

    @property
    def fiable(self) -> bool:
        """Una discontinuidad es un dato corrupto y sí invalida la serie; un
        movimiento extremo es información legítima del mercado y no."""
        return (
            not self.errores
            and not self.duplicados
            and not self.no_monotono
            and not self.discontinuidades
        )

    def resumen(self) -> str:
        estado = "OK" if self.fiable else "NO FIABLE"
        return (
            f"[{estado}] {self.activo} {self.timeframe}: {self.filas} velas, "
            f"cobertura {self.cobertura:.4%}, huecos {len(self.huecos)} "
            f"({self.velas_ausentes} velas), mov. extremos {self.movimientos_extremos}, "
            f"discontinuidades {self.discontinuidades}, "
            f"sinteticas {self.velas_sinteticas}, volumen cero {self.volumen_cero}"
            + (f", errores: {'; '.join(self.errores)}" if self.errores else "")
        )


def _z_robusto(retornos: pl.Series) -> pl.Series | None:
    """z-score sobre MAD. None si la serie es demasiado corta o degenerada."""
    if retornos.len() < 3:
        return None
    mediana = retornos.median()
    mad = (retornos - mediana).abs().median()
    if not mad:
        return None
    return _ESCALA_MAD * (retornos - mediana) / mad


def _contar_movimientos_extremos(retornos: pl.Series) -> int:
    z = _z_robusto(retornos)
    return 0 if z is None else int((z.abs() > Z_MAD_OUTLIER).sum())


def _marcar_sinteticas(df: pl.DataFrame) -> pl.Series:
    """Velas de relleno: OHLC idénticos y volumen nulo.

    Algunos proveedores (Binance entre ellos) rellenan así los intervalos sin
    datos en vez de omitirlos. Parecen "el precio no se movió" cuando en
    realidad significan "no hay información", y falsean cualquier cálculo de
    volatilidad o de volumen que las tome por buenas.
    """
    return df.select(
        (
            (pl.col("volume") <= 0)
            & (pl.col("open") == pl.col("close"))
            & (pl.col("high") == pl.col("low"))
            & (pl.col("open") == pl.col("high"))
        ).alias("s")
    )["s"]


def _contar_discontinuidades(df: pl.DataFrame, paso: timedelta) -> int:
    """Velas donde el precio salta entre el cierre de una y la apertura de la siguiente.

    Esta es la firma inequívoca de un dato mal grabado. En una serie correcta,
    `close[t]` y `open[t+1]` coinciden salvo diferencias mínimas de redondeo,
    y eso se cumple igual en un mercado tranquilo que en un desplome.

    El criterio anterior ---salto extremo seguido de reversión--- se descartó
    porque marcaba como corruptos los flash crash reales: el colapso de Terra
    (2022-05-11), la capitulación de Celsius (2022-06-14) y la caída de FTX
    (2022-11-08) tienen exactamente esa forma y son datos correctos.
    """
    if df.height < 2:
        return 0
    sintetica = _marcar_sinteticas(df)
    tabla = df.select(
        ((pl.col("open").shift(-1) - pl.col("close")).abs() / pl.col("close")).alias("d"),
        # Un salto a través de un hueco no es un dato mal grabado: el precio se
        # movió durante las velas que faltan. Ya está reportado como hueco, y
        # contarlo otra vez bajo otro nombre solo duplica el mismo defecto.
        ((pl.col("ts").shift(-1) - pl.col("ts")) > paso).alias("tras_hueco"),
    ).with_columns(
        # Igual con las velas de relleno: la discontinuidad la explica la vela.
        (sintetica | sintetica.shift(-1).fill_null(False)).alias("sintetica")
    )
    return int(
        (
            (tabla["d"] > TOLERANCIA_CONTINUIDAD)
            & ~tabla["sintetica"]
            & ~tabla["tras_hueco"].fill_null(False)
        ).sum()
    )


def _laborables_entre(inicio: datetime, fin: datetime) -> int:
    """Días de lunes a viernes estrictamente entre dos fechas."""
    dias = (fin.date() - inicio.date()).days
    if dias <= 1:
        return 0
    return sum(
        1
        for i in range(1, dias)
        if (inicio.date() + timedelta(days=i)).weekday() < 5
    )


def _huecos_por_dias_habiles(ts: pl.Series) -> list[Hueco]:
    """Huecos en un mercado con sesión: solo cuentan los días laborables.

    Entre el cierre del viernes y la apertura del lunes no falta ningún dato:
    la bolsa está cerrada. Los festivos sí aparecen como huecos de un día, que
    es lo correcto: son jornadas sin cotización.
    """
    huecos = []
    for i in range(ts.len() - 1):
        ausentes = _laborables_entre(ts[i], ts[i + 1])
        if ausentes > 0:
            huecos.append(Hueco(desde=ts[i], hasta=ts[i + 1], velas_ausentes=ausentes))
    return huecos


def validar_ohlcv(
    df: pl.DataFrame,
    activo: str,
    timeframe: Timeframe,
    *,
    ahora: datetime | None = None,
    max_stale_minutos: float | None = None,
    continuo: bool = True,
) -> InformeCalidad:
    informe = InformeCalidad(activo=activo, timeframe=timeframe.value, filas=df.height)

    if df.is_empty():
        informe.errores.append("serie vacía")
        return informe

    ts = df["ts"]
    if ts.dtype.time_zone is None:
        informe.errores.append("ts sin zona horaria")
        return informe

    informe.desde, informe.hasta = ts.min(), ts.max()

    informe.duplicados = df.height - df["ts"].n_unique()
    if informe.duplicados:
        informe.errores.append(f"{informe.duplicados} timestamps duplicados")

    if not ts.is_sorted():
        informe.no_monotono = True
        informe.errores.append("timestamps no monótonos")
        return informe

    paso = timedelta(minutes=DURACION_MINUTOS[timeframe])
    if continuo:
        diffs = df.select(pl.col("ts").diff().alias("d")).drop_nulls()
        saltos = diffs.with_row_index("i").filter(pl.col("d") > paso)
        for fila in saltos.iter_rows(named=True):
            idx = int(fila["i"])
            informe.huecos.append(
                Hueco(
                    desde=ts[idx],
                    hasta=ts[idx + 1],
                    velas_ausentes=int(fila["d"] / paso) - 1,
                )
            )
    elif timeframe is Timeframe.D1 and df.height > 1:
        informe.huecos = _huecos_por_dias_habiles(ts)

    informe.volumen_cero = int(df.filter(pl.col("volume") <= 0).height)

    retornos = df.select(
        (pl.col("close").log() - pl.col("close").shift(1).log()).alias("r")
    ).drop_nulls()
    informe.movimientos_extremos = _contar_movimientos_extremos(retornos["r"])
    # La continuidad close->open solo es exigible en mercados 24/7. En bolsa,
    # el gap de apertura es información real, no un dato mal grabado.
    informe.discontinuidades = _contar_discontinuidades(df, paso) if continuo else 0
    informe.velas_sinteticas = int(_marcar_sinteticas(df).sum())

    if max_stale_minutos is not None:
        ahora = ahora or datetime.now(UTC)
        informe.stale_minutos = (ahora - informe.hasta).total_seconds() / 60
        if informe.stale_minutos > max_stale_minutos:
            informe.errores.append(
                f"datos obsoletos: {informe.stale_minutos:.0f} min > {max_stale_minutos:.0f}"
            )

    return informe
