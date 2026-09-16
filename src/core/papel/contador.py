"""El contador de contrastes, en un solo sitio.

## Por qué existe este módulo

El número que entra en el denominador del Deflated Sharpe llegó a estar escrito
de cuatro formas distintas y ninguna coincidía:

    387 → 423   `hipotesis/C6-...toml`, campo `contabilidad`, a mano
    570         `hipotesis/C11-...toml` e `infoproyecto.md`, en prosa
    746         el cuaderno cripto, calculado mal
    576         la suma de los presupuestos declarados, correcta pero incompleta

El 746 salía de `experimentos + comprometido`, y eso **cuenta dos veces**: las
108 filas de tipo `experimento` de C5 son exactamente las 108 celdas que su
propio presupuesto ya declara. Sumarlas es contar C5 dos veces, y C1 y C2..C4
también.

## Cómo se cuenta bien

    contador = suma de `presupuesto` de las propuestas
             + filas `experimento` que NO pertenecen a ninguna propuesta

Lo segundo son los experimentos anteriores al mecanismo de propuestas —E1..E4 y
las cinco variantes de salida S0..S4—, que gastaron contrastes sin dejar un
registro de presupuesto. Son 31, los mismos 31 de la tabla `experimentos` de
DuckDB.

## Lo que NO se descuenta

Una hipótesis **retirada** sigue contando. El denominador mide el espacio de
búsqueda recorrido, no las ejecuciones: alguien la pensó, se comprometió con
ella y pudo haberla corrido. Si retirar descontara, el denominador sería
ajustable después de ver los resultados.

## Y lo que las filas `experimento` no sirven para contar

No son homogéneas, y es un defecto de la contabilidad que conviene tener a la
vista: C5 y C1 anotan una fila por celda, pero C7, C8, C10, C11, C12 y C13
anotan **solo la celda seleccionada**. Contar filas daría 170 cuando el
presupuesto realmente comprometido es 576. Por eso el contador se calcula de los
presupuestos y no de las ejecuciones.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Contador:
    """El desglose entero, para que el cuaderno pueda enseñar de dónde sale."""

    comprometido: int
    legado: int
    retiradas: tuple[str, ...]
    filas_experimento: int

    @property
    def total(self) -> int:
        return self.comprometido + self.legado


def _cubierta_por(fila_id: str, ids: Sequence[str]) -> bool:
    """Si esta fila de experimento pertenece a una hipótesis con presupuesto.

    Las filas se identifican de dos formas según la época: `C5_celda_001` lleva
    el id delante con guion bajo, y las de C7 en adelante llevan el id a secas.
    Las de E1..E4 y S0..S4 no coinciden con ninguna propuesta, y ésas son
    justamente las que hay que sumar aparte.
    """
    return any(fila_id == i or fila_id.startswith(f"{i}_") for i in ids)


def contar(registros: Iterable[dict]) -> Contador:
    """Calcula el contador a partir de los registros de la cadena."""
    regs = list(registros)
    presupuestos = {
        str(r["id"]): int(r["presupuesto"])
        for r in regs
        if r.get("tipo") == "propuesta" and r.get("presupuesto")
    }
    ids = sorted(presupuestos, key=len, reverse=True)
    retiradas = tuple(
        str(r["id"]) for r in regs if r.get("tipo") == "retirada" and r.get("id")
    )
    filas = [r for r in regs if r.get("tipo") == "experimento"]
    legado = sum(1 for r in filas if not _cubierta_por(str(r.get("id", "")), ids))
    return Contador(
        comprometido=sum(presupuestos.values()),
        legado=legado,
        retiradas=retiradas,
        filas_experimento=len(filas),
    )
