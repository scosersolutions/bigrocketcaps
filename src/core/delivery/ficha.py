"""Los cuatro bloques de una ficha de valor, en un solo sitio.

Viven aquí y no en un script porque los usan dos: `scripts/ficha.py`, que la
imprime para un valor, y `scripts/dashboard.py`, que las precalcula todas para
el cuaderno. Duplicarlos garantizaría que un día dijeran cosas distintas.

## La regla que estos bloques no rompen

**El sistema no fabrica un precio objetivo.** Midió que la sesión de resultados
multiplica el movimiento pero que su dirección es simétrica, y la deriva
posterior —E5, PEAD— está refutada. De ahí no sale ninguna dirección, y eso
sigue en `infoproyecto.md`.

Lo que sí sale, desde `data/objetivos.py`, es el objetivo **de los analistas**:
un dato de terceros, publicado, con su mínimo y su máximo. No enseñarlo era una
laguna de datos, no una postura. Y `encaje()` añade lo único que la fuente no
trae: cuánto pide ese objetivo comparado con lo que el propio valor ha hecho.

## Los analistas van contextualizados, no citados

`docs/research/analistas.md` midió que el consenso bate el 67,5 % de las veces y
que ese sesgo crece con la cobertura: del 59,7 % con un analista al 84,8 % con
dieciséis o más. Así que un «batió 7 de 8» hay que leerlo contra el tramo que le
corresponde y no contra el 50 % que uno supondría.
"""

from __future__ import annotations

import json
import statistics as st
from datetime import date, timedelta

from core.data.hechos import ITEMS as ITEMS_8K
from core.store.duck import Store

#: Tasa a la que se bate el consenso según cuántos analistas cubren el valor.
#: Medido sobre 56.984 anuncios en `docs/research/analistas.md`.
BATE_POR_COBERTURA = (
    (1, 1, 0.597), (2, 3, 0.647), (4, 7, 0.720), (8, 15, 0.771), (16, 10_000, 0.848)
)

#: Por debajo de este EPS previsto el porcentaje de sorpresa no significa nada:
#: prever 0,01 y salir 0,03 son «+200 %» y dos céntimos.
EPS_MINIMO = 0.05


def referencia_bate(n: float | None) -> float | None:
    if not n:
        return None
    for lo, hi, tasa in BATE_POR_COBERTURA:
        if lo <= n <= hi:
            return tasa
    return None


def insiders(store: Store, act: str) -> dict:
    """`insiders` ya contiene SOLO compras en mercado abierto —el código P del
    Form 4— y su columna `transaccion` es la fecha del hecho, no el tipo.

    Se agrupa por `presentado`, que es cuando el dato se hizo público: un Form 4
    se presenta hasta dos días hábiles después de la compra, y agrupar por la
    fecha del hecho metería futuro.
    """
    filas = store.con.execute(
        """
        SELECT presentado, count(DISTINCT cik_insider) AS n,
               sum(valor) AS importe, max(desfase) AS desfase
        FROM insiders WHERE activo = ? GROUP BY 1 ORDER BY 1 DESC LIMIT 8
        """,
        [act],
    ).fetchall()
    total = store.con.execute(
        "SELECT count(*), count(DISTINCT cik_insider), sum(valor), max(presentado) "
        "FROM insiders WHERE activo = ?",
        [act],
    ).fetchone()
    return {
        "recientes": [{"fecha": str(f), "insiders": n, "importe": imp, "desfase": d}
                      for f, n, imp, d in filas],
        "compras": total[0], "distintos": total[1],
        "importe": total[2], "ultima": str(total[3]) if total[3] else None,
    }


