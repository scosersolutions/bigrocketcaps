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


def horquilla_de_equilibrio(exceso_medio_pct: float, direccion: str) -> float:
    """A que horquilla, en bps, una hipotesis deja de ganar dinero.

    ## Por que esta pregunta y no la otra

    El criterio de coste de siempre es "¿pasa el filtro a X bps?", y necesita
    saber cuanto vale X. Aqui no se sabe: la horquilla no se puede medir gratis
    -IEX cobra 500 $/mes- y lo que hay es un estimador que sobrestima por lo
    menos a la mitad. Con esa incertidumbre, un "no pasa" no significa nada.

    Esta pregunta se contesta al reves y no necesita estimar nada: dado el
    exceso que la hipotesis produce, ¿hasta que coste sigue siendo rentable?
    Sale del propio backtest, no se puede afinar a conveniencia, y el lector
    la compara con la realidad que conozca -o con la cota de
    `cota_por_activo`- sin tener que creerse ningun numero nuestro.

    Ida y vuelta se paga una horquilla ENTERA: media al entrar y media al
    salir, que es lo que cobra `CostesPorAccion.slippage()` por operacion.
    Asi que el equilibrio en bps es la ganancia bruta en por ciento por cien.

    Cero significa que no funciona ni siendo gratis operar: el exceso ya va en
    contra antes de pagar nada. No es un caso raro; es el de B1.
    """
    if direccion not in ("corto", "largo"):
        raise ValueError('direccion debe ser "corto" o "largo"')
    bruto = -exceso_medio_pct if direccion == "corto" else exceso_medio_pct
    return round(max(bruto, 0.0) * 100, 2)


def _pares_ajustados(precios: pl.DataFrame) -> pl.DataFrame:
    """Pares de sesiones consecutivas, con el salto entre ellas ya corregido.

    `_h0/_l0` son el maximo y el minimo del primer dia; `_h1/_l1` los del
    segundo, desplazados hasta pegarlos al CIERRE del primero si el rango
    quedaba entero por encima o por debajo. Ese hueco es precio que se movio,
    no horquilla.

    Sale aparte porque lo usan el estimador y el calculo del sesgo, y tener
    dos copias del ajuste seria tener dos criterios que pueden separarse.
    """
    d = precios.sort(["activo", "fecha"])
    h0 = pl.col("high").shift(1).over("activo")
    l0 = pl.col("low").shift(1).over("activo")
    c0 = pl.col("close").shift(1).over("activo")

    # Mismo orden de comprobacion que la implementacion de referencia.
    salto = (
        pl.when(pl.col("high") < c0).then(pl.col("high") - c0)
        .when(pl.col("low") > c0).then(pl.col("low") - c0)
        .otherwise(0.0)
    )
    return d.with_columns(
        h0.alias("_h0"), l0.alias("_l0"),
        (pl.col("high") - salto).alias("_h1"),
        (pl.col("low") - salto).alias("_l1"),
    ).drop_nulls(["_h0", "_l0"]).filter(
        (pl.col("_h0") > 0) & (pl.col("_l0") > 0)
        & (pl.col("_h1") > 0) & (pl.col("_l1") > 0)
    )


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

    d = _pares_ajustados(precios)

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


#: sqrt(2) y 1+sqrt(2) aparecen en todas las expresiones del sesgo.
RAIZ2 = 2 ** 0.5
UNO_MAS_RAIZ2 = 1 + RAIZ2


