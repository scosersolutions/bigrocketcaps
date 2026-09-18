"""Juzga una hipotesis pre-registrada contra sus criterios de refutacion.

    python scripts/juzgar.py --hipotesis B2

## Por que lee el TOML en vez de repetirlo

`juzgar_b1.py` llevaba la clase de evento, el horizonte y la direccion escritos
como constantes, al lado de un pre-registro que decia lo mismo. Dos copias del
mismo criterio es una invitacion a que se separen, y la unica forma de que se
separen sin que nadie lo note es la peor: que el juez mida con un horizonte que
la hipotesis no declaro.

Aqui los parametros salen del fichero sellado. El juez no PUEDE medir otra cosa
que lo pre-registrado, y eso no depende de que quien lo ejecute se acuerde.

## Por que no pasa por `core.backtest.validacion.validar()`

Ese juez entra siempre en la APERTURA de la vela siguiente a la señal; estas
hipotesis exigen entrar al CIERRE de la sesion de reaccion (ver
`sesion_de_entrada`). Forzarlo desplazaria la entrada una sesion -justo lo que
el criterio de refutacion 3 comprueba como posible artefacto de horquilla-. En
su lugar se reutilizan sus mismos umbrales (`PERCENTIL_EXIGIDO`,
`MUESTRA_MINIMA`) sobre la convencion de `brc.estudio.eventos.medir()`.

## Supuestos declarados (los TOML no los fijan; se documentan para auditarlos)

1. **La particion `ventana=validacion` se aplica a que EMPRESAS cuentan como
   evento real**, no al panel de precios que sirve de referencia/azar.
   `medir()` usa un solo `precios` para las dos cosas, y separarlas exigiria
   etiquetar de sector miles de tickers de referencia sin necesidad: el reparto
   protege que discovery no pueda mirar QUE EMPRESAS se sellan, no contra que
   sirvan de fondo estadistico neutro.
2. **"El universo" de la referencia es todo `ohlcv` con `mercado='stock_us'` y
   precio ese dia**, no el cruce adicional contra `universo_decil`.
3. **Los tramos se parten por la MITAD del periodo de validacion declarado**,
   no se eligen. Para 2018-2024 eso da 2018-2021 y 2022-2024, que es el reparto
   con el que se midio B1. Elegir el corte despues de ver los numeros seria
   buscar la particion que da la razon.
"""
from __future__ import annotations

import argparse
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl

from brc.datos import lago, particion
from brc.datos.particion import Puerta, Ventana
from brc.datos.sector import SECTORES
from brc.estudio import azar
from brc.estudio.eventos import EstudioError, medir, sesion_de_entrada
from brc.estudio.horquilla import horquilla_de_equilibrio

#: Mismos umbrales que `core.backtest.validacion`, para que "superar el azar"
#: signifique lo mismo en las dos ramas del proyecto.
PERCENTIL_EXIGIDO = 95.0
MUESTRA_MINIMA = 100
MINIMO_EMPRESAS = 20


class Hipotesis:
    """Los parametros sellados, leidos del TOML y no escritos aqui."""

    def __init__(self, ruta: Path) -> None:
        d = tomllib.loads(ruta.read_text(encoding="utf-8"))
        self.ruta = ruta
        self.id = d["id"]
        self.titulo = d["titulo"]
        self.estado = d["estado"]
        self.direccion = d["mecanismo"]["direccion"]
        self.clase = d["universo"]["fuente_eventos"].split("= ")[1].strip()
        self.minimo_velas = d["universo"]["minimo_velas_previas"]
        self.horizonte = d["senal"]["horizonte_sesiones"]
        self.criterios_declarados = d["criterios_refutacion"]
        desde, hasta = (p.strip() for p in d["periodos"]["validacion"].split(".."))
        self.desde, self.hasta = desde, hasta
        self.anyo_desde, self.anyo_hasta = int(desde[:4]), int(hasta[:4])

    @property
    def tramos(self) -> tuple[tuple[int, int], ...]:
        """El periodo de validacion partido por la mitad. No se elige."""
        medio = (self.anyo_desde + self.anyo_hasta) // 2
        return ((self.anyo_desde, medio), (medio + 1, self.anyo_hasta))

    def a_favor(self, exceso: float) -> bool:
        """Si ese exceso va en el sentido que la hipotesis predijo."""
        return exceso < 0 if self.direccion == "corto" else exceso > 0


def localizar(identificador: str) -> Path:
    candidatos = sorted(Path("hipotesis").glob(f"{identificador}-*.toml"))
    if not candidatos:
        raise SystemExit(f"no hay pre-registro para {identificador} en hipotesis/")
    if len(candidatos) > 1:
        raise SystemExit(f"{identificador} tiene varios pre-registros: {candidatos}")
    return candidatos[0]


