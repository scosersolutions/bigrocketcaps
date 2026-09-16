"""Control de calidad de una serie de precios, sin depender de otra fuente.

## Por qué no hay verificación cruzada contra un segundo proveedor

El plan la daba por hecha con Stooq. Comprobado el 2026-09-16: Stooq responde
con un desafío anti-bot —una prueba de trabajo en JavaScript— en vez del CSV.
Saltárselo no es una opción, así que Stooq queda fuera.

Las alternativas gratuitas que quedan (Tiingo, 500 peticiones al día; Alpha
Vantage, 25) exigen registrarse y dan para una muestra, no para el universo.
Eso es una decisión del dueño del proyecto, no del código, y mientras no se
tome **no hay segunda fuente**. Se declara aquí en vez de fingir que la hay:
una verificación cruzada que en realidad no cruza nada es peor que ninguna,
porque da confianza sin fundamento.

## Qué hace esto en su lugar

Comprobaciones internas, que no necesitan a nadie y cogen la mayor parte de lo
que de verdad se rompe en una serie de precios descargada: coherencia del OHLC,
saltos con pinta de split mal ajustado, series congeladas, huecos y duplicados.

No detectan un sesgo sistemático de la fuente —para eso hace falta la segunda—
pero sí el error que arruina un backtest en silencio: un precio imposible que
genera un retorno de −90 % que nunca ocurrió.

## Severidades

- `grave`: la fila es inutilizable y el día se excluye.
- `sospechoso`: puede ser real; se marca y se cuenta, no se tira.
"""
from __future__ import annotations

import polars as pl

#: Un movimiento diario de este tamaño sin ser split es rarísimo en una acción
#: con liquidez. No se excluye por sí solo: se marca.
#:
#: La comparación es `>=` y no `>` a propósito: un split 2:1 --el más común de
#: todos-- deja el cierre en exactamente la mitad, o sea un movimiento de 0,50
#: clavado. Con el umbral estricto se colaba justo el caso que más importa.
SALTO_SOSPECHOSO = 0.50

#: Fracciones típicas de un split no ajustado: 1/2, 1/3, 1/4, 2/3, 3/4...
#: Un cierre que cae a exactamente la mitad no es una caída del 50 %, es un
#: 2:1 que la fuente no ajustó.
RATIOS_SPLIT = (0.5, 1 / 3, 0.25, 2 / 3, 0.75, 0.2, 0.1, 2.0, 3.0, 4.0, 1.5)

#: Medido sobre 300 series reales el 2026-09-16: de los saltos que caian en
#: alguna banda, el 86 % estaba a menos del 0,05 % del ratio EXACTO --0,5000,
#: 0,3333-- y el resto disperso (0,3248, 0,4902). Los primeros son splits; los
#: segundos, casualidades que una tolerancia del 1 % se tragaba.
#:
#: Con 0,2 % los exactos siguen dentro y lo dudoso baja a `salto_grande`, que
#: se marca pero no se excluye. Es la diferencia entre tirar un dato malo y
#: tirar una caida real del 51 %.
TOLERANCIA_SPLIT = 0.002

#: Días seguidos con el mismo cierre y volumen positivo. Una acción líquida no
#: cierra cinco días idénticos; una serie congelada por la fuente, sí.
DIAS_CONGELADO = 5


def revisar(velas: pl.DataFrame, *, activo: str = "") -> pl.DataFrame:
    """Incidencias de una serie. Vacío significa que no se encontró nada.

    `velas` necesita ts, open, high, low, close, volume.
    """
    if velas.is_empty():
        return _vacio()
    df = velas.sort("ts")
    partes = [
        _ohlc_incoherente(df),
        _no_positivos(df),
        _duplicados(df),
        _saltos(df),
        _congelados(df),
    ]
    fuera = pl.concat([p for p in partes if not p.is_empty()]) if any(
        not p.is_empty() for p in partes) else _vacio()
    if activo and not fuera.is_empty():
        fuera = fuera.with_columns(pl.lit(activo).alias("activo"))
    return fuera


def _vacio() -> pl.DataFrame:
    return pl.DataFrame(schema={"ts": pl.Datetime(time_zone="UTC"),
                                "tipo": pl.Utf8, "detalle": pl.Utf8,
                                "severidad": pl.Utf8})


def _incidencia(df: pl.DataFrame, tipo: str, detalle: pl.Expr,
                severidad: str) -> pl.DataFrame:
    if df.is_empty():
        return _vacio()
    return df.select(
        pl.col("ts"),
        pl.lit(tipo).alias("tipo"),
        detalle.alias("detalle"),
        pl.lit(severidad).alias("severidad"),
    )