def cota_superior(precios: pl.DataFrame, *,
                 predictor: str = "doble") -> pl.DataFrame:
    """Cota superior sobre el spread VERDADERO, por sesion.

    Tremacoldi-Rossi e Irwin (2021), seccion 5.1. El sesgo del estimador de
    Corwin-Schultz se descompone en dos partes (su Proposicion 2):

        E[S_HL] - S = (1+r2)*r_min*(phi-r2)   [sesgo de momento]
                    + (1+r2)*(r2*R_min - R*)  [sesgo de muestra pequeña]

    con `r_t = ln(H_t/L_t)` el rango diario observado, `r_min` y `r_max` el
    menor y el mayor del par, `kappa = r_max/r_min` y `phi = sqrt(1+kappa^2)`.

    El sesgo de momento es SIEMPRE no negativo, porque `phi >= sqrt(2)` por
    construccion, y se calcula entero con datos observables. El de muestra
    pequeña depende de rangos verdaderos, que no se ven; pero solo hace falta
    su SIGNO, y su ecuacion (16) lo predice con observables:

        SSB ~ (1+r2)*(r2*r_min - r*)      con r* el rango de las DOS sesiones

    De ahi su condicion (14): cuando ese signo es positivo,

        S < S_HL - sesgo de momento

    Es decir: la estimacion menos el sesgo de momento es un techo para la
    horquilla de verdad. No dice cuanto vale; dice cuanto NO puede valer, que
    es justo lo que faltaba para poder usar un estimador que sobrestima.

    Devuelve (activo, fecha, cota_pct) solo para las sesiones donde la cota
    aplica: estimacion positiva y sesgo de muestra pequeña positivo. En las
    demas el signo del sesgo total es indeterminado y el articulo no acota.
    """
    if precios.is_empty():
        return pl.DataFrame(schema={"activo": pl.Utf8, "fecha": pl.Date,
                                    "cota_pct": pl.Float64,
                                    "estimacion_pct": pl.Float64})
    d = _pares_ajustados(precios)

    r0 = (pl.col("_h0") / pl.col("_l0")).log()
    r1 = (pl.col("_h1") / pl.col("_l1")).log()
    r_min = pl.min_horizontal(r0, r1)
    r_max = pl.max_horizontal(r0, r1)
    # `r*`: el rango de las dos sesiones juntas, que es la raiz de gamma.
    r_estrella = (pl.max_horizontal("_h0", "_h1")
                  / pl.min_horizontal("_l0", "_l1")).log()

    phi = (1 + (r_max / r_min) ** 2).sqrt()
    sesgo_momento = UNO_MAS_RAIZ2 * r_min * (phi - RAIZ2)

    # Los dos predictores del SIGNO del sesgo de muestra pequeña. El articulo
    # pide reportar ambos cuando no hay spread efectivo con el que validar,
    # que es exactamente nuestro caso: si coinciden, la cota no depende de
    # cual se eligio.
    if predictor == "doble":
        # Ecuacion (16), la que ellos usan: acierta el signo verdadero mas a
        # menudo en su muestra.
        sesgo_muestra = UNO_MAS_RAIZ2 * (RAIZ2 * r_min - r_estrella)
    else:
        # Ecuacion (15) con el rango verdadero sustituido por el observado.
        # Esa sustitucion SOBREestima el sesgo, asi que se le escapan algunos
        # negativos tomandolos por positivos: sus falsos positivos son
        # esperados, no ruido.
        eta0 = (pl.col("_h0").log() + pl.col("_l0").log()) / 2
        eta1 = (pl.col("_h1").log() + pl.col("_l1").log()) / 2
        delta_r = r_min - r_max
        delta_eta = pl.max_horizontal(eta0, eta1) - pl.min_horizontal(eta0, eta1)
        sesgo_muestra = r_min + UNO_MAS_RAIZ2 * (0.5 * delta_r - delta_eta)

    estimacion = corwin_schultz(precios, recortar=False).sort(["activo", "fecha"])
    return (d.sort(["activo", "fecha"])
            .with_columns(
                estimacion["horquilla_pct"].alias("_s_hl"),
                (sesgo_momento * 100).alias("_momento"),
                sesgo_muestra.alias("_muestra"))
            .filter((pl.col("_s_hl") > 0) & (pl.col("_muestra") > 0))
            .with_columns((pl.col("_s_hl") - pl.col("_momento")).alias("cota_pct"),
                         pl.col("_s_hl").alias("estimacion_pct"))
            .select("activo", "fecha", "cota_pct", "estimacion_pct"))


