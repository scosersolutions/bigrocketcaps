"""Juicio automático de una hipótesis de trading.

Este módulo es el producto principal de MoonRocket: convierte una idea de
estrategia en un veredicto defendible sin que quien la propone tenga que
acordarse de todas las formas de engañarse.

Aplica, siempre y en el mismo orden, los controles que las tres primeras
hipótesis del proyecto fueron necesitando:

1. **Separación de muestras.** Los activos donde se exploró la idea no pueden
   juzgarla. E3 daba +0,204 R en BTC (que la sugirió) y −0,269 R en los cinco
   activos que no la habían visto. Sin esa separación habría pasado a paper.

2. **Contraste contra el azar.** Un profit factor de 1,14 parece una ventaja
   hasta que se compara con estrategias aleatorias equivalentes y resulta que
   el 14 % de ellas lo hace mejor. Se exige el percentil 95.

3. **Consistencia entre activos.** Una ventaja real no cambia de signo al
   cambiar de mercado. E2 ganaba en BTC y perdía en ETH, con los lados
   invertidos: eso es ruido con buena presentación.

4. **Descuento del funding.** En perpetuos, ciertas estrategias cobran funding
   por construcción. Si el resultado desaparece al quitarlo, la hipótesis
   direccional es falsa aunque la curva de capital suba.

5. **Coste sobre objetivo.** Una estrategia cuyo objetivo se lo come la
   fricción no es operable, por mucho que el backtest salga positivo.

6. **Muestra suficiente.** Con pocas operaciones, el ruido supera al efecto y
   cualquier conclusión es prematura.

Un veredicto favorable no significa "esto gana dinero". Significa "esto ha
sobrevivido a los controles que descartaron a las anteriores", que es la única
afirmación que un backtest puede sostener.
"""

from __future__ import annotations

import json
import math
import random
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import polars as pl

from core.backtest import ventanas
from core.backtest.motor import ejecutar
from core.papel.registro import Registro
from core.quant.costes import ModeloCostes
from core.quant.estrategias import COLUMNAS_SENAL, Direccion

#: Percentil de la distribución aleatoria que hay que superar.
PERCENTIL_EXIGIDO = 95.0

#: Operaciones mínimas agregadas para que la muestra diga algo.
MUESTRA_MINIMA = 100

#: Simulaciones de Monte Carlo. Más da más resolución en el percentil.
SIMULACIONES = 200

#: Fracción máxima del objetivo que pueden llevarse los costes.
COSTE_SOBRE_OBJETIVO_MAX = 0.15

#: Copia versionada de la contabilidad de contrastes. `validar()` la escribe
#: sola: el contador del Deflated Sharpe no puede depender de que alguien se
#: acuerde de exportarlo. Se pasa `papel=None` para desactivarlo en pruebas.
PAPEL_EXPERIMENTOS = Path("data/papel/experimentos.jsonl")


def p_binomial(positivos: int, total: int) -> float:
    """P(X >= positivos) con una moneda justa. Test de una cola.

    Generaliza el criterio de consistencia. Con pocos activos exige que todos
    sean positivos, igual que la versión anterior: con 5 activos, 5 de 5 da
    p=0,031 y 4 de 5 da p=0,188. Con muchos activos, en cambio, exigir que
    todos lo sean sería imposible incluso para una estrategia excelente, porque
    con pocas operaciones por activo la varianza individual es enorme.

    Sin esta generalización, el criterio de consistencia se volvía imposible de
    superar al ampliar el universo, y habría convertido cualquier ampliación de
    muestra en un rechazo automático.
    """
    if total <= 0:
        return 1.0
    cola = sum(math.comb(total, i) for i in range(positivos, total + 1))
    return cola / (2**total)


class Estrategia(Protocol):
    """Cualquier clase con `nombre`, `version` y `generar()` encaja."""

    nombre: str
    version: str

    def generar(
        self, velas: pl.DataFrame, regimenes: pl.Series, activo: str
    ) -> pl.DataFrame: ...


@dataclass
class Criterio:
    nombre: str
    supera: bool
    valor: str
    umbral: str

    def __str__(self) -> str:
        marca = "PASA" if self.supera else "FALLA"
        return f"  [{marca}] {self.nombre}: {self.valor} (exige {self.umbral})"


