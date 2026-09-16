"""Estudio de eventos: qué hace el precio después de un hecho público.

## La pregunta que responde

Dado un conjunto de eventos con fecha y hora, ¿el precio se mueve de forma
distinta a como se mueve el resto del mercado esos mismos días?

## Las tres formas de engañarse aquí, y cómo se evitan

1. **Entrar antes de que el hecho fuera público.** Un 8-K aceptado a las 20:30
   UTC no se pudo operar esa sesión. El 61 % de las emisiones se aceptan tras
   el cierre, así que esto no es un detalle: es la mitad de la muestra. La
   entrada es el cierre de la primera sesión en que el mercado PUDO reaccionar,
   que sale de `tras_cierre`.

2. **Comparar contra el índice equivocado.** Medir una small cap contra el
   S&P 500 mide estilo, no evento. La referencia es la mediana del propio
   universo ese día: lo que hicieron los demás valores con precio esa jornada.

3. **Confundir la cola con el centro.** Una media negativa con mediana positiva
   no es un efecto, es un puñado de desplomes arrastrando el promedio. Se
   devuelven las dos, siempre, y el criterio de refutación las mira.

## Lo que este módulo NO hace

No juzga. Devuelve los excesos y quien juzga es el juez, con sus criterios y su
contraste contra el azar. Separarlo importa: un módulo que midiera y además
decidiera si el resultado es bueno tendería a medir lo que decide bien.
"""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl


class EstudioError(ValueError):
    """Los datos no permiten medir lo que se pide."""


@dataclass(frozen=True)
class Resultado:
    """Lo medido, sin interpretar."""

    n: int
    n_empresas: int
    exceso_medio_pct: float
    exceso_mediano_pct: float
    sigma_pct: float
    t: float
    ganadoras_pct: float
    por_tramo: dict[str, float]

    def __str__(self) -> str:
        return (
            f"n={self.n:,} en {self.n_empresas:,} empresas · "
            f"exceso medio {self.exceso_medio_pct:+.3f}% · "
            f"mediana {self.exceso_mediano_pct:+.3f}% · "
            f"t={self.t:+.2f} · gana {self.ganadoras_pct:.1f}%"
        )


def sesion_de_entrada(eventos: pl.DataFrame, sesiones: list) -> pl.DataFrame:
    """Primera sesión en la que se pudo reaccionar a cada evento.

    Buscar en el calendario real en vez de sumar un día evita atribuir la
    reacción a un fin de semana o a un festivo: la operación aparecería con el
    precio de una sesión que no existió.
    """
    if eventos.is_empty():
        return eventos.with_columns(pl.lit(None).alias("entrada"))
    orden = sorted(sesiones)
    marco = pl.DataFrame({"entrada": orden}).sort("entrada")
    # Tras el cierre -> la siguiente sesión ESTRICTAMENTE posterior.
    # Antes del cierre -> la misma sesión si cotiza, si no la siguiente.
    ev = eventos.with_columns(
        pl.when(pl.col("tras_cierre"))
        .then(pl.col("presentado") + pl.duration(days=1))
        .otherwise(pl.col("presentado"))
        .alias("_desde")
    ).sort("_desde")
    return ev.join_asof(marco, left_on="_desde", right_on="entrada",
                        strategy="forward").drop("_desde")


def medir(
    eventos: pl.DataFrame,
    precios: pl.DataFrame,
    *,
    horizonte: int,
) -> Resultado:
    """Exceso sobre la mediana del universo, a `horizonte` sesiones.

    `eventos` necesita (ticker, presentado, tras_cierre).
    `precios` necesita (activo, fecha, close).
    """
    if eventos.is_empty() or precios.is_empty():
        raise EstudioError("sin eventos o sin precios no hay nada que medir")

    px = precios.sort(["activo", "fecha"]).with_columns(
        pl.col("close").shift(-horizonte).over("activo").alias("_salida")
    ).with_columns(
        (pl.col("_salida") / pl.col("close") - 1).alias("_ret")
    ).drop_nulls("_ret")

    # La referencia: lo que hizo la mediana del universo ese día. Se calcula
    # sobre TODOS los valores con precio, no solo sobre los del evento.
    referencia = px.group_by("fecha").agg(
        pl.col("_ret").median().alias("_ref"),
        pl.len().alias("_vivos"),
    )

    sesiones = px["fecha"].unique().to_list()
    ev = sesion_de_entrada(eventos, sesiones).drop_nulls("entrada")
    if ev.is_empty():
        raise EstudioError("ningún evento cae en una sesión con precio")

    j = (
        ev.join(px, left_on=["ticker", "entrada"], right_on=["activo", "fecha"],
                how="inner")
        .join(referencia, left_on="entrada", right_on="fecha", how="inner")
        .with_columns(((pl.col("_ret") - pl.col("_ref")) * 100).alias("exceso"))
    )
    if j.is_empty():
        raise EstudioError("ningún evento cruza con un precio de entrada")

    exc = j["exceso"]
    n = len(exc)
    sigma = float(exc.std()) if n > 1 else 0.0
    media = float(exc.mean())
    t = media / (sigma / (n ** 0.5)) if sigma > 0 and n > 1 else 0.0

    # Por tramos de años: un efecto que solo vive en una época no es un efecto.
    tramos = {}
    for desde, hasta in ((2018, 2020), (2021, 2022), (2023, 2024)):
        t_exc = j.filter(
            pl.col("entrada").dt.year().is_between(desde, hasta))["exceso"]
        if len(t_exc):
            tramos[f"{desde}-{hasta}"] = round(float(t_exc.mean()), 3)

    return Resultado(
        n=n,
        n_empresas=j["ticker"].n_unique(),
        exceso_medio_pct=round(media, 4),
        exceso_mediano_pct=round(float(exc.median()), 4),
        sigma_pct=round(sigma, 3),
        t=round(t, 3),
        ganadoras_pct=round(float((exc > 0).mean()) * 100, 2),
        por_tramo=tramos,
    )
