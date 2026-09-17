"""Horquilla estimada a partir de maximos y minimos diarios (Corwin-Schultz).

## Por que estimarla y no medirla

Medirla era el plan -`scripts/medir_spread_us.py`- y dejo de poder serlo: desde
el 1 de febrero de 2025 IEX exige un Data Subscriber Agreement firmado para el
TOPS en tiempo real, y sin el, Tiingo devuelve `bidPrice` y `askPrice` a NULL
(sus campos van marcados "IEX entitlement required" en la documentacion).
La tarifa de IEX vigente el 2026-09-17 pone el TOPS en tiempo real en 500 $/mes;
el retardado es gratis pero llega como feed binario, no por REST. Con el suelo
de coste en cero, esa puerta esta cerrada.

## Por que esto es MEJOR que lo que habia, y no un apaño

El medidor daba la horquilla de HOY. Su propio JSON lo escribia como
limitacion: "aplicarlo a un historico supone que la liquidez no ha cambiado, y
ha cambiado". Las hipotesis se juzgan sobre 2018-2024, asi que el numero que
hace falta no es el de hoy: es el de CADA dia de ese periodo. Corwin-Schultz lo
da, porque solo necesita maximos y minimos diarios, que ya estan en el lago.

Y el sesgo cae del lado seguro: el estimador tiende a SOBREestimar la horquilla
de los valores poco liquidos. Un coste sobrestimado mata hipotesis que quiza
sobrevivirian; uno subestimado aprueba hipotesis que pierden dinero. Para un
juez que existe para refutar, equivocarse caro es lo correcto.

Misma sustitucion declarada que SIC -> Fama-French en `brc.datos.sector`: el
dato de pago se cambia por un sustituto libre, citable, y dicho en voz alta.

## La fuente

Corwin, Shane A., y Paul Schultz (2012), "A Simple Way to Estimate Bid-Ask
Spreads from Daily High and Low Prices", The Journal of Finance 67(2), 719-760.

Para un par de sesiones consecutivas, con H y L el maximo y el minimo:

    beta  = ln(H_1/L_1)^2 + ln(H_2/L_2)^2
    gamma = ln(max(H_1,H_2) / min(L_1,L_2))^2
    alpha = (sqrt(2*beta) - sqrt(beta)) / (3 - 2*sqrt(2))
            - sqrt(gamma / (3 - 2*sqrt(2)))
    S     = 2 * (e^alpha - 1) / (1 + e^alpha)

El salto de una sesion a otra se corrige como manda el articulo: si el rango
del segundo dia queda entero por encima -o por debajo- del CIERRE del primero,
se desplaza hasta pegarlo, porque ese hueco es movimiento de precio y no
horquilla. La referencia es el cierre, no el maximo ni el minimo del dia
anterior: comprobado contra la implementacion de referencia de Bernt Arne
Odegaard (`high_low_spread_estimator_adjust_overnight.R`), que es la que
replica el articulo.
"""
from __future__ import annotations

import polars as pl

#: 3 - 2*sqrt(2). Aparece dos veces en alpha y tiene nombre en el articulo.
K = 3 - 2 * (2 ** 0.5)


def corwin_schultz(precios: pl.DataFrame, *, recortar: bool = True) -> pl.DataFrame:
    """Horquilla estimada por sesion, en porcentaje del precio.

    `precios` necesita (activo, fecha, high, low, close). Devuelve (activo, fecha,
    horquilla_pct), donde `fecha` es la SEGUNDA sesion del par: es la primera
    fecha en la que esa estimacion se podria haber usado.

    Las estimaciones negativas se dejan en cero, como recomienda el articulo
    para promediarlas: una horquilla negativa no existe, es ruido del
    estimador cuando la volatilidad del par es baja.
    """
    if precios.is_empty():
        return pl.DataFrame(schema={"activo": pl.Utf8, "fecha": pl.Date,
                                    "horquilla_pct": pl.Float64})

    d = precios.sort(["activo", "fecha"])
    h0 = pl.col("high").shift(1).over("activo")
    l0 = pl.col("low").shift(1).over("activo")
    c0 = pl.col("close").shift(1).over("activo")

    # El hueco entre sesiones es precio que se movio, no horquilla: se pega el
    # rango del segundo dia al cierre del primero antes de medir nada. Mismo
    # orden de comprobacion que la implementacion de referencia.
    salto = (
        pl.when(pl.col("high") < c0).then(pl.col("high") - c0)
        .when(pl.col("low") > c0).then(pl.col("low") - c0)
        .otherwise(0.0)
    )
    d = d.with_columns(
        h0.alias("_h0"), l0.alias("_l0"),
        (pl.col("high") - salto).alias("_h1"),
        (pl.col("low") - salto).alias("_l1"),
    ).drop_nulls(["_h0", "_l0"]).filter(
        (pl.col("_h0") > 0) & (pl.col("_l0") > 0)
        & (pl.col("_h1") > 0) & (pl.col("_l1") > 0)
    )

    beta = (pl.col("_h0") / pl.col("_l0")).log() ** 2 + (
        pl.col("_h1") / pl.col("_l1")).log() ** 2
    gamma = (pl.max_horizontal("_h0", "_h1")
             / pl.min_horizontal("_l0", "_l1")).log() ** 2
    alfa = ((2 * beta).sqrt() - beta.sqrt()) / K - (gamma / K).sqrt()
    s = 2 * (alfa.exp() - 1) / (1 + alfa.exp())

    valor = (
        pl.when(s > 0).then(s * 100).otherwise(0.0) if recortar
        # Sin recortar se conserva el SIGNO, que es informacion: ver
        # `frecuencia_negativos`. La magnitud negativa no significa nada, asi
        # que se devuelve el valor crudo tal cual sale de la formula.
        else pl.when(s > 0).then(s * 100).otherwise(s * 100)
    )
    return d.with_columns(valor.alias("horquilla_pct")).select(
        "activo", "fecha", "horquilla_pct")