@dataclass
class Veredicto:
    estrategia: str
    version: str
    criterios: list[Criterio] = field(default_factory=list)
    resumen_por_activo: dict[str, dict] = field(default_factory=dict)
    agregado: dict = field(default_factory=dict)
    activos_desarrollo: tuple[str, ...] = ()
    activos_validacion: tuple[str, ...] = ()
    #: Rango real de datos juzgado y, si se abrió el holdout, quién lo
    #: autorizó. Va al registro: un juicio sin rango anotado no se puede
    #: auditar después.
    ventana: dict = field(default_factory=dict)

    @property
    def aprobada(self) -> bool:
        return bool(self.criterios) and all(c.supera for c in self.criterios)

    def informe(self) -> str:
        estado = "SOBREVIVE" if self.aprobada else "REFUTADA"
        lineas = [
            f"{self.estrategia} v{self.version} — {estado}",
            "",
            f"Juzgada en: {', '.join(self.activos_validacion)}",
        ]
        if self.activos_desarrollo:
            lineas.append(
                f"Excluidos por haber participado en su formulación: "
                f"{', '.join(self.activos_desarrollo)}"
            )
        lineas += ["", "Criterios:"]
        lineas += [str(c) for c in self.criterios]
        if self.agregado:
            a = self.agregado
            lineas += [
                "",
                f"Agregado: {a['n']} ops · win {a['win']:.1f}% · PF {a['pf']:.2f} · "
                f"expectancy {a['expectancy']:+.3f}R · PnL {a['pnl']:+,.0f} "
                f"(sin funding {a['pnl_sin_funding']:+,.0f})",
            ]
        return "\n".join(lineas)


def _metricas(ops: pl.DataFrame) -> dict:
    if ops.is_empty():
        return {"n": 0, "win": 0.0, "pf": 0.0, "expectancy": 0.0, "pnl": 0.0,
                "pnl_sin_funding": 0.0}
    ganadoras = ops.filter(pl.col("pnl_neto") > 0)
    perdedoras = ops.filter(pl.col("pnl_neto") <= 0)
    bruto_perdido = abs(perdedoras["pnl_neto"].sum()) if perdedoras.height else 0.0
    return {
        "n": ops.height,
        "win": ganadoras.height / ops.height * 100,
        "pf": (ganadoras["pnl_neto"].sum() / bruto_perdido) if bruto_perdido > 0 else float("inf"),
        "expectancy": float(ops["r_multiple"].mean()),
        "pnl": float(ops["pnl_neto"].sum()),
        "pnl_sin_funding": float(ops["pnl_sin_funding"].sum()),
    }


def _percentil_montecarlo(
    velas: pl.DataFrame,
    activo: str,
    n_ops: int,
    stop_pct: float,
    rr: float,
    expectancy_real: float,
    costes: ModeloCostes,
    nocional: float,
    simulaciones: int,
) -> float:
    """Porcentaje de estrategias aleatorias equivalentes que quedan por debajo.

    Equivalentes significa: mismo número de operaciones, mismo stop, mismo R:R,
    mismos costes y los mismos datos. Lo único distinto son las entradas.
    """
    if velas.height < 600 or n_ops < 5:
        return 0.0

    expectativas = []
    for semilla in range(simulaciones):
        rnd = random.Random(semilla)
        maximo = velas.height - 400
        muestra = min(int(n_ops * 1.3), maximo - 100)
        if muestra < 5:
            return 0.0
        indices = sorted(rnd.sample(range(100, maximo), muestra))
        filas = []
        for i in indices:
            cierre = velas["close"][i]
            direccion = rnd.choice([Direccion.LONG.value, Direccion.SHORT.value])
            signo = 1 if direccion == Direccion.LONG.value else -1
            filas.append(
                {
                    "ts": velas["ts"][i],
                    "activo": activo,
                    "estrategia": "montecarlo",
                    "version": "1",
                    "direccion": direccion,
                    "entrada": cierre,
                    "stop": cierre * (1 - stop_pct * signo),
                    "objetivo": cierre * (1 + stop_pct * rr * signo),
                    "rr": rr,
                    "regimen": "n/a",
                }
            )
        resultado = ejecutar(
            velas,
            pl.DataFrame(filas).select(COLUMNAS_SENAL),
            costes=costes,
            nocional=nocional,
        )
        ops = resultado.a_dataframe()
        if not ops.is_empty():
            expectativas.append(float(ops["r_multiple"].mean()))

    if not expectativas:
        return 0.0
    return sum(1 for e in expectativas if e < expectancy_real) / len(expectativas) * 100


#: Mercado con el que se declara que un juicio no toca ningún holdout. Existe
#: para que omitirlo sea imposible y saltárselo tenga que escribirse: el
#: agujero anterior no fue que alguien decidiera mal, fue que no había que
#: decidir nada.
SIN_HOLDOUT = "sin_holdout"