def origen_datos(
    base: Path, con_lago: duckdb.DuckDBPyConnection,
) -> tuple[duckdb.DuckDBPyConnection, str, str, str]:
    """De donde salen `eventos_societarios` y `sector_empresa`.

    La base local si esta, y el lago si no. Esa es la unica diferencia entre
    juzgar aqui y juzgar en un runner de GitHub: el veredicto no puede depender
    de que una maquina concreta este encendida.
    """
    if base.exists():
        return (duckdb.connect(str(base), read_only=True),
                "eventos_societarios", "sector_empresa", f"base local {base}")
    return (con_lago,
            lago.leer(con_lago, "eventos_societarios"),
            lago.leer(con_lago, "sector_empresa"),
            "lago remoto")


def cargar_precios(con_lago: duckdb.DuckDBPyConnection, anyos: list[int]) -> pl.DataFrame:
    expr = lago.leer(con_lago, "ohlcv", anyos)
    return con_lago.execute(
        f"SELECT activo, ts::DATE AS fecha, close FROM {expr} "
        f"WHERE mercado = 'stock_us' AND timeframe = '1d'"
    ).pl()


def cargar_eventos(
    con: duckdb.DuckDBPyConnection, precios: pl.DataFrame, h: Hipotesis,
    *, tabla_eventos: str, tabla_sectores: str,
) -> tuple[pl.DataFrame, dict]:
    """Eventos de la hipotesis, restringidos a la mitad `validacion`.

    Devuelve (eventos, diagnostico) para poder informar cuantos se caen en cada
    puerta -sector, sin precio, sin historia suficiente- y no solo el total.
    """
    crudos = con.execute(
        f"SELECT e.cik, e.ticker, e.presentado, e.tras_cierre, s.sector "
        f"FROM {tabla_eventos} e JOIN {tabla_sectores} s ON s.cik = e.cik "
        f"WHERE e.clase = ? AND e.presentado BETWEEN ? AND ? "
        f"AND e.ticker IS NOT NULL AND e.ticker != ''",
        [h.clase, h.desde, h.hasta],
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
    ).filter(pl.col("_n_previas").is_not_null()
             & (pl.col("_n_previas") > h.minimo_velas))

    diagnostico = {
        "eventos_crudos": crudos.height,
        "empresas_crudas": crudos["cik"].n_unique(),
        "tras_particion_sectores": en_validacion.height,
        "empresas_tras_particion": (en_validacion["cik"].n_unique()
                                    if en_validacion.height else 0),
        "reparto_validacion": sorted(reparto.validacion),
        "reparto_exploracion": sorted(reparto.exploracion),
        f"tras_{h.minimo_velas}_velas_previas": con_historia.height,
        "empresas_finales": (con_historia["cik"].n_unique()
                             if con_historia.height else 0),
    }
    return con_historia.select("ticker", "presentado", "tras_cierre"), diagnostico