def resultados(store: Store, act: str, hoy: date | None = None) -> dict:
    filas = store.con.execute(
        """
        SELECT fecha, eps_previsto, eps_real, sorpresa_pct, n_estimaciones, capitalizacion
        FROM calendario WHERE activo = ? AND eps_real IS NOT NULL
        ORDER BY fecha DESC LIMIT 12
        """,
        [act],
    ).fetchall()
    if not filas:
        return {}
    utiles = [f for f in filas if f[1] and abs(f[1]) >= EPS_MINIMO]
    cob = st.median([f[4] for f in filas if f[4]]) if any(f[4] for f in filas) else None
    prox = store.con.execute(
        "SELECT fecha, n_estimaciones FROM calendario WHERE activo = ? AND fecha > ? "
        "ORDER BY fecha LIMIT 1",
        [act, hoy or date.today()],
    ).fetchone()
    return {
        "historial": [{"fecha": str(f[0]), "previsto": f[1], "real": f[2],
                       "sorpresa": f[3], "analistas": f[4]} for f in filas[:8]],
        "bate": sum(1 for f in utiles if f[2] > f[1]), "de": len(utiles),
        "cobertura": cob, "referencia": referencia_bate(cob),
        "capitalizacion": next((f[5] for f in filas if f[5]), None),
        "proximo": str(prox[0]) if prox else None,
    }


def fundamentales(store: Store, act: str) -> dict:
    filas = store.con.execute(
        """
        SELECT concepto, fin, publicado, valor, formulario FROM fundamentales
        WHERE activo = ? AND formulario IN ('10-K', '10-Q') ORDER BY concepto, fin
        """,
        [act],
    ).fetchall()
    if not filas:
        return {}
    por: dict[str, list] = {}
    for c, fin, pub, val, form in filas:
        por.setdefault(c, []).append(
            {"fin": str(fin), "publicado": str(pub), "valor": val, "formulario": form}
        )
    fuera = {}
    for c, v in por.items():
        # Un mismo cierre se publica varias veces: el 10-K original y los
        # posteriores que lo repiten como comparativo. Se queda la PRIMERA
        # publicación, que es cuando el dato se supo; la última daría la cifra
        # revisada y fecharía mal cuándo estuvo disponible.
        primera: dict[str, dict] = {}
        for x in v:
            if x["formulario"] != "10-K":
                continue
            if x["fin"] not in primera or x["publicado"] < primera[x["fin"]]["publicado"]:
                primera[x["fin"]] = x
        fuera[c] = {"ultimo": v[-1], "anuales": [primera[k] for k in sorted(primera)][-5:]}
    return fuera


def riesgo(store: Store, act: str, mercado: str = "stock_us") -> dict:
    """Cuánto se mueve un día normal y cuánto en sesión de resultados.

    Es el ÚNICO bloque que el proyecto ha validado como útil, y solo como filtro
    de riesgo: la dirección de ese movimiento es simétrica.
    """
    px = store.con.execute(
        "SELECT ts::DATE, open, close FROM ohlcv WHERE activo = ? AND mercado = ? "
        "AND timeframe = '1d' ORDER BY ts",
        [act, mercado],
    ).fetchall()
    if len(px) < 60:
        return {}
    res = {r[0] for r in store.con.execute(
        "SELECT fecha FROM calendario WHERE activo = ?", [act]).fetchall()}
    normales, anuncio = [], []
    for f, o, c in px:
        if not o or o <= 0:
            continue
        # La reacción es la sesión del anuncio o la siguiente, según si se
        # publicó antes de abrir o después de cerrar.
        (anuncio if (f in res or f - timedelta(days=1) in res) else normales).append(
            abs(c / o - 1)
        )
    if len(normales) < 30:
        return {}
    mn = st.median(normales)
    ma = st.median(anuncio) if len(anuncio) >= 4 else None
    return {
        "sesiones": len(px), "normal": mn, "resultados": ma,
        "n_resultados": len(anuncio),
        "razon": (ma / mn) if ma and mn > 0 else None,
    }


