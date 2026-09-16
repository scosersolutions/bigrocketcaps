"""La puerta del holdout: qué periodo puede ver un juicio, y quién lo autoriza.

## El agujero que cierra

El holdout vivía dentro de cada script que juzgaba. `barrido.py`, `juzgar_cripto.py`
y `juzgar_daytrading.py` lo recortan leyendo `[periodos]` de su pre-registro, y
lo hacen bien. `juzgar.py` no lo recorta: carga la serie entera. El 2026-09-04
juzgó E1..E4 y S0..S4 sobre un rango que incluye el holdout de cripto, que para
entonces ya existía como datos.

Es decir: el holdout no se saltó por descuido de una persona, sino porque nada
lo impedía. Un control que depende de que cada script se acuerde no es un
control, es una costumbre.

## Cómo funciona

`hipotesis/holdouts.toml` declara los periodos reservados, uno por mercado.
`validar()` mira el rango REAL de las velas que recibe y, si pisa un holdout,
se niega. Para abrirlo hay que pasar una autorización explícita, que queda
anotada en la cadena junto al veredicto.

No comprueba intenciones ni nombres de variables: mira las fechas de los datos
que se van a usar. Es la única comprobación que no se puede rodear sin querer.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import polars as pl

RAIZ = Path(__file__).resolve().parents[3]
DECLARACION = RAIZ / "hipotesis" / "holdouts.toml"


class HoldoutInvadido(Exception):
    """Se ha intentado juzgar con datos de un periodo reservado."""


@dataclass(frozen=True)
class Holdout:
    mercado: str
    desde: date
    hasta: date
    declarado: str
    motivo: str

    def solapa(self, desde: date, hasta: date) -> bool:
        """¿El rango de datos pisa el holdout? Intervalos cerrados."""
        return desde <= self.hasta and hasta >= self.desde


def cargar(ruta: Path | None = None) -> dict[str, Holdout]:
    ruta = ruta or DECLARACION
    if not ruta.exists():
        # Sin declaración no hay barrera, y eso tiene que notarse: devolver un
        # diccionario vacío en silencio sería exactamente el fallo de antes.
        raise HoldoutInvadido(
            f"no existe {ruta}. Sin holdouts declarados no se juzga: "
            f"un control que no se puede leer no protege de nada."
        )
    datos = tomllib.loads(ruta.read_text(encoding="utf-8"))
    fuera = {}
    for mercado, d in datos.items():
        fuera[mercado] = Holdout(
            mercado=mercado,
            desde=_fecha(d["desde"]), hasta=_fecha(d["hasta"]),
            declarado=str(d.get("declarado", "?")),
            motivo=str(d.get("motivo", "")).strip(),
        )
    return fuera


def _fecha(v: object) -> date:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    return date.fromisoformat(str(v))


def rango_de(velas: pl.DataFrame) -> tuple[date, date] | None:
    """Primera y última fecha de una tabla de velas, o None si no tiene `ts`."""
    if velas.is_empty() or "ts" not in velas.columns:
        return None
    lo, hi = velas["ts"].min(), velas["ts"].max()
    return (_fecha_ts(lo), _fecha_ts(hi))


def _fecha_ts(v: object) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def comprobar(
    velas: pl.DataFrame,
    mercado: str,
    *,
    autorizacion: str | None = None,
    holdouts: dict[str, Holdout] | None = None,
) -> dict[str, str] | None:
    """Falla si `velas` pisa el holdout de `mercado` sin autorización.

    Devuelve lo que hay que anotar en la cadena: el rango usado y, si se abrió
    el holdout, quién lo autorizó. Devuelve None si el mercado no tiene holdout
    declarado, que es distinto de que no lo pise.
    """
    tabla = holdouts if holdouts is not None else cargar()
    h = tabla.get(mercado)
    rango = rango_de(velas)
    if h is None or rango is None:
        return None
    desde, hasta = rango
    anotacion = {"rango_datos": f"{desde} .. {hasta}", "mercado": mercado}
    if not h.solapa(desde, hasta):
        return anotacion
    if not autorizacion:
        raise HoldoutInvadido(
            f"los datos van de {desde} a {hasta} y pisan el holdout de "
            f"{mercado} ({h.desde} .. {h.hasta}, declarado el {h.declarado}).\n"
            f"Recorta los datos antes de juzgar, o pasa `autorizacion_holdout` "
            f"con el hash del veredicto de validación que lo justifica. "
            f"Un holdout se abre UNA vez por hipótesis y queda anotado."
        )
    anotacion["holdout_abierto"] = autorizacion
    return anotacion