def validar(
    estrategia: Estrategia,
    datos: dict[str, tuple[pl.DataFrame, pl.Series, pl.DataFrame]],
    *,
    mercado: str,
    autorizacion_holdout: str | None = None,
    permitir_costes_supuestos: bool = False,
    costes: ModeloCostes | dict[str, ModeloCostes],
    activos_desarrollo: tuple[str, ...] = (),
    nocional: float = 10_000.0,
    rr: float = 2.5,
    simulaciones: int = SIMULACIONES,
    registrar_en=None,
    hipotesis: str = "",
    papel: Path | None = PAPEL_EXPERIMENTOS,
    gestion=None,
) -> Veredicto:
    """Somete una estrategia a todos los controles y emite el veredicto.

    `datos` es {activo: (velas_con_funding, regimenes, funding)}. Los activos de
    `activos_desarrollo` se ejecutan igual, pero **no cuentan para el juicio**:
    son los que vieron nacer la idea.

    `costes` admite un modelo único o uno POR ACTIVO. Lo segundo hace falta desde
    que el spread se mide en vez de suponerse: BNB paga 17,9 bps de ida y vuelta
    y BTC 10,1, y cobrarles lo mismo falsea el juicio en los dos sentidos a la
    vez. Si falta el activo en el diccionario, falla en vez de caer en un modelo
    por defecto: un coste tomado de otro activo es peor que ninguno, porque
    parece medido.

    `gestion` se pasa tal cual a `ejecutar`. Sin él la tenencia máxima es la del
    motor por defecto (336 velas), que no es la de ningún pre-registro de
    daytrading: A cierra en 96 y B en 120.
    """
    def coste_de(activo: str) -> ModeloCostes:
        if not isinstance(costes, dict):
            modelo = costes
        elif activo not in costes:
            raise ValueError(
                f"{activo} no tiene modelo de costes declarado; no se usa el de otro")
        else:
            modelo = costes[activo]
        # Un spread supuesto puede fabricar un falso positivo: medido el
        # 2026-09-16, con stops de 0,41-0,61 % el coste real refuta lo que el
        # supuesto de 2,0 bps aprueba, en cinco de siete perpetuos.
        if not modelo.spread_medido and not permitir_costes_supuestos:
            raise ValueError(
                f"{activo} se juzgaría con un spread {modelo.origen_spread} "
                f"({modelo.spread_bps:.2f} bps). El supuesto de 2,0 bps se queda "
                f"corto en 5 de 7 perpetuos y con stops estrechos aprueba lo que "
                f"el coste real refuta. "
                f"Mídelo (scripts/medir_spread_kraken.py) y usa "
                f"perfiles.costes_kraken(), o pasa permitir_costes_supuestos=True "
                f"si de verdad quieres un juicio con costes inventados."
            )
        return modelo
    veredicto = Veredicto(
        estrategia=estrategia.nombre,
        version=estrategia.version,
        activos_desarrollo=activos_desarrollo,
        activos_validacion=tuple(a for a in datos if a not in activos_desarrollo),
    )
    if not veredicto.activos_validacion:
        raise ValueError("no queda ningún activo para juzgar la hipótesis")

    # La puerta del holdout, ANTES de calcular nada: si los datos pisan un
    # periodo reservado, no se juzga. Se mira el rango real de las velas y no
    # lo que diga el llamador, porque el agujero anterior era justo ese —cada
    # script recortaba por su cuenta, y el que no lo hacía juzgaba igual—.
    if mercado != SIN_HOLDOUT:
        for activo, (velas, _, _) in datos.items():
            anotacion = ventanas.comprobar(
                velas, mercado, autorizacion=autorizacion_holdout)
            if anotacion:
                veredicto.ventana = anotacion | {"activo": activo}

    operaciones, percentiles, ratios_coste = [], [], []

    for activo, (velas, regimenes, funding) in datos.items():
        coste = coste_de(activo)
        senales = estrategia.generar(velas, regimenes, activo)
        resultado = ejecutar(
            velas, senales, costes=coste, funding=funding, nocional=nocional,
            **({"gestion": gestion} if gestion is not None else {}),
        )
        ops = resultado.a_dataframe()
        veredicto.resumen_por_activo[activo] = _metricas(ops) | {
            "desarrollo": activo in activos_desarrollo
        }
        if ops.is_empty() or activo in activos_desarrollo:
            continue

        operaciones.append(ops.with_columns(pl.lit(activo).alias("sym")))

        stop_pct = float(
            ((senales["entrada"] - senales["stop"]).abs() / senales["entrada"]).median()
        )
        ratios_coste.append(
            coste.operacion(nocional).total / (nocional * stop_pct * rr)
        )
        percentiles.append(
            _percentil_montecarlo(
                velas, activo, ops.height, stop_pct, rr,
                veredicto.resumen_por_activo[activo]["expectancy"],
                coste, nocional, simulaciones,
            )
        )

    if not operaciones:
        veredicto.criterios.append(
            Criterio("muestra", False, "0 operaciones", f">= {MUESTRA_MINIMA}")
        )
        return veredicto

    todas = pl.concat(operaciones)
    veredicto.agregado = _metricas(todas)
    a = veredicto.agregado

    signos = [
        v["expectancy"] > 0
        for k, v in veredicto.resumen_por_activo.items()
        if not v["desarrollo"] and v["n"] > 0
    ]
    consistentes = sum(signos)
    percentil_medio = statistics.mean(percentiles) if percentiles else 0.0
    coste_max = max(ratios_coste) if ratios_coste else 1.0

    veredicto.criterios = [
        Criterio("muestra", a["n"] >= MUESTRA_MINIMA, f"{a['n']} ops",
                 f">= {MUESTRA_MINIMA}"),
        Criterio("expectancy fuera de muestra", a["expectancy"] > 0,
                 f"{a['expectancy']:+.3f}R", "> 0"),
        Criterio("supera el azar", percentil_medio >= PERCENTIL_EXIGIDO,
                 f"percentil {percentil_medio:.0f}", f">= {PERCENTIL_EXIGIDO:.0f}"),
        Criterio(
            "consistencia entre activos",
            bool(signos) and p_binomial(consistentes, len(signos)) < 0.05,
            f"{consistentes} de {len(signos)} positivos "
            f"(p={p_binomial(consistentes, len(signos)):.3f})",
            "p < 0,05 frente a moneda justa",
        ),
        Criterio("sobrevive sin funding", a["pnl_sin_funding"] > 0,
                 f"{a['pnl_sin_funding']:+,.0f}", "> 0"),
        Criterio("coste sobre objetivo", coste_max < COSTE_SOBRE_OBJETIVO_MAX,
                 f"{coste_max:.1%}", f"< {COSTE_SOBRE_OBJETIVO_MAX:.0%}"),
    ]

    if registrar_en is not None:
        _registrar(registrar_en, estrategia, hipotesis, veredicto, nocional, rr, papel)

    return veredicto