def analistas(store: Store, act: str) -> dict:
    """El consenso de precio objetivo, tal como lo publican los analistas.

    Es un dato de terceros, no una opinión del sistema: `data/objetivos.py`
    explica por qué llevaba tanto tiempo sin salir y por qué eso era una laguna
    y no una postura. `horizonte` viene a NULL porque la fuente no lo declara,
    y aquí no se rellena a ojo.
    """
    if not store.con.execute(
        "SELECT 1 FROM duckdb_tables() WHERE table_name = 'objetivos'"
    ).fetchone():
        return {}
    f = store.con.execute(
        "SELECT objetivo, minimo, maximo, compra, mantener, venta, casas, "
        "consenso, horizonte, consultado, postura, desde, serie FROM objetivos "
        "WHERE activo = ? ORDER BY consultado DESC LIMIT 1",
        [act],
    ).fetchone()
    if not f or f[0] is None:
        return {}
    n = (f[3] or 0) + (f[4] or 0) + (f[5] or 0)
    return {
        "objetivo": f[0], "minimo": f[1], "maximo": f[2],
        "compra": f[3], "mantener": f[4], "venta": f[5],
        "n": n or None, "casas": f[6], "consenso": f[7],
        "horizonte": f[8], "consultado": str(f[9]),
        "postura": f[10], "desde": f[11], "serie": _serie(f[12]),
    }


def _serie(bruto: str | None) -> list | None:
    """Una serie ilegible deja la ficha sin ese bloque, no sin ficha.

    No es paranoia de manual: una desalineación de columnas metió `source`
    dentro de `serie`, y un `json.loads` desnudo tumbó la generación entera del
    cuaderno por una celda mala de 174. El dato es decorativo; el cuaderno no.
    """
    if not bruto:
        return None
    try:
        v = json.loads(bruto)
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, list) else None


def encaje(store: Store, act: str, objetivo: float | None) -> dict:
    """Cuánto pide el objetivo de consenso, medido contra el propio valor.

    Ésta es la aportación que no está en la fuente. El objetivo implica una
    subida de X %; aquí se dice en qué proporción de sus ventanas de cada plazo
    este valor dio esa subida. No convierte el objetivo en pronóstico ni lo
    contradice: dice si pedirle eso a este valor es rutina o es raro.

    Réplica exacta de `encaje()` en `worker/ficha.js`. Si divergieran, el mismo
    valor diría dos cosas distintas según estuviera guardado o consultado en
    directo, que es peor que no decir nada.
    """
    if not objetivo:
        return {}
    px = [
        r[0]
        for r in store.con.execute(
            # Siempre bolsa: esto compara contra un objetivo de analistas, y de
            # un perpetuo no publica objetivos nadie.
            "SELECT close FROM ohlcv WHERE activo = ? AND mercado = 'stock_us' "
            "AND timeframe = '1d' AND close > 0 ORDER BY ts",
            [act],
        ).fetchall()
    ]
    if len(px) < 300 or not px[-1]:
        return {}
    pide = objetivo / px[-1] - 1.0
    por = {}
    for etiqueta, n in HORIZONTES:
        if len(px) < n * 3:
            continue
        r = [px[i + n] / px[i] - 1.0 for i in range(len(px) - n)]
        por[etiqueta] = {
            "llega": sum(1 for x in r if x >= pide) / len(r),
            "w": len(r),
        }
    return {"px": px[-1], "pide": pide, "por": por} if por else {}


def hechos_recientes(store: Store, act: str, n: int = 6) -> list[dict]:
    """Los últimos 8-K del valor, con el significado de cada item.

    Viajan en el cuaderno —y no se piden al vuelo como en la ficha viva— porque
    el listado de contraste compara todos los valores a la vez, y eso serían
    doscientas peticiones desde el navegador de cada lector.
    """
    if not store.con.execute(
        "SELECT 1 FROM duckdb_tables() WHERE table_name = 'hechos'"
    ).fetchone():
        return []
    filas = store.con.execute(
        "SELECT fecha, items, adverso FROM hechos WHERE activo = ? "
        "ORDER BY fecha DESC LIMIT ?",
        [act, n],
    ).fetchall()
    fuera = []
    for f, items, adverso in filas:
        cods = [c for c in (items or "").split(",") if c]
        fuera.append({
            "f": str(f),
            "i": [{"c": c, "t": ITEMS_8K.get(c, (c, False))[0],
                   "mal": ITEMS_8K.get(c, ("", False))[1]} for c in cods],
            "mal": bool(adverso),
        })
    return fuera