def _ohlc_incoherente(df: pl.DataFrame) -> pl.DataFrame:
    """El máximo tiene que ser el máximo. Si no, la fila no vale para nada."""
    malas = df.filter(
        (pl.col("high") < pl.col("low"))
        | (pl.col("high") < pl.col("open")) | (pl.col("high") < pl.col("close"))
        | (pl.col("low") > pl.col("open")) | (pl.col("low") > pl.col("close"))
    )
    return _incidencia(
        malas, "ohlc_incoherente",
        pl.format("o={} h={} l={} c={}", "open", "high", "low", "close"),
        "grave")


def _no_positivos(df: pl.DataFrame) -> pl.DataFrame:
    malas = df.filter(
        (pl.col("close") <= 0) | (pl.col("open") <= 0)
        | (pl.col("high") <= 0) | (pl.col("low") <= 0)
        | (pl.col("volume") < 0)
    )
    return _incidencia(malas, "precio_no_positivo",
                       pl.format("close={} volume={}", "close", "volume"), "grave")


def _duplicados(df: pl.DataFrame) -> pl.DataFrame:
    """Dos velas con el mismo instante: alguna descarga se pegó dos veces."""
    malas = df.filter(pl.col("ts").is_duplicated()).unique(subset=["ts"])
    return _incidencia(malas, "ts_duplicado", pl.lit("ts repetido"), "grave")


def _saltos(df: pl.DataFrame) -> pl.DataFrame:
    """Saltos enormes, separando los que huelen a split de los que no.

    Un split mal ajustado deja un cociente casi exacto (0,5 · 1/3 · 0,25...).
    Eso no es una caída: es la misma empresa con el doble de acciones, y
    tratarlo como retorno mete una pérdida del 50 % que nunca existió.
    """
    # El ratio se calcula contra el vecino REAL y solo despues se descarta lo
    # que no vale. Filtrar antes quitaba la fila anterior y con ella la
    # referencia del salto: con [100, 50, 50.5] el 2:1 desaparecia.
    #
    # Y se anula donde alguno de los dos cierres no es positivo: un ratio
    # contra cero no significa nada, da infinito, y formatear infinito hace
    # reventar a polars 1.44 por dentro. Los ceros los coge _no_positivos.
    d = df.with_columns(
        pl.col("close").shift(1).alias("_previo")
    ).with_columns(
        pl.when((pl.col("close") > 0) & (pl.col("_previo") > 0))
        .then(pl.col("close") / pl.col("_previo"))
        .otherwise(None)
        .alias("_ratio")
    ).drop_nulls("_ratio").with_columns(
        (pl.col("_ratio") - 1).abs().alias("_mov")
    )
    grandes = d.filter(pl.col("_mov") >= SALTO_SOSPECHOSO)
    if grandes.is_empty():
        return _vacio()

    cerca = pl.any_horizontal(
        [(pl.col("_ratio") - r).abs() < TOLERANCIA_SPLIT for r in RATIOS_SPLIT])
    partes = [
        _incidencia(grandes.filter(cerca), "posible_split_sin_ajustar",
                    pl.format("ratio={}", pl.col("_ratio").round(4)), "grave"),
        _incidencia(grandes.filter(~cerca), "salto_grande",
                    pl.format("ratio={}", pl.col("_ratio").round(4)), "sospechoso"),
    ]
    # Concatenar un marco de texto vacio con otro lleno hace que polars 1.44
    # reviente por dentro (assertion failed: i < self.len(), en binview). Se
    # filtran los vacios: mas barato que perseguir el panic desde fuera.
    llenos = [x for x in partes if not x.is_empty()]
    return pl.concat(llenos) if llenos else _vacio()


def _congelados(df: pl.DataFrame) -> pl.DataFrame:
    """Cierres idénticos varios días seguidos con volumen: serie congelada."""
    d = df.with_columns(
        (pl.col("close") != pl.col("close").shift(1)).fill_null(True)
        .cum_sum().alias("_tramo")
    )
    tramos = d.group_by("_tramo").agg(
        pl.len().alias("_dias"),
        pl.col("ts").first().alias("ts"),
        pl.col("close").first().alias("close"),
        pl.col("volume").min().alias("_vol_min"),
    ).filter((pl.col("_dias") >= DIAS_CONGELADO) & (pl.col("_vol_min") > 0))
    return _incidencia(
        tramos.sort("ts"), "serie_congelada",
        pl.format("{} días al mismo cierre {}", "_dias", "close"), "sospechoso")


def dias_a_excluir(incidencias: pl.DataFrame) -> set:
    """Las fechas con algo grave. Lo sospechoso se cuenta, no se tira."""
    if incidencias.is_empty():
        return set()
    graves = incidencias.filter(pl.col("severidad") == "grave")
    return set(graves["ts"].dt.date().to_list())
