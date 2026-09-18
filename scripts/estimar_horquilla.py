"""Estima la horquilla de acciones US desde el OHLC diario del lago.

Sustituye a `scripts/medir_spread_us.py`, que dejo de poder medir nada: desde
el 1-feb-2025 IEX exige un Data Subscriber Agreement para el TOPS en tiempo
real, Tiingo devuelve `bidPrice`/`askPrice` a NULL sin el, y la tarifa de IEX
lo pone en 500 $/mes. El porque esta entero en `brc.estudio.horquilla`.

Escribe EL MISMO fichero y con la MISMA forma que escribia el medidor, asi que
`calibrar_costes_us.py` y los widgets del Centro de Control siguen leyendolo
sin enterarse de que ha cambiado la fuente. Lo que si cambia es el campo
`fuente`, que es donde se lee de donde sale el numero.

Uso:
    python scripts/estimar_horquilla.py
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl

from brc.datos import lago
from brc.estudio.horquilla import (abdi_ranaldo, corwin_schultz,
                                   cota_por_activo, diagnostico_sesgo,
                                   resumen_por_activo)

#: Los mismos 24 valores que muestreaba el medidor, y por el mismo motivo: van
#: de AAPL a CLOV a proposito, para cubrir la banda de liquidez entera en vez
#: de una punta. Se importan de alli para que no haya dos listas que se puedan
#: separar sin que nadie lo note.
from medir_spread_us import MUESTRA  # noqa: E402


def cargar(con: duckdb.DuckDBPyConnection, anyos: list[int],
           tickers: list[str]) -> pl.DataFrame:
    expr = lago.leer(con, "ohlcv", anyos)
    comillas = ", ".join(f"'{t}'" for t in tickers)
    return con.execute(
        f"SELECT activo, ts::DATE AS fecha, high, low, close FROM {expr} "
        f"WHERE mercado = 'stock_us' AND timeframe = '1d' "
        f"AND activo IN ({comillas}) "
        f"AND high > 0 AND low > 0 AND close > 0"
    ).pl()


def lectura_por_valor(resumen: dict[str, dict]) -> list[dict]:
    """Ticker a ticker: cuanto cuesta operarlo y que hace falta para ganar.

    ## Lo que esta tabla dice, y lo que NO

    NO dice que valores merece la pena comprar. No lo dice porque este
    laboratorio no lo sabe: B1 esta refutada y no hay ninguna hipotesis viva,
    asi que no hay ni una accion sobre la que tenga nada que opinar. Una tabla
    que dijera "compra esta" seria inventarsela.

    SI dice lo que cuesta ENTRAR Y SALIR de cada una, que es un hecho y no una
    opinion, y de ahi sale el umbral: una idea que gane menos que eso pierde
    dinero por mucho que acierte la direccion. Ida y vuelta se paga
    aproximadamente una horquilla entera -media al entrar y media al salir-,
    asi que el umbral en por ciento es la horquilla en bps entre cien.

    Sirve para descartar, que es lo unico que este sistema sabe hacer: si una
    idea futura promete un 0,3 % por operacion, esta tabla dice en que valores
    ni merece la pena probarla.
    """
    filas = []
    for t, v in sorted(resumen.items(), key=lambda kv: kv[1]["mediana_bps"]):
        umbral = v["mediana_bps"] / 100
        if umbral < 0.5:
            lectura = "de los baratos de operar, dentro de esta muestra"
        elif umbral < 1.5:
            lectura = "caro: solo para ideas que ganen bastante por operacion"
        else:
            lectura = "carisimo: casi nada compensa entrar y salir aqui"
        filas.append({
            "valor": t,
            "cuesta_entrar_y_salir_pct": round(umbral, 2),
            "hay_que_ganar_mas_de": f"{umbral:.2f} % por operacion",
            "lectura": lectura,
        })
    return filas


def tabla_de_valores(resumen: dict[str, dict],
                    diagnostico: dict[str, dict] | None = None,
                    cotas: dict[str, dict] | None = None) -> list[dict]:
    """Un valor por fila, del mas barato de operar al mas caro.

    `activos` es un diccionario de ticker a objeto, y eso el Centro de Control
    no lo pinta como tabla -solo pinta listas-, asi que la cifra agregada
    aparecia sola, sin decir sobre QUE valores se ha calculado. Esta lista es
    la respuesta a esa pregunta.
    """
    diagnostico = diagnostico or {}
    return [{"valor": t,
             "horquilla_tipica_bps": v["mediana_bps"],
             "mal_mes_bps": v["p90_bps"],
             "peor_mes_bps": v["max_bps"],
             "meses": v["n"],
             # Cuantas estimaciones diarias salieron negativas. Cuanto mas
             # alto, menos fiable es el NIVEL de la fila. Ver
             # `brc.estudio.horquilla.diagnostico_sesgo`.
             "estimaciones_absurdas_pct": diagnostico.get(t, {}).get("negativos_pct"),
             # De lo que dice el estimador, cuanto es sesgo DEMOSTRADO. Ver
             # `brc.estudio.horquilla.cota_por_activo`.
             "sesgo_minimo_pct": (cotas or {}).get(t, {}).get("sesgo_minimo_pct")}
            for t, v in sorted(resumen.items(), key=lambda kv: kv[1]["mediana_bps"])]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--anyo-desde", type=int, default=2018)
    p.add_argument("--anyo-hasta", type=int, default=2024)
    p.add_argument("--estimador", choices=("cs", "ar"), default="cs",
                   help="cs = Corwin-Schultz (por defecto), ar = Abdi-Ranaldo. "
                        "Dan niveles parecidos sobre esta muestra; se deja "
                        "elegir para poder contrastarlos, no porque haya duda.")
    p.add_argument("--salida", type=Path,
                   default=Path("data/experimentos/spread_us.json"))
    a = p.parse_args()

    anyos = list(range(a.anyo_desde, a.anyo_hasta + 1))
    con = lago.conectar()
    print(f"leyendo ohlcv {a.anyo_desde}-{a.anyo_hasta} de {len(MUESTRA)} valores...")
    precios = cargar(con, anyos, MUESTRA)
    print(f"  {precios.height:,} sesiones, {precios['activo'].n_unique()} valores")
    if precios.is_empty():
        print("el lago no devolvio ninguna sesion: no hay nada que estimar")
        return 1

    metodo = corwin_schultz if a.estimador == "cs" else abdi_ranaldo
    estimaciones = metodo(precios)
    resumen = resumen_por_activo(estimaciones)
    diagnostico = diagnostico_sesgo(precios, metodo=metodo)
    # La cota solo esta derivada para el estimador de rango alto-bajo.
    cotas = cota_por_activo(precios) if a.estimador == "cs" else {}
    if not resumen:
        print("ningun par de sesiones utilizable")
        return 1

    p90s = sorted(v["p90_bps"] for v in resumen.values())
    medio = len(p90s) // 2
    mediana_de_p90s = round(
        p90s[medio] if len(p90s) % 2 else (p90s[medio - 1] + p90s[medio]) / 2, 3)

    print(f"\n{'activo':<8} {'n':>6} {'mediana':>9} {'p90':>8} {'max':>8}")
    print("-" * 44)
    for t, v in sorted(resumen.items(), key=lambda kv: kv[1]["p90_bps"]):
        print(f"{t:<8} {v['n']:>6} {v['mediana_bps']:>9.2f} "
              f"{v['p90_bps']:>8.2f} {v['max_bps']:>8.2f}")

    a.salida.parent.mkdir(parents=True, exist_ok=True)
    a.salida.write_text(json.dumps({
        "medido": datetime.now(UTC).isoformat(),
        "fuente": ("corwin-schultz" if a.estimador == "cs" else "abdi-ranaldo")
                  + " sobre high/low/close diario del lago",
        "aviso": ("es un ESTIMADOR, no una medicion, y SOBRESTIMA el nivel "
                  "en los valores liquidos: da ~50 bps para AAPL, cuya "
                  "horquilla real es ~1 bp. El ORDEN entre valores si es "
                  "fiable. Uselo como COTA SUPERIOR -lo que sobrevive a este "
                  "coste sobrevive de verdad-, no como el coste real."),
        "limitacion": ("da una horquilla por sesion, no por orden: no recoge "
                       "que la horquilla se abre en la apertura ni que una "
                       "orden grande se come varios niveles del libro"),
        "periodo": f"{a.anyo_desde}-{a.anyo_hasta}",
        "sesiones": estimaciones.height,
        "fuera_de_zona_buena": sum(
            1 for v in diagnostico.values() if v["fuera_de_zona_buena"]),
        "que_significa_eso": (
            "cuantos de los valores tienen mas del 40 % de estimaciones diarias "
            "negativas. Una horquilla negativa no existe: que aparezcan tantas "
            "dice que el estimador esta fuera de su zona buena, o sea que el "
            "NIVEL no es de fiar. El ORDEN entre valores si lo es."),
        "titular": (f"Horquilla estimada de {len(resumen)} valores US entre "
                   f"{a.anyo_desde} y {a.anyo_hasta}. Es una COTA SUPERIOR: "
                   f"sirve para absolver una hipotesis, no para condenarla"),
        "que_son_estos_valores": ("los mismos que muestreaba el medidor: una "
                                 "escalera de liquidez de AAPL a CLOV, elegida "
                                 "para cubrir la banda entera y no una punta"),
        "valores": tabla_de_valores(resumen, diagnostico, cotas),
        "sesgo_minimo_mediano_pct": (
            round(statistics.median(
                [v["sesgo_minimo_pct"] for v in cotas.values()]), 1)
            if cotas else None),
        "en_cristiano": lectura_por_valor(resumen),
        "mediana_de_p90s": mediana_de_p90s,
        "maximo_de_p90s": round(max(p90s), 3),
        "activos": resumen,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nmediana de los p90: {mediana_de_p90s:.3f} bps")
    print(f"escrito en {a.salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