def completa(store: Store, act: str, hoy: date | None = None) -> dict:
    return {
        "activo": act,
        "insiders": insiders(store, act),
        "resultados": resultados(store, act, hoy),
        "fundamentales": fundamentales(store, act),
        "riesgo": riesgo(store, act),
        "analistas": analistas(store, act),
        "hechos": hechos_recientes(store, act),
    }


#: Conceptos que el cuaderno pinta. Los otros dos —patrimonio y acciones— se
#: descargan igual porque cuestan lo mismo, pero no se embeben: cada campo que
#: viaja en el HTML lo paga el lector en descarga.
CONCEPTOS_VISIBLES = ("ingresos", "beneficio_neto", "activos", "pasivos", "flujo_operativo")


def compacta(store: Store, act: str, hoy: date | None = None) -> dict | None:
    """La misma ficha, recortada para viajar dentro del cuaderno.

    Un HTML autocontenido paga en descarga cada campo que lleva dentro, así que
    aquí se quita todo lo que no se pinta: los conceptos que no se enseñan, las
    etiquetas XBRL, y los formularios de cada fila anual. Devuelve `None` si no
    hay nada que enseñar, para no ocupar sitio con fichas vacías.
    """
    f = completa(store, act, hoy)
    ins, res, fun, rie = f["insiders"], f["resultados"], f["fundamentales"], f["riesgo"]
    if not (ins["compras"] or res or fun or rie):
        return None

    out: dict = {"t": act}
    if ins["compras"]:
        out["i"] = {
            "n": ins["compras"], "d": ins["distintos"],
            "usd": round(ins["importe"] or 0),
            "ult": ins["ultima"],
            "r": [{"f": x["fecha"], "n": x["insiders"], "usd": round(x["importe"] or 0)}
                  for x in ins["recientes"][:5]],
        }
    if res:
        out["r"] = {
            "bate": res["bate"], "de": res["de"],
            "cob": res["cobertura"], "ref": res["referencia"],
            "cap": res["capitalizacion"], "prox": res["proximo"],
            "h": [{"f": x["fecha"], "p": x["previsto"], "r": x["real"], "s": x["sorpresa"]}
                  for x in res["historial"][:8]],
        }
    if fun:
        out["f"] = {
            c: {"v": fun[c]["ultimo"]["valor"], "fin": fun[c]["ultimo"]["fin"],
                "pub": fun[c]["ultimo"]["publicado"],
                "a": [[x["fin"], x["valor"]] for x in fun[c]["anuales"]]}
            for c in CONCEPTOS_VISIBLES if c in fun
        }
    if rie:
        out["v"] = {"n": rie["sesiones"], "dia": rie["normal"],
                    "res": rie["resultados"], "razon": rie["razon"],
                    "na": rie["n_resultados"]}
    ana = f.get("analistas") or {}
    if ana:
        out["a"] = {"obj": ana["objetivo"], "lo": ana["minimo"], "hi": ana["maximo"],
                    "compra": ana["compra"], "mantener": ana["mantener"],
                    "venta": ana["venta"], "n": ana["n"], "casas": ana["casas"],
                    "tipo": ana["consenso"], "horizonte": ana["horizonte"],
                    "postura": ana["postura"], "desde": ana["desde"],
                    "serie": ana["serie"]}
        enc = encaje(store, act, ana["objetivo"])
        if enc:
            out["e"] = {"px": round(enc["px"], 4), "pide": round(enc["pide"], 4),
                        "por": {k: {"llega": round(v["llega"], 4), "w": v["w"]}
                                for k, v in enc["por"].items()}}

    hh = f.get("hechos") or []
    if hh:
        out["k"] = hh

    hz = por_horizonte(store, act)
    if hz:
        out["h"] = [{"e": x["et"], "n": x["n"], "w": x["ventanas"],
                     "up": round(x["arriba"], 4), "p10": round(x["p10"], 4),
                     "med": round(x["med"], 4), "p90": round(x["p90"], 4),
                     "peor": round(x["peor"], 4)} for x in hz]
    return out


