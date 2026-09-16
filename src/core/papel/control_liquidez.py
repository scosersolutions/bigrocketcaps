"""Control de E7c contra acciones de liquidez comparable, no contra un índice.

## Por qué hace falta

El seguimiento en papel compara cada señal contra **IWM**, porque es un índice
comprable y con datos públicos que el job diario puede leer sin la base. Pero el
control del backtest era otro: acciones **del mismo día y del mismo quintil de
liquidez**. La diferencia importa. Un exceso sobre IWM puede venir de que las
acciones de la banda 0,6-50 M$ se comporten distinto del Russell 2000 en su
conjunto, y eso no lo aporta ningún insider.

`papel_e7c.py` ya lo dejó escrito el 2026-09-02: «esa versión se recalculará
cuando la SEC publique el trimestre». Esto es esa versión.

## Cuándo se escribió, que es lo único que la hace válida

El 2026-09-04, con **cero predicciones cerradas**. La primera señal es del
2026-09-01 y vence a 21 sesiones, en octubre. No existe todavía ni un número que
pudiera haber influido en cómo se define este control, y por eso se define ahora
y no cuando haya resultados que mirar.

## Lo que este módulo no hace

No toca E7c. No cambia sus criterios, ni qué señales entran en banda, ni el
exceso sobre IWM con el que se juzgará. Produce una **segunda medición** sobre
las mismas predicciones, que se reporta al lado de la primera. Si las dos
discrepan, eso es información, no un motivo para quedarse con la que guste.

## La regla, entera

Para una señal en el activo A con fecha de señal F y horizonte h sesiones:

1. **Universo**: acciones de `stock_us` con al menos `VENTANA` sesiones de
   historia terminando en F, y cierre en F de al menos `PRECIO_MINIMO` dólares.
   El filtro de precio es el de Jegadeesh y Titman, el mismo que E7b adoptó para
   quitarse de encima las deslistadas cotizando en céntimos.
2. **Liquidez**: mediana del dólar-volumen de esas `VENTANA` sesiones. Mismo
   estimador que usa `papel_e7c._liquidez()` para la señal, para que el quintil
   al que cae A signifique lo mismo en los dos sitios.
3. **Quintiles**: se calculan sobre ese universo y ese día. Nada de cortes fijos
   heredados de otro periodo.
4. **Control**: todas las acciones del quintil de A, menos A y menos las que
   tengan su propia señal de insiders en la ventana. Si el control contiene el
   efecto que se está midiendo, mide cero por construcción.
5. **Retorno**: apertura de la sesión siguiente a F, cierre de la sesión F+h.
   Idéntico a como se mide A en `papel_e7c.evaluar()`, porque comparar dos
   retornos calculados de forma distinta no compara nada.
6. **Exceso**: retorno de A menos la **media** del control.

Todo se calcula con información disponible en F. La única fecha posterior que
interviene son los precios del horizonte, que es lo que se está midiendo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Sequence

import polars as pl

# --- Parámetros del control. Escritos antes del primer resultado. ------------
VENTANA = 30          # sesiones para la mediana de dólar-volumen
PRECIO_MINIMO = 5.0   # dólares, evaluados en la fecha de señal
QUINTILES = 5
MERCADO = "stock_us"
TIMEFRAME = "1d"
# Un control con menos de esto no se reporta: la media de cuatro acciones no es
# un control, es otra señal con ruido.
CONTROL_MINIMO = 20
# ----------------------------------------------------------------------------


class SinDatos(Exception):
    """No hay precios suficientes para construir el control de esta señal."""


@dataclass(frozen=True)
class Control:
    """El resultado de contrastar una señal contra su quintil de liquidez."""

    activo: str
    fecha_senal: str
    horizonte: int
    quintil: int
    n_control: int
    retorno: float
    retorno_control: float
    entrada: str
    salida: str

    @property
    def exceso(self) -> float:
        return self.retorno - self.retorno_control


def universo_liquido(con: Any, fecha: date) -> pl.DataFrame:
    """Acciones vivas en `fecha`, con su liquidez y su cierre de ese día.

    Solo mira sesiones anteriores o iguales a `fecha`: el universo de un día no
    puede saber nada de los siguientes.
    """
    filas = con.execute(
        f"""
        WITH recientes AS (
            SELECT activo, ts::date AS d, close, volume,
                   ROW_NUMBER() OVER (PARTITION BY activo ORDER BY ts DESC) AS atras
            FROM ohlcv
            WHERE mercado = ? AND timeframe = ? AND ts::date <= ?
        )
        SELECT activo,
               MEDIAN(close * volume) AS liquidez,
               COUNT(*)               AS sesiones,
               MAX(CASE WHEN atras = 1 THEN close END) AS cierre,
               MAX(CASE WHEN atras = 1 THEN d END)     AS ultima
        FROM recientes
        WHERE atras <= {VENTANA}
        GROUP BY activo
        HAVING COUNT(*) = {VENTANA}
        """,
        [MERCADO, TIMEFRAME, fecha],
    ).fetchall()

    df = pl.DataFrame(
        filas,
        schema=["activo", "liquidez", "sesiones", "cierre", "ultima"],
        orient="row",
    )
    if df.is_empty():
        return df
    # `ultima` puede ser anterior a `fecha` si la acción dejó de cotizar. Se
    # exige el dato del día: una acción sin precio en F no está en el universo
    # de F, y arrastrarla sería resucitar una deslistada.
    return df.filter(
        (pl.col("ultima") == fecha) & (pl.col("cierre") >= PRECIO_MINIMO)
    ).drop("sesiones", "ultima")


def con_quintil(df: pl.DataFrame) -> pl.DataFrame:
    """Añade el quintil de liquidez, 1 el más ilíquido y 5 el más líquido."""
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Int32).alias("quintil"))
    return df.with_columns(
        (
            (pl.col("liquidez").rank("ordinal") - 1)
            * QUINTILES
            // pl.len()
            + 1
        )
        .cast(pl.Int32)
        .alias("quintil")
    )


def retornos(con: Any, activos: Sequence[str], f_ent: date, f_sal: date) -> pl.DataFrame:
    """Retorno porcentual de apertura en `f_ent` a cierre en `f_sal`.

    Solo devuelve las acciones que tienen ambos precios. Una acción que deja de
    cotizar a mitad de la ventana no entra en el control con un cero: no cotizó,
    y suponerle un retorno es inventarse el dato que falta.
    """
    if not activos:
        return pl.DataFrame(schema={"activo": pl.String, "retorno": pl.Float64})

    filas = con.execute(
        """
        SELECT e.activo, (s.close / e.open - 1) * 100 AS retorno
        FROM ohlcv e
        JOIN ohlcv s
          ON s.activo = e.activo AND s.mercado = e.mercado
         AND s.timeframe = e.timeframe AND s.ts::date = ?
        WHERE e.mercado = ? AND e.timeframe = ? AND e.ts::date = ?
          AND e.activo IN (SELECT UNNEST(?)) AND e.open > 0
        """,
        [f_sal, MERCADO, TIMEFRAME, f_ent, list(activos)],
    ).fetchall()
    return pl.DataFrame(filas, schema=["activo", "retorno"], orient="row")


def sesiones_desde(con: Any, fecha: date, cuantas: int) -> list[date]:
    """Las `cuantas` primeras sesiones posteriores a `fecha`, del propio mercado.

    Se toman del mercado y no de un calendario aparte porque el horizonte de
    E7c está definido en sesiones, y las que cuentan son en las que se pudo
    operar de verdad.
    """
    filas = con.execute(
        """
        SELECT DISTINCT ts::date AS d FROM ohlcv
        WHERE mercado = ? AND timeframe = ? AND ts::date > ?
        ORDER BY d LIMIT ?
        """,
        [MERCADO, TIMEFRAME, fecha, cuantas],
    ).fetchall()
    return [f[0] for f in filas]


def con_senal_insider(con: Any, desde: date, hasta: date) -> set[str]:
    """Acciones con compra de insider publicada en la ventana.

    Se lee de la tabla `insiders`, que es la fuente real, y no del registro en
    papel: el registro solo contiene los grupos que pasaron los criterios de
    E7c, y para ensuciar el control basta con que una acción tenga compras.

    La fecha es la de **publicación**, igual que en todo el proyecto: un Form 4
    se presenta hasta dos días hábiles después de la compra, y usar la del hecho
    metería en el control información que ese día no era pública.
    """
    filas = con.execute(
        "SELECT DISTINCT activo FROM insiders WHERE presentado BETWEEN ? AND ?",
        [desde, hasta],
    ).fetchall()
    return {f[0] for f in filas}


def cobertura_insiders(con: Any, hasta: date) -> tuple[date | None, bool]:
    """Hasta dónde llega la tabla `insiders`, y si cubre la ventana pedida.

    La SEC publica sus conjuntos con retraso trimestral, así que la tabla se
    queda corta justo en el periodo vivo, que es el que E7c está midiendo. Sin
    datos, `con_senal_insider` no excluye nada y el control acaba conteniendo
    acciones con su propia señal dentro.

    El sesgo va **en contra** de E7c: si el control lleva dentro el efecto, mide
    de más y el exceso sale menor del real. Es la dirección buena para
    equivocarse, pero eso no es motivo para no decirlo.
    """
    fila = con.execute("SELECT MAX(presentado) FROM insiders").fetchone()
    ultima = fila[0] if fila else None
    return ultima, bool(ultima and ultima >= hasta)


def controlar(
    con: Any,
    activo: str,
    fecha_senal: date,
    horizonte: int,
    excluidos: Iterable[str] = (),
) -> Control:
    """Contrasta una señal contra su quintil de liquidez. Levanta si no se puede.

    `excluidos` son las acciones con señal propia en la ventana: si el control
    las contiene, contiene el efecto que se mide.
    """
    sesiones = sesiones_desde(con, fecha_senal, horizonte + 1)
    if len(sesiones) < horizonte + 1:
        raise SinDatos(
            f"{activo}: faltan sesiones tras {fecha_senal} "
            f"({len(sesiones)} de {horizonte + 1})"
        )
    f_ent, f_sal = sesiones[0], sesiones[horizonte]

    universo = con_quintil(universo_liquido(con, fecha_senal))
    fila = universo.filter(pl.col("activo") == activo)
    if fila.is_empty():
        raise SinDatos(f"{activo}: fuera del universo el {fecha_senal}")
    quintil = int(fila["quintil"][0])

    fuera = {activo, *excluidos}
    pares = universo.filter(
        (pl.col("quintil") == quintil) & (~pl.col("activo").is_in(list(fuera)))
    )["activo"].to_list()

    r = retornos(con, [activo, *pares], f_ent, f_sal)
    propio = r.filter(pl.col("activo") == activo)
    if propio.is_empty():
        raise SinDatos(f"{activo}: sin precios de {f_ent} a {f_sal}")

    control = r.filter(pl.col("activo") != activo)
    if control.height < CONTROL_MINIMO:
        raise SinDatos(
            f"{activo}: control de {control.height} acciones, "
            f"se exigen {CONTROL_MINIMO}"
        )

    return Control(
        activo=activo,
        fecha_senal=str(fecha_senal),
        horizonte=horizonte,
        quintil=quintil,
        n_control=control.height,
        retorno=round(float(propio["retorno"][0]), 3),
        retorno_control=round(float(control["retorno"].mean()), 3),
        entrada=str(f_ent),
        salida=str(f_sal),
    )