def evaluar(eventos: pl.DataFrame, precios: pl.DataFrame, h: Hipotesis,
            *, simulaciones: int, semilla: int) -> dict:
    r = medir(eventos, precios, horizonte=h.horizonte, tramos=h.tramos)

    # Criterio 3: "dejar pasar una sesion" es entrar en la sesion ESTRICTAMENTE
    # posterior a la entrada real, sea cual sea el `tras_cierre` original.
    # Forzar `tras_cierre=True` sobre el `presentado` original NO vale: para un
    # evento que ya entraba al dia siguiente no cambiaria nada. Se resuelve la
    # entrada real primero y se reconstruye un evento sintetico que parte de
    # ELLA, que siempre salta a la siguiente sesion de verdad.
    sesiones = precios["fecha"].unique().to_list()
    con_entrada_real = sesion_de_entrada(eventos, sesiones).drop_nulls("entrada")
    retrasado = con_entrada_real.select(
        pl.col("ticker"), pl.col("entrada").alias("presentado"),
        pl.lit(True).alias("tras_cierre"),
    )
    try:
        r_retrasado = medir(retrasado, precios, horizonte=h.horizonte)
        sobrevive_al_retraso = h.a_favor(r_retrasado.exceso_medio_pct)
    except EstudioError:
        r_retrasado = None
        sobrevive_al_retraso = False

    percentil = azar.percentil_contra_azar(
        r.n, precios, horizonte=h.horizonte, exceso_real=r.exceso_medio_pct,
        direccion=h.direccion, simulaciones=simulaciones, semilla=semilla,
    )

    valores = list(r.por_tramo.values())
    cambia_de_signo = (len(valores) == 2 and (valores[0] < 0) != (valores[1] < 0))
    # La "cola" es que la mediana contradiga a la media: unos pocos casos
    # enormes sosteniendo un efecto que la mayoria no tiene.
    es_cola = h.a_favor(r.exceso_medio_pct) and not h.a_favor(r.exceso_mediano_pct)
    esperado = "negativo" if h.direccion == "corto" else "positivo"
    etiquetas = " y ".join(f"{d}-{ha}" for d, ha in h.tramos)

    criterios = {
        f"1_exceso_{esperado}": {
            "supera": h.a_favor(r.exceso_medio_pct),
            "valor": f"{r.exceso_medio_pct:+.4f}%",
            "refuta_si": f"el exceso medio no es {esperado}",
        },
        "2_supera_el_azar": {
            "supera": percentil >= PERCENTIL_EXIGIDO,
            "valor": f"percentil {percentil:.1f}",
            "refuta_si": f"percentil < {PERCENTIL_EXIGIDO:.0f}",
        },
        "3_sobrevive_al_retraso_de_una_sesion": {
            "supera": sobrevive_al_retraso,
            "valor": (f"{r_retrasado.exceso_medio_pct:+.4f}%"
                      if r_retrasado else "sin datos"),
            "refuta_si": f"el efecto deja de ser {esperado}",
        },
        "4_estable_entre_tramos": {
            "supera": not cambia_de_signo,
            "valor": str(r.por_tramo),
            "refuta_si": f"cambia de signo entre {etiquetas}",
        },
        f"5_al_menos_{MINIMO_EMPRESAS}_empresas": {
            "supera": r.n_empresas >= MINIMO_EMPRESAS,
            "valor": f"{r.n_empresas} empresas",
            "refuta_si": f"< {MINIMO_EMPRESAS} empresas distintas",
        },
        "6_mediana_no_es_cola": {
            "supera": not es_cola,
            "valor": (f"mediana {r.exceso_mediano_pct:+.4f}% / "
                      f"media {r.exceso_medio_pct:+.4f}%"),
            "refuta_si": "la mediana contradice a la media",
        },
    }
    return {
        "resultado": r, "criterios": criterios, "percentil_azar": percentil,
        "refutada": any(not c["supera"] for c in criterios.values()),
    }


def tabla_de_tramos(por_tramo: dict[str, float], direccion: str) -> list[dict]:
    """`{"2018-2021": 0.464}` -> una fila legible por tramo."""
    a_favor = "negativo" if direccion == "corto" else "positivo"
    filas = []
    for periodo, exceso in por_tramo.items():
        acierta = exceso < 0 if direccion == "corto" else exceso > 0
        filas.append({
            "periodo": periodo,
            "exceso_medio": f"{exceso:+.3f} %",
            "lectura": (f"a favor (la hipotesis predice {a_favor})" if acierta
                        else f"en contra (la hipotesis predice {a_favor})"),
        })
    return filas


def tabla_de_criterios(criterios: dict[str, dict]) -> list[dict]:
    """Un criterio por fila, con el veredicto delante y sin barras bajas."""
    return [{
        "criterio": nombre.split("_", 1)[1].replace("_", " ").capitalize(),
        "resultado": "PASA" if c["supera"] else "REFUTA",
        "valor": c["valor"],
        "se_refuta_si": c["refuta_si"],
    } for nombre, c in criterios.items()]


