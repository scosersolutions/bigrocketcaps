"""Mide y juzga B1: ampliacion de capital -> presion bajista, 5 sesiones.

Pre-registro: `hipotesis/B1-ampliacion-capital.toml`, sellado en el commit
`1eea521` el 2026-09-16 y nunca tocado despues. Ni una cifra de ese fichero
sale de haber mirado un resultado; este script no lo edita, solo lo cumple.

## Por que no pasa por `core.backtest.validacion.validar()`

Ese juez entra siempre en la APERTURA de la vela siguiente a la señal; B1
exige entrar al CIERRE de la sesion de reaccion (ver `sesion_de_entrada`).
Forzarlo desplazaria la entrada una sesion -justo lo que el criterio de
refutacion 3 de B1 comprueba como posible artefacto de horquilla-, y ninguna
hipotesis de acciones lo ha usado nunca (tampoco E7c). En su lugar se
reutilizan sus mismos umbrales (`PERCENTIL_EXIGIDO`, `COSTE_SOBRE_OBJETIVO_MAX`,
`MUESTRA_MINIMA`) sobre la convencion de `brc.estudio.eventos.medir()`.

## Supuestos declarados (B1 no los fija; se documentan para poder auditarlos)

1. **La particion `ventana=validacion` se aplica a que EMPRESAS cuentan como
   evento real**, no al panel de precios que sirve de referencia/azar.
   `medir()` usa un solo `precios` para las dos cosas -la mediana del
   universo y el propio evento-, y separarlas exigiria etiquetar de sector
   miles de tickers de referencia sin necesidad: el reparto exploracion/
   validacion protege que discovery no pueda mirar QUE EMPRESAS se sellan,
   no contra que sirvan de fondo estadistico neutro.
2. **"El universo" de la referencia es todo `ohlcv` con `mercado='stock_us'`
   y precio ese dia**, no el cruce adicional contra `universo_decil`. B1 no
   pide ese cruce y anadirlo solo mueve la mediana por construccion, no por
   metodo.
3. **`minimo_velas_previas=60`** se aplica por evento: se descarta un evento
   si su ticker tiene menos de 60 velas de `ohlcv` con fecha anterior a su
   `presentado`.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl

from brc.datos import lago, particion
from brc.datos.particion import Puerta, Ventana
from brc.datos.sector import SECTORES
from brc.estudio import azar
from brc.estudio.eventos import EstudioError, medir, sesion_de_entrada

CLASE = "emision_precio"
VALIDACION_DESDE = "2018-01-01"
VALIDACION_HASTA = "2024-12-31"
HORIZONTE_SESIONES = 5
DIRECCION = "corto"
MINIMO_VELAS_PREVIAS = 60
TRAMOS_B1 = ((2018, 2021), (2022, 2024))

#: Mismos umbrales que `core.backtest.validacion`, para que "superar el azar"
#: signifique lo mismo en las dos ramas del proyecto.
PERCENTIL_EXIGIDO = 95.0
MUESTRA_MINIMA = 100
COSTE_SOBRE_OBJETIVO_MAX = 0.15


def cargar_precios(con_lago: duckdb.DuckDBPyConnection, anyos: list[int]) -> pl.DataFrame:
    expr = lago.leer(con_lago, "ohlcv", anyos)
    return con_lago.execute(
        f"SELECT activo, ts::DATE AS fecha, close FROM {expr} "
        f"WHERE mercado = 'stock_us' AND timeframe = '1d'"
    ).pl()


def cargar_eventos_validacion(
    con_local: duckdb.DuckDBPyConnection, precios: pl.DataFrame,
) -> tuple[pl.DataFrame, dict]:
    """Eventos de B1 restringidos a la mitad `validacion` de sectores.

    Devuelve (eventos, diagnostico) para poder informar cuantos se caen en
    cada puerta -sector, sin precio, sin 60 velas previas- y no solo el total.
    """
    crudos = con_local.execute(
        "SELECT e.cik, e.ticker, e.presentado, e.tras_cierre, s.sector "
        "FROM eventos_societarios e JOIN sector_empresa s ON s.cik = e.cik "
        "WHERE e.clase = ? AND e.presentado BETWEEN ? AND ? "
        "AND e.ticker IS NOT NULL AND e.ticker != ''",
        [CLASE, VALIDACION_DESDE, VALIDACION_HASTA],
    ).pl()

    reparto = particion.sortear(list(SECTORES))
    puerta = Puerta(Ventana.VALIDACION, reparto)
    en_validacion = puerta.recortar(crudos, columna_fecha="presentado")

    minimas = (
        precios.sort(["activo", "fecha"])
        .with_columns(pl.col("fecha").cum_count().over("activo").alias("_n_previas"))
    )
    con_historia = en_validacion.join(
        minimas.select("activo", "fecha", "_n_previas"),
        left_on=["ticker", "presentado"], right_on=["activo", "fecha"], how="left",
    ).filter(pl.col("_n_previas").is_not_null() & (pl.col("_n_previas") > MINIMO_VELAS_PREVIAS))

    diagnostico = {
        "eventos_crudos": crudos.height,
        "empresas_crudas": crudos["cik"].n_unique(),
        "tras_particion_sectores": en_validacion.height,
        "empresas_tras_particion": en_validacion["cik"].n_unique() if en_validacion.height else 0,
        "reparto_validacion": sorted(reparto.validacion),
        "reparto_exploracion": sorted(reparto.exploracion),
        "tras_60_velas_previas": con_historia.height,
        "empresas_finales": con_historia["cik"].n_unique() if con_historia.height else 0,
    }
    return con_historia.select("ticker", "presentado", "tras_cierre"), diagnostico


def evaluar_criterios_b1(
    eventos: pl.DataFrame, precios: pl.DataFrame, *, simulaciones: int, semilla: int,
) -> dict:
    r = medir(eventos, precios, horizonte=HORIZONTE_SESIONES, tramos=TRAMOS_B1)

    # Criterio 3: "dejar pasar una sesion" es entrar en la sesion
    # ESTRICTAMENTE posterior a la entrada real, sea cual sea el
    # `tras_cierre` original. Forzar `tras_cierre=True` sobre el
    # `presentado` original NO vale: para un evento que ya entraba al dia
    # siguiente (tras_cierre=True) no cambiaria nada. Se resuelve la entrada
    # real primero y se reconstruye un evento sintetico que parte de ELLA:
    # `tras_cierre=True` sobre la entrada real siempre salta a la siguiente
    # sesion de verdad, uniformemente.
    sesiones = precios["fecha"].unique().to_list()
    con_entrada_real = sesion_de_entrada(eventos, sesiones).drop_nulls("entrada")
    retrasado = con_entrada_real.select(
        pl.col("ticker"), pl.col("entrada").alias("presentado"),
        pl.lit(True).alias("tras_cierre"),
    )
    try:
        r_retrasado = medir(retrasado, precios, horizonte=HORIZONTE_SESIONES)
        efecto_sobrevive_al_retraso = r_retrasado.exceso_medio_pct < 0
    except EstudioError:
        r_retrasado = None
        efecto_sobrevive_al_retraso = False

    percentil = azar.percentil_contra_azar(
        r.n, precios, horizonte=HORIZONTE_SESIONES, exceso_real=r.exceso_medio_pct,
        direccion=DIRECCION, simulaciones=simulaciones, semilla=semilla,
    )

    tramo_valores = list(r.por_tramo.values())
    cambia_de_signo = (len(tramo_valores) == 2
                       and (tramo_valores[0] < 0) != (tramo_valores[1] < 0))

    criterios = {
        "1_exceso_negativo": {
            "supera": r.exceso_medio_pct < 0,
            "valor": f"{r.exceso_medio_pct:+.4f}%",
            "refuta_si": "exceso medio >= 0",
        },
        "2_supera_el_azar": {
            "supera": percentil >= PERCENTIL_EXIGIDO,
            "valor": f"percentil {percentil:.1f}",
            "refuta_si": f"percentil < {PERCENTIL_EXIGIDO:.0f}",
        },
        "3_sobrevive_al_retraso_de_una_sesion": {
            "supera": efecto_sobrevive_al_retraso,
            "valor": (f"{r_retrasado.exceso_medio_pct:+.4f}%" if r_retrasado else "sin datos"),
            "refuta_si": "el efecto desaparece (deja de ser negativo)",
        },
        "4_estable_entre_tramos": {
            "supera": not cambia_de_signo,
            "valor": str(r.por_tramo),
            "refuta_si": "cambia de signo entre 2018-2021 y 2022-2024",
        },
        "5_al_menos_20_empresas": {
            "supera": r.n_empresas >= 20,
            "valor": f"{r.n_empresas} empresas",
            "refuta_si": "< 20 empresas distintas",
        },
        "6_mediana_no_es_cola": {
            "supera": not (r.exceso_mediano_pct > 0 and r.exceso_medio_pct < 0),
            "valor": f"mediana {r.exceso_mediano_pct:+.4f}% / media {r.exceso_medio_pct:+.4f}%",
            "refuta_si": "mediana positiva con media negativa",
        },
    }
    refutada = any(not c["supera"] for c in criterios.values())
    return {
        "resultado": r, "criterios": criterios, "refutada": refutada,
        "percentil_azar": percentil,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default="data/bigrocketcaps.duckdb")
    p.add_argument("--anyo-desde", type=int, default=2017)
    p.add_argument("--anyo-hasta", type=int, default=2025)
    p.add_argument("--simulaciones", type=int, default=200)
    p.add_argument("--semilla", type=int, default=0)
    p.add_argument("--salida", type=Path,
                   default=Path("data/experimentos/veredicto_b1.json"))
    a = p.parse_args()

    con_local = duckdb.connect(a.base, read_only=True)
    con_lago = lago.conectar()

    print("cargando precios stock_us 1d...")
    precios = cargar_precios(con_lago, list(range(a.anyo_desde, a.anyo_hasta + 1)))
    print(f"  {precios.height:,} velas, {precios['activo'].n_unique():,} tickers")

    eventos, diagnostico = cargar_eventos_validacion(con_local, precios)
    print("diagnostico del universo:", json.dumps(diagnostico, indent=2, ensure_ascii=False))

    if eventos.height < MUESTRA_MINIMA:
        print(f"AVISO: {eventos.height} eventos, por debajo de "
             f"MUESTRA_MINIMA={MUESTRA_MINIMA}. Se mide igual para dejar "
             f"constancia; el criterio de muestra queda como INCONCLUSA.")

    resultado = evaluar_criterios_b1(
        eventos, precios, simulaciones=a.simulaciones, semilla=a.semilla)

    print()
    print(resultado["resultado"])
    print()
    for nombre, c in resultado["criterios"].items():
        marca = "PASA" if c["supera"] else "REFUTA"
        print(f"  [{marca}] {nombre}: {c['valor']} (refuta si: {c['refuta_si']})")
    print()
    print("VEREDICTO:", "REFUTADA" if resultado["refutada"] else "SOBREVIVE (sin coste)")
    print("(el criterio de coste se añade aparte, con el spread medido de la horquilla US)")

    salida = {
        "hipotesis": "B1", "medido": datetime.now(UTC).isoformat(),
        "n": resultado["resultado"].n, "n_empresas": resultado["resultado"].n_empresas,
        "exceso_medio_pct": resultado["resultado"].exceso_medio_pct,
        "exceso_mediano_pct": resultado["resultado"].exceso_mediano_pct,
        "t": resultado["resultado"].t, "por_tramo": resultado["resultado"].por_tramo,
        "percentil_azar": resultado["percentil_azar"],
        "criterios": {k: {"supera": v["supera"], "valor": v["valor"]}
                     for k, v in resultado["criterios"].items()},
        "refutada_sin_coste": resultado["refutada"],
        "diagnostico_universo": diagnostico,
        "coste": "PENDIENTE: falta calibrar con data/experimentos/spread_us.json",
    }
    a.salida.parent.mkdir(parents=True, exist_ok=True)
    a.salida.write_text(json.dumps(salida, indent=2, ensure_ascii=False, default=str),
                        encoding="utf-8")
    print(f"\nescrito en {a.salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