def abdi_ranaldo(precios: pl.DataFrame, *, recortar: bool = True) -> pl.DataFrame:
    """Horquilla estimada por sesion con el estimador CHL, en porcentaje.

    Abdi, Farshid, y Angelo Ranaldo (2017), "A Simple Estimation of Bid-Ask
    Spreads from Daily Close, High, and Low Prices", The Review of Financial
    Studies 30(12), 4437-4480. Comprobado contra la implementacion de
    referencia de Bernt Arne Odegaard (`abdi_renaldo_estimator.R`).

        eta_t = (ln H_t + ln L_t) / 2          (el "medio" del dia)
        S^2   = 4 * (ln C_1 - eta_1) * (ln C_1 - eta_2)
        S     = sqrt(S^2) si es positivo, y 0 si no

    Usa el CIERRE ademas del rango, que es la diferencia con Corwin-Schultz y
    la razon de que aguante mejor los valores liquidos: el cierre cae en el bid
    o en el ask, asi que su distancia al medio del dia ES media horquilla,
    mientras que CS tiene que deducirla de comparar rangos de uno y dos dias, y
    ahi la volatilidad se come la señal cuando la horquilla es pequeña.
    """
    if precios.is_empty():
        return pl.DataFrame(schema={"activo": pl.Utf8, "fecha": pl.Date,
                                    "horquilla_pct": pl.Float64})

    d = precios.sort(["activo", "fecha"])
    h0, l0 = pl.col("high").shift(1).over("activo"), pl.col("low").shift(1).over("activo")
    c0 = pl.col("close").shift(1).over("activo")
    d = d.with_columns(h0.alias("_h0"), l0.alias("_l0"), c0.alias("_c0")).drop_nulls(
        ["_h0", "_l0", "_c0"]).filter(
        (pl.col("_h0") > 0) & (pl.col("_l0") > 0) & (pl.col("_c0") > 0)
        & (pl.col("high") > 0) & (pl.col("low") > 0)
        # Una sesion sin rango no dice nada del libro y rompe la cuenta.
        & (pl.col("_h0") > pl.col("_l0")) & (pl.col("high") > pl.col("low"))
    )

    eta0 = (pl.col("_h0").log() + pl.col("_l0").log()) / 2
    eta1 = (pl.col("high").log() + pl.col("low").log()) / 2
    s2 = 4 * (pl.col("_c0").log() - eta0) * (pl.col("_c0").log() - eta1)

    valor = (
        pl.when(s2 > 0).then(s2.sqrt() * 100).otherwise(0.0) if recortar
        # Sin recortar se conserva el SIGNO, que es informacion: ver
        # `frecuencia_negativos`. La magnitud negativa no significa nada, asi
        # que se devuelve el valor crudo tal cual sale de la formula.
        else pl.when(s2 > 0).then(s2.sqrt() * 100).otherwise(-((-s2).sqrt()) * 100)
    )
    return d.with_columns(valor.alias("horquilla_pct")).select(
        "activo", "fecha", "horquilla_pct")


#: Regla de negatividad de Tremacoldi-Rossi e Irwin (2021), seccion 5: con una
#: fraccion de estimaciones diarias negativas por encima del 40 %, la cota
#: implicita sobre el spread VERDADERO es del 0,2 %. Las dos cifras son suyas,
#: no elegidas aqui.
UMBRAL_NEGATIVOS = 0.40
COTA_SI_MUCHOS_NEGATIVOS_BPS = 20.0