def cota_por_activo(precios: pl.DataFrame, *,
                   predictor: str = "doble") -> dict[str, dict]:
    """Por activo, y comparando lo comparable.

    ## El error que hay que no cometer al leer esto

    La cota solo existe en las sesiones que cumplen la condicion del articulo
    -estimacion positiva y sesgo de muestra pequeña positivo-, que en esta
    muestra son una de cada cuatro. Y no son sesiones cualesquiera: son las de
    rango ancho, donde el estimador da numeros MAS altos. Comparar esa cota
    con la estimacion agregada de TODAS las sesiones da que la cota es mayor,
    y de ahi se saldria con la conclusion absurda de que acotar empeora.

    Por eso aqui se devuelven los dos numeros medidos SOBRE LAS MISMAS
    sesiones: `estimacion_cruda_bps` es la mediana mensual del estimador en
    las que cumplen la condicion, y `cota_bps` la del techo en esas mismas.
    La diferencia entre ambos es sesgo de momento demostrado, no supuesto.

    `sesgo_minimo_pct` es que fraccion de lo que dice el estimador en esas
    sesiones es, como minimo, sesgo. Es el numero con mas contenido de todo
    el modulo: no depende de creerse el nivel del estimador.
    """
    cotas = cota_superior(precios, predictor=predictor)
    if cotas.is_empty():
        return {}
    mensual = (cotas
               .with_columns(pl.col("fecha").dt.truncate("1mo").alias("mes"))
               .group_by("activo", "mes")
               .agg(pl.col("cota_pct").median(),
                    pl.col("estimacion_pct").median()))
    agregado = mensual.group_by("activo").agg(
        (pl.col("cota_pct").median() * 100).round(2).alias("cota_bps"),
        (pl.col("estimacion_pct").median() * 100).round(2).alias("estimacion_cruda_bps"),
        pl.len().alias("meses"))
    cuenta = cotas.group_by("activo").len()
    sesiones = dict(zip(cuenta["activo"], cuenta["len"], strict=True))
    salida = {}
    for f in agregado.to_dicts():
        cruda, cota = f["estimacion_cruda_bps"], f["cota_bps"]
        salida[f["activo"]] = {
            "cota_bps": cota,
            "estimacion_cruda_bps": cruda,
            "sesgo_minimo_pct": round((cruda - cota) / cruda * 100, 1) if cruda else None,
            "meses": f["meses"],
            "sesiones_con_cota": sesiones.get(f["activo"], 0),
        }
    return salida


def acuerdo_entre_predictores(precios: pl.DataFrame) -> dict[str, float]:
    """Que fraccion de sesiones acotan IGUAL los dos predictores, por activo.

    El articulo pide reportar los dos "especialmente cuando no se dispone del
    spread efectivo con el que validar", que es literalmente nuestro caso: no
    tenemos bid/ask con el que comprobar nada. Si los dos marcan las mismas
    sesiones, la cota no depende de cual se eligio; si no, elegir uno es una
    decision que habria que justificar y ahora mismo no habria con que.

    Uno de los dos -el de la ecuacion (15)- sobrestima el sesgo por
    construccion, asi que se espera que marque de mas. Lo que importa no es
    que coincidan al 100 %, sino cuanto.
    """
    doble = cota_superior(precios, predictor="doble")
    simple = cota_superior(precios, predictor="simple")
    if doble.is_empty() and simple.is_empty():
        return {}
    marcadas = (doble.select("activo", "fecha").with_columns(pl.lit(True).alias("_d"))
                .join(simple.select("activo", "fecha").with_columns(pl.lit(True).alias("_s")),
                      on=["activo", "fecha"], how="full", coalesce=True)
                .with_columns(pl.col("_d").fill_null(False), pl.col("_s").fill_null(False)))
    acuerdo = marcadas.group_by("activo").agg(
        (pl.col("_d") == pl.col("_s")).mean().alias("acuerdo"))
    return {f["activo"]: round(f["acuerdo"] * 100, 1) for f in acuerdo.to_dicts()}


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