#: Perpetuos con menos historia que esto no dan ni la ventana de un mes.
MIN_VELAS_CRIPTO = 300


def compacta_cripto(store: Store, act: str) -> dict | None:
    """La ficha de un perpetuo, que es otra cosa y no una de acciones recortada.

    No lleva insiders porque no hay Form 4, ni 8-K porque no hay SEC, ni consenso
    de analistas porque nadie publica objetivos de precio de un perpetuo. Lo que
    sí lleva —y calculado con el MISMO código, a propósito— es el horizonte y la
    volatilidad: si divergieran, «lo que hizo a un año» significaría dos cosas
    según el mercado.

    Van dentro del cuaderno y no se piden al vuelo porque **Binance responde 403
    a Cloudflare Workers**, comprobado en sus tres hosts. Kraken y Coinbase sí
    responden pero topan en 720 velas diarias, que no llegan a la ventana anual.
    La base tiene desde 2017.
    """
    rie = riesgo(store, act, mercado="crypto_perp")
    hz = por_horizonte(store, act, mercado="crypto_perp")
    if not hz:
        return None

    fila = store.con.execute(
        "SELECT count(*), median(close * volume), last(close ORDER BY ts) FROM ohlcv "
        "WHERE activo = ? AND mercado = 'crypto_perp' AND timeframe = '1d'",
        [act],
    ).fetchone()

    out: dict = {"t": act, "mercado": "cripto", "velas": fila[0]}
    if fila[1]:
        out["vol"] = round(fila[1])
    if fila[2]:
        out["px"] = fila[2]
    if rie:
        # `res` y `razon` no existen aquí: no hay anuncios de resultados que
        # separar. Se omiten en vez de mandarlos a null.
        out["v"] = {"n": rie["sesiones"], "dia": rie["normal"]}
    out["h"] = [{"e": x["et"], "n": x["n"], "w": x["ventanas"],
                 "up": round(x["arriba"], 4), "p10": round(x["p10"], 4),
                 "med": round(x["med"], 4), "p90": round(x["p90"], 4),
                 "peor": round(x["peor"], 4)} for x in hz]
    return out


#: Horizontes del selector, en sesiones de mercado (~21 al mes).
HORIZONTES = (("1 mes", 21), ("3 meses", 63), ("6 meses", 126), ("1 año", 252))


def por_horizonte(store: Store, act: str, mercado: str = "stock_us") -> list[dict]:
    """Qué se puede decir de este valor a cada horizonte, y qué no.

    Todo lo de aquí es **frecuencia histórica**, no pronóstico: en ventanas
    solapadas de N sesiones, qué proporción acabaron arriba y entre qué valores
    se movieron. Que algo subiera el 58 % de las veces en el pasado no dice que
    vaya a subir; dice a qué se ha estado expuesto quien lo tuvo ese plazo.

    La distinción no es retórica. El proyecto midió que la dirección del
    movimiento en resultados es simétrica y que la deriva posterior (E5, PEAD)
    está refutada: no hay nada aquí que ordene comprar ni vender.
    """
    px = [
        r[0]
        for r in store.con.execute(
            "SELECT close FROM ohlcv WHERE activo = ? AND mercado = ? "
            "AND timeframe = '1d' AND close > 0 ORDER BY ts",
            [act, mercado],
        ).fetchall()
    ]
    if len(px) < 300:
        return []

    fuera = []
    for etiqueta, n in HORIZONTES:
        if len(px) < n * 3:
            continue
        # Ventanas SOLAPADAS: a horizontes largos las sin solape se cuentan con
        # los dedos. Para una frecuencia descriptiva sirven; para un contraste
        # no servirían, y por eso esto no es un contraste.
        r = [px[i + n] / px[i] - 1.0 for i in range(len(px) - n)]
        r.sort()
        fuera.append({
            "et": etiqueta, "n": n, "ventanas": len(r),
            "arriba": sum(1 for x in r if x > 0) / len(r),
            "p10": r[int(0.10 * len(r))],
            "med": r[len(r) // 2],
            "p90": r[int(0.90 * len(r))],
            "peor": r[0],
        })
    return fuera