def _registrar(store, estrategia, hipotesis, veredicto, nocional, rr, papel=None) -> None:
    """Deja constancia del experimento. El contador entra en el Deflated Sharpe."""
    identificador = f"{estrategia.nombre}_{estrategia.version}_{uuid.uuid4().hex[:8]}"
    parametros = json.dumps(
        {
            "version": estrategia.version,
            "nocional": nocional,
            "rr": rr,
            "desarrollo": list(veredicto.activos_desarrollo),
            "validacion": list(veredicto.activos_validacion),
        }
    )
    metricas = json.dumps(veredicto.agregado)
    notas = (
        "SOBREVIVE"
        if veredicto.aprobada
        else "REFUTADA: " + "; ".join(c.nombre for c in veredicto.criterios if not c.supera)
    )

    ahora = datetime.now(UTC)
    texto = hipotesis or estrategia.__doc__ or ""

    store.con.execute(
        "INSERT OR REPLACE INTO experimentos VALUES (?,?,?,?,?,?,?)",
        [identificador, ahora, estrategia.nombre, texto, parametros, metricas, notas],
    )

    if papel is None:
        return

    # La tabla vive en una base de 1,1 GB que no esta en git: si se pierde o se
    # reconstruye, el contador vuelve a cero y el Deflated Sharpe pasa a mentir
    # a nuestro favor. La copia encadenada va versionada, y se escribe aqui para
    # que no dependa de acordarse de exportarla.
    try:
        Registro(papel).anexar(
            "experimento",
            {
                "id": identificador,
                "ts_experimento": ahora.isoformat(),
                "estrategia": estrategia.nombre,
                # Se suben a primer nivel porque el expediente del proponente
                # los lee de ahi y no debe abrir `metricas`, que es justo lo
                # que no puede ver.
                "version": estrategia.version,
                "n": veredicto.agregado.get("n"),
                "hipotesis": texto,
                "parametros": parametros,
                "metricas": metricas,
                "notas": notas,
            },
        )
    except OSError as e:
        # El experimento ya esta en la base, que es la fuente. Tumbar aqui un
        # backtest de quince minutos por un fallo de disco seria peor que
        # perder la copia, y `scripts/registrar_experimento.py` la reconstruye.
        print(f"AVISO: el experimento no se pudo anexar a {papel}: {e}")
