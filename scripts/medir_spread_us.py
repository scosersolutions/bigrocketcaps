"""Mide la horquilla REAL de acciones estadounidenses, muestreando el presente.

## Por qué muestrear y no estimar

No hay bid/ask histórico gratuito para acciones US. Las dos salidas son
estimarlo desde datos diarios o medir el presente, y la primera ya se probó:
`core.quant.horquilla` implementa Corwin-Schultz y está marcado como fallido
—sobreestima hasta ×29 en valores conocidos e invierte el orden de liquidez en
small caps— con la nota «No usar para calcular costes».

Así que se mide, igual que se hizo con Kraken: instantáneas del presente,
muchas, y se usa el percentil 90. Un piloto de 40 instantáneas allí solo
detectó dos de los cuatro activos caros: hacen falta cientos, no decenas.

## Lo que esto NO es

El spread de HOY no es el spread de 2018. Una medición del presente aplicada a
un histórico supone que la liquidez no ha cambiado, y ha cambiado. Se usa
igualmente porque la alternativa es inventárselo, pero el pre-registro que lo
use tiene que decir que su modelo de costes viene de aquí.

## La fuente, y su sesgo conocido

Tiingo publica el libro de IEX, que es unos pocos puntos porcentuales del
volumen total. Su horquilla tiende a ser MÁS ANCHA que la consolidada de todo
el mercado.

Eso hace la medición conservadora: se cobra de más, no de menos. Para un
sistema cuyo sesgo es hacia el no, equivocarse por ahí es el lado correcto.
Queda escrito en la salida para que nadie lo lea como el spread real del NBBO.

Uso, con el mercado ABIERTO (13:30-20:00 UTC):
    python scripts/medir_spread_us.py --instantaneas 60 --pausa 30
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

SALIDA = Path("./data/experimentos/spread_us.json")
IEX = "https://api.tiingo.com/iex/"

#: Valores de la muestra, elegidos para cubrir el rango de liquidez del
#: universo y NO por su comportamiento. Se fijan aquí antes de medir: elegir
#: la muestra viendo los spreads sería medir lo que conviene.
MUESTRA = [
    # grandes
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "XOM",
    # medianas
    "F", "PFE", "KHC", "HAL", "GAP", "NCLH", "RIG", "AAL",
    # pequeñas y de precio bajo, donde la horquilla muerde
    "PLUG", "SIRI", "AMC", "BBAI", "SOUN", "GEVO", "FCEL", "CLOV",
]


def instantanea(tickers: list[str], token: str,
                cliente: httpx.Client) -> list[dict]:
    r = cliente.get(IEX, params={"tickers": ",".join(tickers), "token": token},
                    timeout=30)
    r.raise_for_status()
    return r.json()


def bps(bid: float, ask: float) -> float | None:
    """Horquilla en puntos básicos sobre el punto medio."""
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    medio = (bid + ask) / 2
    return (ask - bid) / medio * 10_000


def agregados(resumen: dict[str, dict]) -> dict[str, float]:
    """Los dos numeros que resumen la horquilla de todo el universo.

    Se escriben en el JSON en vez de dejarlos para quien lo lea por dos
    motivos. Uno: son la entrada del modelo de costes -ver
    `scripts/calibrar_costes_us.py`, que explica por que la MEDIANA de los p90
    por activo y no el p90 de todo junto-. Y dos: al ser campos de primer
    nivel se convierten en metricas con serie temporal en el Centro de
    Control, que es justo la EVOLUCION por la que esto se mide a diario en vez
    de una sola vez. Enterrados dentro de `activos` no serian ninguna de las
    dos cosas.
    """
    p90s = [v["p90_bps"] for v in resumen.values()]
    return {"mediana_de_p90s": round(statistics.median(p90s), 3),
            "maximo_de_p90s": round(max(p90s), 3)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instantaneas", type=int, default=60)
    p.add_argument("--pausa", type=float, default=30.0, help="segundos")
    p.add_argument("--salida", type=Path, default=SALIDA)
    a = p.parse_args()

    load_dotenv()
    token = os.environ.get("TIINGO_TOKEN", "")
    if not token:
        print("falta TIINGO_TOKEN en el .env")
        return 2

    medidas: dict[str, list[float]] = {t: [] for t in MUESTRA}
    vacias = 0
    print(f"{len(MUESTRA)} valores · {a.instantaneas} instantáneas · "
          f"cada {a.pausa:.0f}s\n")
    with httpx.Client(follow_redirects=True) as cliente:
        for i in range(a.instantaneas):
            try:
                datos = instantanea(MUESTRA, token, cliente)
            except httpx.HTTPError as e:
                print(f"  {i+1:>3}  fallo de red: {type(e).__name__}")
                time.sleep(a.pausa)
                continue
            utiles = 0
            for d in datos:
                v = bps(d.get("bidPrice"), d.get("askPrice"))
                if v is not None:
                    medidas[d["ticker"].upper()].append(v)
                    utiles += 1
            if utiles == 0:
                vacias += 1
                if vacias == 1:
                    print("  sin bid/ask: ¿está el mercado abierto? "
                          "(13:30-20:00 UTC)")
            if (i + 1) % 10 == 0:
                print(f"  {i+1:>3}/{a.instantaneas} · {utiles} valores con libro",
                      flush=True)
            if i + 1 < a.instantaneas:
                time.sleep(a.pausa)

    con_datos = {t: v for t, v in medidas.items() if len(v) >= 5}
    if not con_datos:
        print("\nNinguna medición útil. Con el mercado cerrado no hay libro que "
              "medir: esto se ejecuta entre las 13:30 y las 20:00 UTC.")
        return 1

    print(f"\n{'activo':<8} {'n':>4} {'mediana':>9} {'p90':>8} {'max':>8}")
    print("-" * 42)
    resumen = {}
    for t in MUESTRA:
        v = sorted(con_datos.get(t, []))
        if not v:
            print(f"{t:<8} sin libro")
            continue
        p90 = v[int(len(v) * 0.9)] if len(v) > 1 else v[0]
        resumen[t] = {"n": len(v), "mediana_bps": round(statistics.median(v), 2),
                      "p90_bps": round(p90, 2), "max_bps": round(v[-1], 2)}
        print(f"{t:<8} {len(v):>4} {resumen[t]['mediana_bps']:>8.2f} "
              f"{resumen[t]['p90_bps']:>8.2f} {resumen[t]['max_bps']:>8.2f}")

    a.salida.parent.mkdir(parents=True, exist_ok=True)
    a.salida.write_text(json.dumps({
        "medido": datetime.now(UTC).isoformat(),
        "fuente": "tiingo/iex",
        "aviso": ("libro de IEX, unos pocos puntos porcentuales del volumen "
                  "total: su horquilla tiende a ser MAS ANCHA que la "
                  "consolidada. La medicion es conservadora, no el NBBO real."),
        "limitacion": ("es el spread de HOY; aplicarlo a un historico supone "
                       "que la liquidez no ha cambiado, y ha cambiado"),
        "instantaneas_pedidas": a.instantaneas,
        **agregados(resumen),
        "activos": resumen,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nescrito en {a.salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
