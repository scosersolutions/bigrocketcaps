"""Calibra `CostesPorAccion` con la horquilla medida, igual que Kraken.

## Por que la mediana de los p90, y no el p90 de todo junto

`perfiles.costes_kraken()` usa el p90 propio de CADA activo porque valida
exactamente esos 7-24 activos. Aqui no: B1 opera sobre una banda ancha de
liquidez (los 24 valores de `medir_spread_us.py` van de AAPL a CLOV a
proposito, para cubrir el rango), y el JSON solo guarda resumenes por activo
-mediana/p90/max-, no las instantaneas crudas, asi que no hay un "p90 de
todo junto" que calcular sin re-muestrear.

Se usa la MEDIANA de los 24 p90 individuales: participa el mismo criterio
"peor caso habitual" que usa Kraken (p90, no la mediana de cada activo), pero
agregado de forma que un puñado de small caps de precio muy bajo -SIRI, AMC,
BBAI, SOUN, GEVO, FCEL, CLOV, cuya horquilla es la que mas muerde- no
domine el numero para un universo que en su mayoria es media-alta
capitalizacion (D4: 5-50 M$/dia). El MAXIMO de los 24 queda escrito tambien,
para ver cuanto cambiaria el veredicto con el escenario mas conservador.

Uso:
    python scripts/calibrar_costes_us.py
"""
from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime
from pathlib import Path

from core.quant.costes import CostesPorAccion

ENTRADA = Path("data/experimentos/spread_us.json")


def calibrar(datos: dict) -> dict:
    activos = datos.get("activos", {})
    if not activos:
        raise ValueError(f"{ENTRADA} no tiene mediciones en 'activos'")

    p90s = [v["p90_bps"] for v in activos.values()]
    # Los escribe `medir_spread_us.agregados()` desde el 2026-09-16. Se
    # recalculan si no estan para poder leer una medicion anterior a ese
    # campo, no porque haya dos criterios: el criterio es este, y esta
    # explicado arriba.
    mediana_de_p90s = datos.get("mediana_de_p90s",
                               round(statistics.median(p90s), 3))
    maximo_de_p90s = datos.get("maximo_de_p90s", round(max(p90s), 3))
    origen = (f"tiingo/iex p90 por activo, mediana de {len(p90s)} activos, "
             f"medido {datos.get('medido', '?')}")

    return {
        "modelo_principal": CostesPorAccion(
            spread_bps=mediana_de_p90s, spread_medido=True, origen_spread=origen),
        "modelo_conservador": CostesPorAccion(
            spread_bps=maximo_de_p90s, spread_medido=True,
            origen_spread=origen.replace("mediana", "maximo")),
        "mediana_de_p90s": mediana_de_p90s,
        "maximo_de_p90s": maximo_de_p90s,
        "n_activos": len(p90s),
        "detalle": {t: v["p90_bps"] for t, v in sorted(
            activos.items(), key=lambda kv: kv[1]["p90_bps"])},
    }


def main() -> int:
    if not ENTRADA.exists():
        print(f"{ENTRADA} no existe todavia: la horquilla no ha corrido. "
             f"El workflow 'Horquilla US' corre lunes-viernes a las 14:23 UTC.")
        return 1

    datos = json.loads(ENTRADA.read_text(encoding="utf-8"))
    r = calibrar(datos)

    print(f"medido: {datos.get('medido')}  ·  fuente: {datos.get('fuente')}")
    print(f"{r['n_activos']} activos, spread p90 por activo (bps):")
    for t, bps in r["detalle"].items():
        print(f"  {t:<8} {bps:>7.2f}")
    print()
    print(f"mediana de los p90:  {r['mediana_de_p90s']:.3f} bps  <- modelo principal")
    print(f"maximo de los p90:   {r['maximo_de_p90s']:.3f} bps  <- modelo conservador")
    print()
    print("modelo_principal:", r["modelo_principal"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
