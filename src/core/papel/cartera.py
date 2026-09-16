"""Cartera de demostración que avanza hacia delante en el tiempo.

## Qué es y qué no es

El registro de `papel_e7c.py` anota predicciones sueltas. Esto es un paso más:
una **cartera con capital**, que abre y cierra posiciones día a día con precios
reales, cobra comisiones y deslizamiento, y va acumulando un resultado.

No predice nada. Cada día hace lo que la regla dice con la información que
existe **ese día**, igual que haría alguien operando de verdad. Por eso hay que
esperar: no se puede adelantar, del mismo modo que no se puede adelantar un
experimento clínico.

La diferencia con el simulador del navegador es toda: aquel recorre datos que ya
se conocen, y este no sabe qué va a pasar mañana. Solo el segundo sirve para
validar o refutar una hipótesis.

## Cómo funciona

Un `tick` por sesión, que hace tres cosas en este orden:

1. **Cierra** lo que ha cumplido su horizonte, al cierre de la sesión.
2. **Abre** con las señales publicadas ayer, a la apertura de hoy. Nunca el
   mismo día de la publicación: es la regla de E7c y la razón es que el
   registro de la SEC da fecha sin hora.
3. **Valora** la cartera entera y anota el punto.

Si un día falla la ejecución, el siguiente `tick` recupera: las señales
pendientes siguen ahí y las posiciones vencidas se cierran con retraso, que es
lo que pasaría de verdad.

## El diario

Todo queda en un registro encadenado por hashes, igual que las predicciones:
cada evento lleva la huella del anterior. Retocar una operación pasada para que
el resultado cuadre rompe la cadena y se nota.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from core.papel.registro import Registro

#: Comisión de bróker minorista y deslizamiento sobre la horquilla. Son
#: supuestos declarados, no medidas: se dejan aquí arriba para que se puedan
#: discutir en vez de quedar escondidos dentro del cálculo.
COMISION_PCT = 0.0005
COMISION_MIN = 1.0
DESLIZAMIENTO_PCT = 0.0015


def coste(importe: float) -> float:
    return max(importe * COMISION_PCT, COMISION_MIN)


@dataclass
class Posicion:
    activo: str
    acciones: int
    precio_entrada: float      # ya con deslizamiento
    fecha_entrada: str
    sesiones_restantes: int
    n_insiders: int
    #: Lo que salió de la caja: importe con deslizamiento más comisión. Se
    #: guarda en vez de recalcularlo porque la comisión tiene mínimo y
    #: multiplicar precio por acciones no lo devuelve.
    gastado: float = 0.0
    #: Fecha de publicacion del Form 4 que la motivo. Es el "por que" de la
    #: posicion y el unico dato que la explica; sin el hay que ir al diario.
    senal: str = ""

    def valor(self, precio: float) -> float:
        return self.acciones * precio


@dataclass
class Cartera:
    """Estado reconstruido a partir del diario. Nunca se guarda como tal."""

    id: str
    capital_inicial: float
    horizonte: int
    max_posiciones: int
    caja: float
    abierta: bool = True
    posiciones: dict[str, Posicion] = field(default_factory=dict)
    cerradas: list[dict] = field(default_factory=list)
    curva: list[dict] = field(default_factory=list)
    costes: float = 0.0
    ultimo_tick: str | None = None
    #: Reglas de admisión de la cartera, congeladas al abrirla. Van aquí y no
    #: en el script para que una cartera vieja siga diciendo con qué reglas
    #: operó aunque el script cambie. Vacío = las de E7c.
    filtros: dict = field(default_factory=dict)
    nota: str = ""

    @property
    def por_posicion(self) -> float:
        pct = self.filtros.get("pct_posicion")
        if pct:
            return self.capital_inicial * pct
        return self.capital_inicial / self.max_posiciones


def reconstruir(reg: Registro, id_cartera: str) -> Cartera | None:
    """Rehace el estado leyendo el diario entero.

    Se reconstruye siempre en vez de guardar un resumen: un resumen podría
    desincronizarse del diario sin que nadie lo notara, y entonces el diario
    dejaría de ser la fuente de verdad.
    """
    c: Cartera | None = None
    for r in reg:
        if r.get("cartera") != id_cartera:
            continue
        t = r["tipo"]
        if t == "cartera_abierta":
            c = Cartera(id=id_cartera, capital_inicial=r["capital"],
                        horizonte=r["horizonte"], max_posiciones=r["max_posiciones"],
                        caja=r["capital"], filtros=r.get("filtros") or {},
                        nota=r.get("nota") or "")
        elif c is None:
            continue
        elif t == "posicion_abierta":
            c.posiciones[r["activo"]] = Posicion(
                activo=r["activo"], acciones=r["acciones"],
                precio_entrada=r["precio"], fecha_entrada=r["fecha"],
                sesiones_restantes=r["sesiones"], n_insiders=r.get("n_insiders", 0),
                gastado=round(r["importe"] + r["coste"], 2),
                senal=r.get("senal") or "")
            c.caja -= r["importe"] + r["coste"]
            c.costes += r["coste"]
        elif t == "posicion_cerrada":
            c.posiciones.pop(r["activo"], None)
            c.caja += r["importe"] - r["coste"]
            c.costes += r["coste"]
            c.cerradas.append(r)
        elif t == "valoracion":
            c.curva.append({"fecha": r["fecha"], "total": r["total"]})
            c.ultimo_tick = r["fecha"]
            for act, resta in (r.get("restantes") or {}).items():
                if act in c.posiciones:
                    c.posiciones[act].sesiones_restantes = resta
        elif t == "cartera_cerrada":
            c.abierta = False
    return c


def abiertas(reg: Registro) -> list[str]:
    """Identificadores de cartera declarados en el diario, en orden de apertura."""
    vistas: list[str] = []
    for r in reg:
        if r["tipo"] == "cartera_abierta" and r["cartera"] not in vistas:
            vistas.append(r["cartera"])
    return vistas


def abrir(reg: Registro, id_cartera: str, capital: float, horizonte: int,
          max_posiciones: int, nota: str = "", filtros: dict | None = None) -> dict:
    return reg.anexar("cartera_abierta", {
        "cartera": id_cartera, "capital": capital, "horizonte": horizonte,
        "max_posiciones": max_posiciones, "nota": nota,
        # Las reglas quedan escritas al abrir, igual que los criterios de una
        # predicción: si mañana se cambian, esta cartera sigue diciendo con
        # cuáles operó y el cambio no puede colarse hacia atrás.
        "filtros": filtros or {},
        "comision_pct": COMISION_PCT, "comision_min": COMISION_MIN,
        "deslizamiento_pct": DESLIZAMIENTO_PCT,
    })


def tick(reg: Registro, c: Cartera, hoy: str, precios: dict[str, dict],
         candidatas: list[dict]) -> dict:
    """Avanza una sesión. `precios` es {activo: {"open":…, "close":…}}.

    `candidatas` son las señales publicadas la sesión anterior, ya filtradas
    por quien llama. Aquí no se decide qué es señal: eso lo fija la hipótesis.

    Lo que sí se comprueba aquí es que ninguna candidata sea de **hoy o
    después**: comprar en la apertura de la sesión en que se publicó el Form 4
    es entrar antes de que la información fuese pública. El dataset da fecha
    sin hora y muchos formularios entran tras el cierre, que es justo el motivo
    por el que E7c entra en la apertura de la sesión SIGUIENTE.

    El aviso de arriba lo prometía y nadie lo verificaba. Con el trabajo diario
    a las 07:00 UTC —Nueva York cerrada— la última sesión con datos es la del
    día anterior, así que la comprobación no es teórica.
    """
    resumen = {"cerradas": 0, "abiertas": 0, "sin_precio": [], "descartadas": []}
    tardias = [x["activo"] for x in candidatas if x.get("fecha_senal", "") >= hoy]
    if tardias:
        resumen["descartadas"] = tardias
        candidatas = [x for x in candidatas if x.get("fecha_senal", "") < hoy]

    # 1. Cerrar lo vencido, al cierre de hoy.
    for act in list(c.posiciones):
        p = c.posiciones[act]
        if p.sesiones_restantes > 0:
            continue
        px = (precios.get(act) or {}).get("close")
        if not px:
            resumen["sin_precio"].append(act)
            continue                      # se reintenta el siguiente tick
        bruto = px * p.acciones * (1 - DESLIZAMIENTO_PCT)
        com = coste(bruto)
        ev = reg.anexar("posicion_cerrada", {
            "cartera": c.id, "activo": act, "fecha": hoy,
            "acciones": p.acciones, "precio": round(px, 4),
            "importe": round(bruto, 2), "coste": round(com, 2),
            "precio_entrada": p.precio_entrada, "fecha_entrada": p.fecha_entrada,
            "resultado_pct": round((px / p.precio_entrada - 1) * 100, 3),
        })
        c.caja += bruto - com
        c.costes += com
        # El estado en memoria tiene que quedar igual que el reconstruido desde
        # el diario. Si divergieran, el diario dejaría de ser la fuente de
        # verdad sin que nadie lo notara.
        c.cerradas.append(ev)
        del c.posiciones[act]
        resumen["cerradas"] += 1

    # 2. Abrir, a la apertura de hoy, con lo publicado ayer.
    for sig in candidatas:
        if len(c.posiciones) >= c.max_posiciones:
            break
        act = sig["activo"]
        if act in c.posiciones:
            continue
        px = (precios.get(act) or {}).get("open")
        if not px or px <= 0:
            continue
        efectivo = px * (1 + DESLIZAMIENTO_PCT)
        acciones = int(min(c.por_posicion, c.caja * 0.95) // efectivo)
        if acciones < 1:
            continue
        bruto = acciones * efectivo
        com = coste(bruto)
        if bruto + com > c.caja:
            continue
        reg.anexar("posicion_abierta", {
            "cartera": c.id, "activo": act, "fecha": hoy,
            "acciones": acciones, "precio": round(efectivo, 4),
            "importe": round(bruto, 2), "coste": round(com, 2),
            "sesiones": c.horizonte, "n_insiders": sig.get("n_insiders", 0),
            "senal": sig.get("fecha_senal"),
        })
        c.posiciones[act] = Posicion(act, acciones, round(efectivo, 4), hoy,
                                     c.horizonte, sig.get("n_insiders", 0),
                                     gastado=round(bruto + com, 2),
                                     senal=sig.get("fecha_senal") or "")
        c.caja -= bruto + com
        c.costes += com
        resumen["abiertas"] += 1

    # 3. Valorar y descontar una sesión a todo lo abierto.
    valor_cartera = 0.0
    restantes: dict[str, int] = {}
    for act, p in c.posiciones.items():
        px = (precios.get(act) or {}).get("close") or p.precio_entrada
        valor_cartera += p.valor(px)
        # La posición abierta hoy consume su primera sesión hoy mismo.
        p.sesiones_restantes = max(0, p.sesiones_restantes - 1)
        restantes[act] = p.sesiones_restantes

    total = c.caja + valor_cartera
    reg.anexar("valoracion", {
        "cartera": c.id, "fecha": hoy,
        "caja": round(c.caja, 2), "posiciones": round(valor_cartera, 2),
        "total": round(total, 2),
        "rentabilidad_pct": round((total / c.capital_inicial - 1) * 100, 3),
        "n_posiciones": len(c.posiciones), "restantes": restantes,
    })
    c.curva.append({"fecha": hoy, "total": round(total, 2)})
    c.ultimo_tick = hoy
    resumen["total"] = round(total, 2)
    resumen["rentabilidad"] = round((total / c.capital_inicial - 1) * 100, 3)
    return resumen


def cerrar(reg: Registro, c: Cartera, hoy: str, precios: dict[str, dict],
           motivo: str) -> dict:
    """Liquida todo y cierra la cartera. Sirve para pararla antes de tiempo."""
    for act in list(c.posiciones):
        p = c.posiciones[act]
        px = (precios.get(act) or {}).get("close") or p.precio_entrada
        bruto = px * p.acciones * (1 - DESLIZAMIENTO_PCT)
        com = coste(bruto)
        reg.anexar("posicion_cerrada", {
            "cartera": c.id, "activo": act, "fecha": hoy,
            "acciones": p.acciones, "precio": round(px, 4),
            "importe": round(bruto, 2), "coste": round(com, 2),
            "precio_entrada": p.precio_entrada, "fecha_entrada": p.fecha_entrada,
            "resultado_pct": round((px / p.precio_entrada - 1) * 100, 3),
            "motivo": "liquidacion",
        })
        c.caja += bruto - com
        c.costes += com
        del c.posiciones[act]

    reg.anexar("cartera_cerrada", {
        "cartera": c.id, "fecha": hoy, "motivo": motivo,
        "capital_final": round(c.caja, 2),
        "rentabilidad_pct": round((c.caja / c.capital_inicial - 1) * 100, 3),
        "costes": round(c.costes, 2),
    })
    c.abierta = False
    return {"capital_final": round(c.caja, 2),
            "rentabilidad": round((c.caja / c.capital_inicial - 1) * 100, 3)}