def en_cristiano(h: Hipotesis, r, refutada: bool, percentil: float) -> list[dict]:
    """Las mismas cifras, contestando lo que se pregunta cualquiera."""
    porques = []
    if not h.a_favor(r.exceso_medio_pct):
        porques.append("el efecto va en el sentido CONTRARIO al que predecia")
    if percentil < PERCENTIL_EXIGIDO:
        porques.append(f"el azar lo iguala o lo mejora en el "
                      f"{100 - percentil:.0f} % de los sorteos")
    valores = list(r.por_tramo.values())
    if len(valores) == 2 and (valores[0] < 0) != (valores[1] < 0):
        porques.append("cambia de signo entre epocas")
    if r.n_empresas < MINIMO_EMPRESAS:
        porques.append(f"solo lo sostienen {r.n_empresas} empresas")
    return [
        {"pregunta": "¿Que se ha probado?", "respuesta": h.titulo},
        {"pregunta": "¿Que ha salido?",
         "respuesta": f"{r.exceso_medio_pct:+.3f} % de media sobre {r.n:,} eventos "
                      f"en {r.n_empresas} empresas"
                      + ("." if h.a_favor(r.exceso_medio_pct)
                         else ", o sea en contra de lo predicho.")},
        {"pregunta": "¿Y esto en que acciones me dice que invierta?",
         "respuesta": "En ninguna. Esto mide si una IDEA tiene ventaja, no si una "
                      "accion esta barata. Una hipotesis que sobrevive tampoco es "
                      "una orden de compra: es una idea que todavia no se ha "
                      "podido matar."},
        {"pregunta": "¿Por que no vale?" if refutada else "¿Y ahora que?",
         "respuesta": ("; ".join(porques).capitalize() + "." if porques else
                       "Pasa los seis criterios. Sobrevive, que no es lo mismo que "
                       "funcionar: queda el holdout, que no se ha tocado.")},
    ]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hipotesis", required=True, help="B2, B3...")
    p.add_argument("--base", default="data/bigrocketcaps.duckdb")
    p.add_argument("--simulaciones", type=int, default=200)
    p.add_argument("--semilla", type=int, default=0)
    p.add_argument("--salida", type=Path, default=None)
    a = p.parse_args()

    h = Hipotesis(localizar(a.hipotesis.upper()))
    salida = a.salida or Path(f"data/experimentos/veredicto_{h.id.lower()}.json")
    print(f"{h.id}: {h.titulo}")
    print(f"  pre-registro {h.ruta} ({h.estado}) · clase={h.clase} · "
          f"{h.direccion} · {h.horizonte} sesiones · tramos {h.tramos}")

    con_lago = lago.conectar()
    con_datos, t_ev, t_sec, origen = origen_datos(Path(a.base), con_lago)
    print(f"  eventos y sectores: {origen}")

    # Un año de margen por cada lado: el horizonte puede cruzar el fin de año.
    anyos = list(range(h.anyo_desde - 1, h.anyo_hasta + 2))
    precios = cargar_precios(con_lago, anyos)
    print(f"  {precios.height:,} velas, {precios['activo'].n_unique():,} tickers")

    try:
        eventos, diagnostico = cargar_eventos(
            con_datos, precios, h, tabla_eventos=t_ev, tabla_sectores=t_sec)
    except duckdb.Error as err:
        raise SystemExit(
            f"""no se pudieron leer los eventos en {origen}: {err}

Si esto corre fuera del portatil, las dos tablas tienen que estar en el
lago. Se suben una vez con:
  python scripts/exportar_lago.py --solo eventos_societarios,sector_empresa
  python scripts/publicar_lago.py"""
        ) from err

    print("diagnostico:", json.dumps(diagnostico, indent=2, ensure_ascii=False))
    if eventos.height < MUESTRA_MINIMA:
        print(f"AVISO: {eventos.height} eventos, por debajo de "
              f"MUESTRA_MINIMA={MUESTRA_MINIMA}.")
    if eventos.is_empty():
        print("sin eventos que medir: la hipotesis no se puede juzgar")
        return 1

    res = evaluar(eventos, precios, h,
                  simulaciones=a.simulaciones, semilla=a.semilla)
    r = res["resultado"]
    print()
    print(r)
    print()
    for nombre, c in res["criterios"].items():
        print(f"  [{'PASA' if c['supera'] else 'REFUTA'}] {nombre}: {c['valor']}")
    print()
    print("VEREDICTO:", "REFUTADA" if res["refutada"] else "SOBREVIVE")

    equilibrio = horquilla_de_equilibrio(r.exceso_medio_pct, h.direccion)
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps({
        "hipotesis": h.id,
        "titulo": h.titulo,
        "pre_registro": str(h.ruta).replace("\\", "/"),
        "medido": datetime.now(UTC).isoformat(),
        "n": r.n, "n_empresas": r.n_empresas,
        "exceso_medio_pct": r.exceso_medio_pct,
        "exceso_mediano_pct": r.exceso_mediano_pct,
        "t": r.t, "por_tramo": r.por_tramo,
        "percentil_azar": res["percentil_azar"],
        "veredicto": "REFUTADA" if res["refutada"] else "SOBREVIVE",
        "refutada_sin_coste": res["refutada"],
        "criterios": {k: {"supera": v["supera"], "valor": v["valor"]}
                      for k, v in res["criterios"].items()},
        "criterios_tabla": tabla_de_criterios(res["criterios"]),
        "tramos": tabla_de_tramos(r.por_tramo, h.direccion),
        "en_cristiano": en_cristiano(h, r, res["refutada"], res["percentil_azar"]),
        "coste": {
            "pregunta": "¿A que horquilla deja de funcionar?",
            "horquilla_de_equilibrio_bps": equilibrio,
            "respuesta": (
                f"aguanta hasta {equilibrio:.0f} bps de horquilla por operacion"
                if equilibrio > 0 else
                "no funciona ni con horquilla cero: el exceso ya va en contra "
                "antes de pagar un solo coste"),
            "para_comparar": (
                "la horquilla estimada del universo ronda los 112 bps, con ~48 % "
                "de sesgo demostrado: la cota fiable esta sobre los 58 bps"),
        },
        "diagnostico_universo": diagnostico,
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"escrito en {salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