def frecuencia_negativos(
    precios: pl.DataFrame, *, metodo=corwin_schultz,
) -> dict[str, float]:
    """Fraccion de estimaciones diarias que salen NEGATIVAS, por activo.

    Es el dato que este modulo tiraba a la basura. Poner las negativas a cero
    es lo que manda el articulo original para promediar, pero cuantas hubo es
    informacion sobre el propio estimador: una horquilla negativa no existe, y
    que aparezca dice que el rango del par de sesiones no converge, que es lo
    que pasa cuando la horquilla verdadera es pequeña frente a la volatilidad.

        "lower levels of spread (and higher price volatility) increase the
        frequency of negative estimates"
        - Tremacoldi-Rossi e Irwin (2021)
    """
    crudas = metodo(precios, recortar=False)
    if crudas.is_empty():
        return {}
    agregado = crudas.group_by("activo").agg(
        (pl.col("horquilla_pct") < 0).mean().alias("negativos")
    )
    return {f["activo"]: round(f["negativos"], 4) for f in agregado.to_dicts()}


def diagnostico_sesgo(
    precios: pl.DataFrame, *, metodo=corwin_schultz,
) -> dict[str, dict]:
    """Por activo: cuantas estimaciones salieron negativas, y si eso es mala señal.

    ## Lo que se midio, y lo que NO se concluye

    El articulo da una regla: con mas del 40 % de estimaciones negativas, la
    cota implicita sobre el spread verdadero es 0,2 %. Aplicada activo por
    activo a esta muestra, marca a 16 de 21 -incluidos SOUN, FCEL y PLUG-, y
    "la horquilla de SOUN es como mucho 20 bps" no se lo cree nadie que haya
    intentado comprarlo. Asi que la cota NO se publica: la regla vive en su
    articulo, con el resto de su test, y sacarla de ahi la rompe.

    Lo que si dice el numero, y es mas util de lo que parece: la fraccion de
    negativas ronda el 40 % en TODA la muestra, de AAPL a SOUN. El articulo
    demuestra que esa fraccion sube cuando la horquilla verdadera es pequeña
    frente a la volatilidad, o sea cuando el estimador esta fuera de su zona
    buena. Que salga plana y alta en los 21 valores significa que el problema
    de nivel no es solo de los liquidos: es de todo el universo que miramos.

    Eso convierte una sospecha en una medida. Antes se sabia que AAPL a 49 bps
    era absurdo; ahora se sabe que tampoco hay que fiarse del 229 de SOUN.

    ## Lo que falta

    El test completo del sesgo de momento -`(1+sqrt(2))*r_min/(phi-sqrt(2))`
    con los dos predictores del signo del sesgo de muestra pequeña- da una cota
    por activo de verdad. Depende de definiciones que no he podido verificar
    enteras, y un numero mal implementado que parece riguroso es peor que no
    tenerlo. Queda citado y pendiente, no escondido.
    """
    negativos = frecuencia_negativos(precios, metodo=metodo)
    return {
        activo: {
            "negativos_pct": round(fraccion * 100, 2),
            "fuera_de_zona_buena": fraccion >= UMBRAL_NEGATIVOS,
        }
        for activo, fraccion in negativos.items()
    }


def por_mes(estimaciones: pl.DataFrame) -> pl.DataFrame:
    """Media de las estimaciones diarias dentro de cada mes, por activo.

    El articulo calcula asi sus estimaciones mensuales, y no es un detalle de
    presentacion: dia a dia el estimador tiene una varianza enorme -de un par
    de sesiones sale tanto un 0 como un 30 %-, asi que cualquier percentil
    sobre las diarias mide sobre todo el ruido del propio estimador. La media
    mensual es la unidad que el metodo pretende estimar.
    """
    if estimaciones.is_empty():
        return pl.DataFrame(schema={"activo": pl.Utf8, "mes": pl.Date,
                                    "horquilla_pct": pl.Float64})
    return (estimaciones
            .with_columns(pl.col("fecha").dt.truncate("1mo").alias("mes"))
            .group_by("activo", "mes")
            .agg(pl.col("horquilla_pct").mean())
            .sort("activo", "mes"))


def resumen_por_activo(estimaciones: pl.DataFrame) -> dict[str, dict]:
    """Mediana, p90 y maximo de los MESES de cada activo, en puntos basicos.

    Mismos tres nombres que producia `medir_spread_us.py`, para que el fichero
    de salida no cambie de forma al cambiar de fuente y `calibrar_costes_us.py`
    y los widgets del Centro de Control sigan leyendolo. Lo que cambia es la
    unidad de lo que se agrega: alli eran instantaneas del libro dentro de una
    sesion, aqui son meses. `n` es el numero de meses.
    """
    mensual = por_mes(estimaciones)
    if mensual.is_empty():
        return {}
    bps = pl.col("horquilla_pct") * 100
    agregado = mensual.group_by("activo").agg(
        pl.len().alias("n"),
        bps.median().round(2).alias("mediana_bps"),
        bps.quantile(0.9).round(2).alias("p90_bps"),
        bps.max().round(2).alias("max_bps"),
    ).sort("activo")
    return {f["activo"]: {"n": f["n"], "mediana_bps": f["mediana_bps"],
                          "p90_bps": f["p90_bps"], "max_bps": f["max_bps"]}
            for f in agregado.to_dicts()}
