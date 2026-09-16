"""Distribución histórica del movimiento tras un evento corporativo.

Es la pieza que convierte una alerta en información utilizable. Cuando el
sistema diga "esto históricamente mueve un X %", ese número sale de aquí: de la
mediana de lo que hicieron los eventos comparables, con su dispersión y su
tamaño de muestra. Nunca de la estimación de un modelo de lenguaje, que no
tiene forma de saberlo y sí de inventarlo.

## La distinción que lo decide todo

Un evento llega tras el cierre del día D. El mercado reacciona en D+1, y buena
parte de la reacción ya está en el **hueco de apertura**, al que nadie llega.

Por eso se miden dos cosas distintas:

| Medida | Desde | Hasta | Qué es |
|---|---|---|---|
| **Movimiento del evento** | cierre de D | cierre de D+n | Lo que el evento mueve |
| **Movimiento capturable** | **apertura de D+1** | cierre de D+n | Lo que se puede operar |

La diferencia entre ambas es el hueco: el trozo que se paga al entrar y que no
se puede ganar. **Si el movimiento capturable es aproximadamente cero mientras
el del evento es grande, la alerta describe algo real y aun así no sirve para
operar.** Es exactamente la clase de conclusión que un informe que solo mire la
primera medida no puede dar.

Todo aquí es determinista y backtesteable. Ningún LLM participa.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl

#: Horizontes en sesiones sobre los que se mide la reacción.
HORIZONTES = (1, 3, 5)

#: Sin al menos esta muestra no se publica un porcentaje. Un número calculado
#: sobre cuatro casos parece información y es ruido con decimales.
MUESTRA_MINIMA = 20


@dataclass(frozen=True)
class Distribucion:
    clase: str
    horizonte: int
    n: int
    mediana_evento: float
    mediana_capturable: float
    p10_capturable: float
    p90_capturable: float
    hueco_mediano: float
    acierto_direccional: float

    @property
    def suficiente(self) -> bool:
        return self.n >= MUESTRA_MINIMA

    @property
    def capturable_neto(self) -> float:
        """Cuánto del movimiento del evento queda tras perder el hueco."""
        return self.mediana_capturable

    def __str__(self) -> str:
        if not self.suficiente:
            return f"{self.clase} @{self.horizonte}d: muestra insuficiente (n={self.n})"
        return (
            f"{self.clase} @{self.horizonte}d (n={self.n}): "
            f"evento {self.mediana_evento:+.2f}% · "
            f"capturable {self.mediana_capturable:+.2f}% "
            f"[p10 {self.p10_capturable:+.2f}%, p90 {self.p90_capturable:+.2f}%] · "
            f"hueco {self.hueco_mediano:+.2f}%"
        )


def _retornos_por_evento(
    precios: pl.DataFrame, sesiones_evento: list[date], horizonte: int
) -> pl.DataFrame:
    """Une cada evento con los precios de su ventana.

    `precios` debe traer una fila por sesión, ordenada, con `ts`, `open` y
    `close`.
    """
    px = (
        precios.sort("ts")
        .with_columns(pl.col("ts").dt.date().alias("sesion"))
        .with_row_index("i")
    )
    indice = {s: i for s, i in zip(px["sesion"], px["i"])}
    cierres, aperturas = px["close"].to_list(), px["open"].to_list()

    filas = []
    for sesion in sesiones_evento:
        i = indice.get(sesion)
        # El cierre que se necesita es cierres[i + horizonte - 1]. Exigir
        # i + horizonte < len descartaba eventos con ventana completa y reducía
        # la muestra en silencio.
        if i is None or i == 0 or i + horizonte > len(cierres):
            continue
        cierre_previo = cierres[i - 1]   # último precio antes de conocerse
        apertura = aperturas[i]          # primer precio operable
        cierre_final = cierres[i + horizonte - 1]
        if not cierre_previo or not apertura:
            continue
        filas.append(
            {
                "evento": (cierre_final / cierre_previo - 1) * 100,
                "capturable": (cierre_final / apertura - 1) * 100,
                "hueco": (apertura / cierre_previo - 1) * 100,
            }
        )
    return pl.DataFrame(filas) if filas else pl.DataFrame(
        schema={"evento": pl.Float64, "capturable": pl.Float64, "hueco": pl.Float64}
    )


def distribucion(
    clase: str, muestras: pl.DataFrame, horizonte: int
) -> Distribucion:
    """Resume la reacción histórica de una clase de evento."""
    if muestras.is_empty():
        return Distribucion(clase, horizonte, 0, 0, 0, 0, 0, 0, 0)

    cap = muestras["capturable"]
    return Distribucion(
        clase=clase,
        horizonte=horizonte,
        n=muestras.height,
        mediana_evento=float(muestras["evento"].median()),
        mediana_capturable=float(cap.median()),
        p10_capturable=float(cap.quantile(0.10)),
        p90_capturable=float(cap.quantile(0.90)),
        hueco_mediano=float(muestras["hueco"].median()),
        # Con qué frecuencia el movimiento capturable va en el mismo sentido
        # que el del evento: mide si el hueco ya agotó la reacción.
        acierto_direccional=float(
            ((muestras["evento"] > 0) == (cap > 0)).mean() * 100
        ),
    )


def analizar(
    eventos: pl.DataFrame,
    precios_por_activo: dict[str, pl.DataFrame],
    *,
    horizontes: tuple[int, ...] = HORIZONTES,
) -> list[Distribucion]:
    """Distribución de reacción por clase de evento y horizonte.

    `eventos` debe traer `activo`, `clase` y `sesion` (la sesión de impacto).
    """
    if eventos.is_empty() or "sesion" not in eventos.columns:
        return []

    salida = []
    for horizonte in horizontes:
        for (clase,), grupo in eventos.group_by(["clase"], maintain_order=True):
            trozos = []
            for (activo,), sub in grupo.group_by(["activo"], maintain_order=True):
                precios = precios_por_activo.get(activo)
                if precios is None or precios.is_empty():
                    continue
                sesiones = [s for s in sub["sesion"].to_list() if s is not None]
                r = _retornos_por_evento(precios, sesiones, horizonte)
                if not r.is_empty():
                    trozos.append(r)
            muestras = pl.concat(trozos) if trozos else pl.DataFrame()
            salida.append(distribucion(str(clase), muestras, horizonte))
    return salida
