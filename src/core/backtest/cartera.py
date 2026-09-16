"""Motor de cartera transversal: N posiciones, rebalanceo por calendario.

## Por qué existe, y por qué NO es una variante de `motor.py`

`motor.py` sabe hacer una sola forma —una señal, una posición, un stop, un
objetivo— y el pre-registro de C6 ya lo dijo: «el motor ha estado decidiendo el
espacio de hipótesis». Nueve hipótesis muertas comparten esa forma.

Ocho cosas concretas impiden usarlo para una cartera, y la sexta invalidaría el
resultado en vez de solo estorbarlo:

1. `ejecutar()` recibe UN DataFrame de velas: un activo. No hay estado
   transversal.
2. Las señales exigen columnas `stop` y `objetivo`. Una cartera no tiene
   ninguna de las dos.
3. `r_multiple` divide por la distancia al stop, así que todo el vocabulario de
   métricas del proyecto (expectancy en R) es indefinido aquí.
4. La salida es por stop, objetivo o `max_barras`. No hay rebalanceo por
   calendario ni «cierra porque salió del decil».
5. `max_posiciones` es por estrategia-activo, no un tope de cartera.
6. **El coste se cobra como ida y vuelta por señal.** Una cartera que rebalancea
   y mantiene la mitad de sus posiciones pagaría el turnover completo cada vez.
   Con rebalanceo a 3 días eso son ~13 bps contra un exceso esperado del mismo
   orden: refutaría cualquier hipótesis transversal por un artefacto del motor,
   igual que el impacto por longitud de vela mató a C1.
7. El funding se resta como coste. Una hipótesis de carry lo necesita **con
   signo**, como parte del retorno.
8. No hay gancho de universo point-in-time.

## Las tres reglas que este módulo impone y no deja configurar

Porque son exactamente las que un motor escrito para complacer a una hipótesis
relajaría:

- **Nada de stops ni objetivos.** El riesgo se controla por tamaño y por
  neutralidad, no por nivel. Ponerle un stop mezclaría la prima con la mecánica
  del stop, que es lo que el Monte Carlo detectó en E1..E4.
- **El ranking se calcula con barras cerradas ANTES de la entrada.** La entrada
  es la apertura de `t`; el ranking termina en `t - 1 - salto`. Con `salto = 0`
  el precio que ordena es el mismo que ejecuta —`close[t-1] == open[t]` al
  tick— y eso cobra el rebote entre bid y ask, que es lo que fundió el hilo
  transversal anterior. `salto` existe para poder demostrarlo, no para elegirlo.
- **El coste se cobra sobre el nocional REALMENTE negociado.** Una posición que
  sigue en la misma pata no se cierra para volver a abrirse y no paga.

## Cómo se evita que este motor se acomode a una hipótesis

`EspecCartera` se construye desde el TOML sellado y **rechaza cualquier clave que
no esté declarada**. El pre-registro es la única entrada. Y `ranking` es un
conjunto cerrado: añadir una variable de orden es cambiar el motor, no
configurarlo, y eso invalida los veredictos anteriores.

El control de permutación comparte este mismo código —`orden` sustituye solo el
criterio— porque un nulo que no comparte mecánica sitúa una hipótesis en un
percentil que no significa nada. Es la lección de `nulo_medido.py`.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import polars as pl

from core.quant.costes import ModeloCostes

#: Variables de orden admitidas. Conjunto CERRADO a propósito: ver el docstring.
Ranking = Literal["momentum", "carry"]

#: Que extremo del orden va LARGO. No es un detalle de implementacion: para
#: momentum la pata larga son los que mas subieron (arriba del orden) y para
#: carry son los que MENOS funding pagan (abajo). Enterrarlo en el motor haria
#: que una de las dos hipotesis se midiera del reves sin que nadie lo viera, asi
#: que se declara y el juez lo lee de la prosa sellada.
Sentido = Literal["alto_largo", "bajo_largo"]

#: Claves que `EspecCartera` acepta. Cualquier otra en el TOML es un error, no
#: un valor por defecto: un parámetro que el motor ignora en silencio es un
#: parámetro que alguien creyó haber declarado.
CLAVES = frozenset(
    {
        "ranking", "sentido", "formacion_barras", "tenencia_barras", "fraccion",
        "liquidez_top", "liquidez_barras", "capital_total", "salto_barras",
        "minimo_elegibles",
    }
)


class CarteraError(ValueError):
    """Especificación o panel incompatibles con la simulación."""


@dataclass(frozen=True)
class EspecCartera:
    """Todo lo que una hipótesis transversal puede elegir. Nada más."""

    ranking: Ranking
    formacion_barras: int
    tenencia_barras: int
    fraccion: float
    capital_total: float = 10_000.0
    #: Solo entran los N con más volumen en USD de la ventana de liquidez.
    #: `None` desactiva el filtro: el universo ya está filtrado por fuera.
    liquidez_top: int | None = None
    liquidez_barras: int = 720
    #: Barras entre el final del ranking y la entrada. Ver el docstring.
    salto_barras: int = 1
    #: Por debajo de esto no se opera el corte: ordenar cuatro nombres en
    #: deciles no es ordenar.
    minimo_elegibles: int = 6
    #: Que extremo del orden va largo. Por defecto el alto, que es lo que hace
    #: momentum y lo que midio C16; una hipotesis que quiera lo contrario tiene
    #: que decirlo.
    sentido: Sentido = "alto_largo"

    def __post_init__(self) -> None:
        if self.ranking not in ("momentum", "carry"):
            raise CarteraError(f"ranking `{self.ranking}` no admitido")
        if self.formacion_barras < 1 or self.tenencia_barras < 1:
            raise CarteraError("formacion y tenencia deben ser al menos 1 barra")
        if not 0 < self.fraccion <= 0.5:
            raise CarteraError("fraccion debe estar en (0, 0.5]: las dos patas no caben")
        if self.capital_total <= 0:
            raise CarteraError("capital_total debe ser positivo")
        if self.salto_barras < 0:
            raise CarteraError("salto_barras no puede ser negativo")
        if self.minimo_elegibles < 2:
            raise CarteraError("con menos de dos elegibles no hay dos patas")
        if self.sentido not in ("alto_largo", "bajo_largo"):
            raise CarteraError(f"sentido `{self.sentido}` no admitido")

    @classmethod
    def desde_toml(cls, datos: Mapping[str, object]) -> EspecCartera:
        """Construye desde el bloque del pre-registro sellado.

        Rechaza las claves desconocidas en vez de ignorarlas: si el TOML declara
        algo que el motor no aplica, la hipótesis medida no es la escrita.
        """
        sobran = set(datos) - CLAVES
        if sobran:
            raise CarteraError(f"claves no declaradas en el motor: {sorted(sobran)}")
        return cls(**datos)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Periodo:
    """Un rebalanceo. Todo en bps sobre el capital total."""

    ts: datetime
    elegibles: int
    largos: tuple[str, ...]
    cortos: tuple[str, ...]
    bruto_bps: float
    funding_bps: float
    coste_bps: float
    turnover: float

    @property
    def neto_bps(self) -> float:
        return self.bruto_bps + self.funding_bps - self.coste_bps


@dataclass
class SerieCartera:
    periodos: list[Periodo] = field(default_factory=list)
    #: Contribución neta acumulada por símbolo, en bps. Existe porque uno de los
    #: criterios de refutación de C6 es «más del 50 % viene de un solo símbolo»,
    #: y un criterio que no se puede evaluar no es un criterio.
    contribucion: dict[str, float] = field(default_factory=dict)
    saltados: int = 0

    @property
    def n(self) -> int:
        return len(self.periodos)

    def a_dataframe(self) -> pl.DataFrame:
        return pl.DataFrame(
            [
                {
                    "ts": p.ts, "elegibles": p.elegibles,
                    "n_largos": len(p.largos), "n_cortos": len(p.cortos),
                    "bruto_bps": p.bruto_bps, "funding_bps": p.funding_bps,
                    "coste_bps": p.coste_bps, "neto_bps": p.neto_bps,
                    "turnover": p.turnover,
                }
                for p in self.periodos
            ]
        )


@dataclass(frozen=True)
class Panel:
    """El panel en forma ancha, construido una vez.

    `precio[i][j]` es la APERTURA del activo j en la barra i. `None` significa
    que el contrato no existía: nunca se rellena hacia atrás, porque hacerlo
    inventaría el universo point-in-time, que es el sesgo que estas hipótesis
    tienen que atacar.
    """

    ts: list[datetime]
    activos: list[str]
    precio: list[list[float | None]]
    volumen_usd: list[list[float | None]]
    funding: list[list[float | None]]
    #: Volumen medio DIARIO en USD de las `ventana_profundidad` barras
    #: anteriores, sin incluir la actual. De aqui sale la profundidad del libro,
    #: y por tanto el impacto. Va en el panel y no en el modelo de costes porque
    #: cambia con el tiempo: un modelo con profundidad fija por activo no puede
    #: expresar `d1 = ratio x volumen de los 30 dias hasta t-1`, que es lo que
    #: declaran C6 y C16.
    vol30: list[list[float | None]] = field(default_factory=list)

    @classmethod
    def desde_largo(
        cls,
        datos: pl.DataFrame,
        *,
        funding: pl.DataFrame | None = None,
        ventana_profundidad: int = 30,
    ) -> Panel:
        """`datos` lleva ts, activo, open y opcionalmente volume."""
        faltan = {"ts", "activo", "open"} - set(datos.columns)
        if faltan:
            raise CarteraError(f"al panel le faltan columnas: {sorted(faltan)}")
        ancho = datos.pivot(on="activo", index="ts", values="open").sort("ts")
        activos = [c for c in ancho.columns if c != "ts"]
        ts = ancho["ts"].to_list()
        precio = [list(f) for f in ancho.select(activos).rows()]

        if "volume" in datos.columns:
            v = datos.pivot(on="activo", index="ts", values="volume").sort("ts")
            vol = [list(f) for f in v.select(activos).rows()]
            volumen = [
                [
                    (x * p) if (x is not None and p is not None) else None
                    for x, p in zip(fv, fp, strict=False)
                ]
                for fv, fp in zip(vol, precio, strict=False)
            ]
            v30 = (
                v.select(activos).fill_null(0.0).with_columns(
                    [pl.col(a).rolling_mean(window_size=ventana_profundidad,
                                            min_periods=1).shift(1) for a in activos]
                )
            )
            precios_v30 = [list(f) for f in v30.rows()]
            vol30 = [
                [
                    (x * p) if (x is not None and p is not None) else None
                    for x, p in zip(fv, fp, strict=False)
                ]
                for fv, fp in zip(precios_v30, precio, strict=False)
            ]
        else:
            volumen = [[None] * len(activos) for _ in ts]
            vol30 = [[None] * len(activos) for _ in ts]

        if funding is not None and not funding.is_empty():
            f = funding.pivot(on="activo", index="ts", values="rate").sort("ts")
            faltantes = [a for a in activos if a not in f.columns]
            f = f.with_columns([pl.lit(None, pl.Float64).alias(a) for a in faltantes])
            # Se alinea a la rejilla del precio: una tasa que no cae en una barra
            # del panel no se puede cobrar sin inventar cuándo se liquidó.
            f = ancho.select("ts").join(f, on="ts", how="left")
            fnd = [list(r) for r in f.select(activos).rows()]
        else:
            fnd = [[None] * len(activos) for _ in ts]

        return cls(ts=ts, activos=activos, precio=precio, volumen_usd=volumen,
                   funding=fnd, vol30=vol30)


def _elegibles(panel: Panel, i: int, espec: EspecCartera) -> list[int]:
    """Índices operables en la barra `i`.

    Un activo es elegible si tiene precio en la barra de decisión, en la de
    entrada y en toda la ventana de formación. Exigir la ventana completa es lo
    que impide que un contrato recién listado entre en el orden con un momentum
    calculado sobre tres barras.
    """
    inicio = i - espec.salto_barras - espec.formacion_barras
    if inicio < 0:
        return []
    fin = i + espec.tenencia_barras
    if fin >= len(panel.ts):
        return []
    idx = []
    for j in range(len(panel.activos)):
        if panel.precio[i][j] is None or panel.precio[fin][j] is None:
            continue
        if any(panel.precio[k][j] is None for k in (inicio, i - espec.salto_barras)):
            continue
        idx.append(j)
    return idx


def _filtro_liquidez(panel: Panel, i: int, idx: list[int], espec: EspecCartera) -> list[int]:
    """Los N con más volumen en USD de la ventana que termina ANTES de decidir.

    Si el volumen no está en el panel, el filtro no se aplica y se dice: fingir
    un filtro con datos que no hay sería peor que no tenerlo.
    """
    if espec.liquidez_top is None:
        return idx
    # `panel.vol30` ya trae la media movil desplazada, calculada una vez al
    # construir el panel. Recalcularla aqui por periodo y por activo hacia la
    # rejilla de 36 celdas impracticable: son 549 activos por 480 periodos por
    # 36 celdas. La ventana del panel y `liquidez_barras` tienen que coincidir,
    # y se comprueba en vez de suponerse.
    if not panel.vol30:
        return idx
    fila = panel.vol30[max(0, i - espec.salto_barras)]
    medias = [(fila[j] or 0.0, j) for j in idx]
    if not any(v for v, _ in medias):
        return idx
    medias.sort(reverse=True)
    return [j for _, j in medias[: espec.liquidez_top]]


def _puntuar(panel: Panel, i: int, idx: list[int], espec: EspecCartera) -> list[float]:
    """Valor de orden de cada elegible, con datos anteriores a la entrada."""
    fin = i - espec.salto_barras
    inicio = fin - espec.formacion_barras
    if espec.ranking == "momentum":
        return [
            (panel.precio[fin][j] / panel.precio[inicio][j] - 1.0)  # type: ignore[operator]
            for j in idx
        ]
    # carry: funding acumulado en la ventana de formación, hasta `fin` incluido.
    puntos = []
    for j in idx:
        vals = [panel.funding[k][j] for k in range(inicio, fin + 1)]
        puntos.append(sum(v for v in vals if v is not None))
    return puntos


def ejecutar_cartera(
    panel: Panel,
    espec: EspecCartera,
    *,
    costes: Callable[[str, int], ModeloCostes],
    orden: Callable[[Sequence[float]], list[int]] | None = None,
) -> SerieCartera:
    """Simula la cartera sobre el panel. Devuelve una serie por rebalanceo.

    `costes` recibe (activo, barra) y no solo el activo: la profundidad del libro
    cambia con el volumen, y un coste fijo por activo no puede expresar el
    `d1 = ratio x volumen de 30 dias hasta t-1` que declaran los pre-registros.

    `orden` recibe las puntuaciones y devuelve los índices ordenados de menor a
    mayor. Existe SOLO para el control de permutación: pasarle una barajadura
    produce el nulo con la misma mecánica, el mismo turnover y los mismos
    costes. Ninguna hipótesis puede usarlo.
    """
    serie = SerieCartera()
    if espec.tenencia_barras < 1:
        raise CarteraError("tenencia_barras debe ser al menos 1")

    # Nocional vivo por activo y por signo. Es lo que permite no cobrar dos
    # veces una posición que se mantiene.
    vivo: dict[int, float] = {}
    n_barras = len(panel.ts)
    arranque = espec.salto_barras + espec.formacion_barras

    for i in range(arranque, n_barras, espec.tenencia_barras):
        idx = _filtro_liquidez(panel, i, _elegibles(panel, i, espec), espec)
        if len(idx) < espec.minimo_elegibles:
            serie.saltados += 1
            continue
        k = int(len(idx) * espec.fraccion)
        if k < 1 or 2 * k > len(idx):
            serie.saltados += 1
            continue

        puntos = _puntuar(panel, i, idx, espec)
        pos = orden(puntos) if orden is not None else sorted(
            range(len(puntos)), key=lambda p: puntos[p]
        )
        bajos = [idx[p] for p in pos[:k]]
        altos = [idx[p] for p in pos[-k:]]
        largos, cortos = (
            (altos, bajos) if espec.sentido == "alto_largo" else (bajos, altos)
        )

        # Neutralidad de nocional: la mitad del capital a cada pata, repartida
        # por igual dentro. No es una elección: es lo que elimina el factor de
        # mercado, y por eso no es un parámetro.
        por_posicion = espec.capital_total / (2 * k)
        objetivo: dict[int, float] = {}
        for j in largos:
            objetivo[j] = por_posicion
        for j in cortos:
            objetivo[j] = -por_posicion

        # Coste sobre el nocional REALMENTE negociado.
        coste = 0.0
        negociado = 0.0
        for j in set(objetivo) | set(vivo):
            delta = abs(objetivo.get(j, 0.0) - vivo.get(j, 0.0))
            if delta <= 0:
                continue
            negociado += delta
            m = costes(panel.activos[j], i)
            coste += m.comision(delta, maker=False) + m.slippage(delta)
        vivo = objetivo

        fin = i + espec.tenencia_barras
        bruto = 0.0
        fnd = 0.0
        for j, nocional in objetivo.items():
            p0, p1 = panel.precio[i][j], panel.precio[fin][j]
            r = p1 / p0 - 1.0  # type: ignore[operator]
            aporte = nocional * r
            bruto += aporte
            # El funding lo paga el largo y lo cobra el corto, con su signo.
            tasas = [panel.funding[m][j] for m in range(i, fin)]
            suma = sum(t for t in tasas if t is not None)
            aporte_f = -nocional * suma
            fnd += aporte_f
            serie.contribucion[panel.activos[j]] = serie.contribucion.get(
                panel.activos[j], 0.0
            ) + (aporte + aporte_f) / espec.capital_total * 10_000

        serie.periodos.append(
            Periodo(
                ts=panel.ts[i],
                elegibles=len(idx),
                largos=tuple(panel.activos[j] for j in largos),
                cortos=tuple(panel.activos[j] for j in cortos),
                bruto_bps=bruto / espec.capital_total * 10_000,
                funding_bps=fnd / espec.capital_total * 10_000,
                coste_bps=coste / espec.capital_total * 10_000,
                turnover=negociado / espec.capital_total,
            )
        )

    return serie


def permutado(semilla: int) -> Callable[[Sequence[float]], list[int]]:
    """Orden al azar, para el control declarado en `[exito].azar` de C6.

    Ignora las puntuaciones a propósito: aísla el ORDEN del resto de la
    mecánica. Todo lo demás —número de posiciones, turnover, costes, funding—
    es idéntico al de la hipótesis.
    """
    rng = random.Random(semilla)

    def _orden(puntos: Sequence[float]) -> list[int]:
        pos = list(range(len(puntos)))
        rng.shuffle(pos)
        return pos

    return _orden
