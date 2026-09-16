"""Informe de análisis por valor: qué ha pasado, qué lo explica y qué vigilar.

## Qué venía antes y por qué no servía

El único informe que salía por Telegram y por correo era la alerta de
volatilidad: «AAPL publica resultados en tres días, se mueve un ±4 %, MANTENER».
Es verdad y es poco. Todo lo que el cuaderno enseña —resultados contra su tramo
de cobertura, ingresos, 8-K presentados, consenso de analistas, compras de
directivos, recorrido histórico del propio valor— ya estaba en la base y no
llegaba a ningún canal. Este módulo no inventa fuentes: reúne las que ya
alimentan el cuaderno y las pone en orden de lectura.

## Las tres líneas que no se cruzan

1. **Dirección, ninguna.** El sistema no tiene precio objetivo, y E7c sale por
   tiempo, nunca por precio. El estado que emite este informe se refiere a la
   EXPOSICIÓN alrededor de un evento —evitar, reducir, vigilar— exactamente
   como `alerta_volatilidad`, que es lo único medido: la magnitud del
   movimiento en resultados, cuya dirección salió simétrica sobre 10.975
   anuncios.

2. **Dato, interpretación y evidencia van separados y etiquetados.** Cada
   factor lleva su cifra, qué significa esa cifra, y qué se sigue de ella. Lo
   que es opinión de terceros —el consenso de analistas— se atribuye a los
   terceros. Lo que es hipótesis en observación —E7c— se dice que lo es.

3. **Lo que falta se declara.** Un valor sin fundamentales no produce un bloque
   de fundamentales vacío ni uno inventado: produce una línea en «lo que no se
   ha podido mirar». Un informe que no distingue «malo» de «desconocido» es
   peor que no mandar informe.

## Noticias

El proyecto no tiene una fuente de prensa y no se le añade una aquí. Su
equivalente verificable son los **8-K**: hechos de presentación obligatoria,
fechados por la SEC, con un código de item que dice de qué van sin que nadie lo
interprete. Solo se marcan adversos los items que el propio formulario define
así. Si no hay ninguno reciente, el informe lo dice en vez de rellenar.

## Qué ha cambiado

Cada informe deja una instantánea en `data/informes/estado.json` y el siguiente
la compara. Sin eso el informe diario es el de ayer otra vez, que es la queja
concreta que lo motivó. El fichero es caché reescribible y **no** tiene nada que
ver con `data/papel/e7c.jsonl`, que es de solo anexado y no se toca.
"""

from __future__ import annotations

import html as _html
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from core.delivery import ficha as F
from core.delivery.alerta_volatilidad import (
    FRACCION_HUECO,
    MUESTRA_MINIMA_ACTIVO,
    REFERENCIA_RESULTADOS,
)
from core.store.duck import Store

#: Ventana en la que un anuncio de resultados cuenta como «próximo».
DIAS_EVENTO = 10

#: Un 8-K más viejo que esto ya no es noticia de este informe.
DIAS_HECHO = 45

#: Una señal de E7c más vieja que esto tampoco.
DIAS_SENAL = 60

#: Movimiento diario mediano a partir del cual el valor se considera movido.
#: Es el mismo corte que usa la capa de lectura del cuaderno.
VOL_ALTA = 0.03
VOL_MEDIA = 0.015

#: Diferencia mínima contra el tramo de cobertura para llamarlo mejor o peor.
#: Por debajo, «bate 7 de 8» y «bate 6 de 8» son el mismo ruido.
MARGEN_BATE = 0.08

#: El relleno de la seccion de riesgos cuando no hay ninguno medible. Es una
#: frase util en su sitio y ruido en el resumen, asi que se reconoce por aqui.
SIN_RIESGO_MEDIBLE = ("Ningún riesgo medible en los datos disponibles. Eso no es lo "
                      "mismo que no haberlo: es que este sistema no lo ve.")

SIGNOS = {"favor": "A favor", "contra": "En contra",
          "neutral": "Sin dirección", "sindato": "Sin datos"}

ICONO = {"favor": "🟢", "contra": "🔴", "neutral": "⚪", "sindato": "⚫"}


def _es(v: float, dec: int = 2) -> str:
    """Cifra en castellano: punto de millar y coma decimal.

    El cuaderno ya escribe así y el informe sale de la misma base; que un valor
    apareciera como «215,938,000,000» en el correo y como «215,9 mM» en la
    pantalla es la clase de detalle que hace dudar de si son el mismo dato.
    """
    return f"{v:,.{dec}f}".replace(",", "@").replace(".", ",").replace("@", ".")


def _corto(v: float | None) -> str:
    """Magnitud abreviada, con las mismas escalas que `corto()` del cuaderno."""
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e12:
        return _es(v / 1e12, 1) + " B"
    if a >= 1e9:
        return _es(v / 1e9, 1) + " mM"
    if a >= 1e6:
        return _es(v / 1e6, 1) + " M"
    if a >= 1e3:
        return _es(v / 1e3, 0) + " k"
    return _es(v, 0)


def _pc(v: float, dec: int = 1) -> str:
    """Porcentaje con signo, a partir de una fracción."""
    return ("+" if v >= 0 else "-") + _es(abs(v) * 100, dec) + " %"


@dataclass(frozen=True)
class Factor:
    """Un motivo, con las tres piezas separadas a propósito.

    `dato` es lo medido. `interpretacion` es qué significa ese número. `impacto`
    es qué se sigue de él. Fundirlos en una frase es exactamente como un dato se
    convierte en una recomendación sin que nadie lo decida.
    """

    clave: str
    titulo: str
    signo: str
    dato: str
    interpretacion: str
    impacto: str
    fuente: str = ""


@dataclass
class Informe:
    activo: str
    nombre: str
    generado: datetime
    precio: float | None = None
    objetivo: float | None = None
    exposicion: str = "SIN AVISO"
    exposicion_motivo: str = ""
    exposicion_previa: str | None = None
    #: Conclusion en escala de compra o venta. Es de LOS ANALISTAS, nunca del
    #: sistema: ver `veredicto_analistas`.
    veredicto: dict | None = None
    evento: dict | None = None
    factores: list[Factor] = field(default_factory=list)
    hechos: list[dict] = field(default_factory=list)
    riesgos: list[str] = field(default_factory=list)
    cambios: list[str] = field(default_factory=list)
    vigilar: list[str] = field(default_factory=list)
    faltan: list[str] = field(default_factory=list)
    motivo_inclusion: list[str] = field(default_factory=list)
    #: Cierres del ultimo año y los indices con compras de insiders dentro de
    #: esa ventana. Solo los usa el correo, que es el unico canal que puede
    #: enseñar una imagen.
    serie: list[float] = field(default_factory=list)
    marcas: list[int] = field(default_factory=list)
    #: `acciones` o `cripto`. Cambia que se puede afirmar y con que calendario
    #: se juzga: un perpetuo cotiza los siete dias de la semana.
    mercado: str = "acciones"

    # --- lecturas derivadas -------------------------------------------------

    @property
    def a_favor(self) -> list[Factor]:
        return [f for f in self.factores if f.signo == "favor"]

    @property
    def en_contra(self) -> list[Factor]:
        return [f for f in self.factores if f.signo == "contra"]

    @property
    def contradictorio(self) -> bool:
        return bool(self.a_favor) and bool(self.en_contra)

    @property
    def evidencia(self) -> str:
        """Cuánta base tiene el informe, sin convertirlo en una probabilidad.

        Se cuenta cuántos bloques han podido pronunciarse, no cuántos apuntan
        hacia arriba. Un valor con seis bloques que se contradicen tiene MÁS
        evidencia que uno con dos que coinciden, y decir lo contrario sería
        confundir acuerdo con información.
        """
        con = [f for f in self.factores if f.signo != "sindato"]
        if not con:
            return "Sin evidencia: ningún bloque de datos ha podido pronunciarse."
        partes = [f"{len(con)} de {len(self.factores)} bloques con datos"]
        if self.contradictorio:
            partes.append(
                f"señales contradictorias ({len(self.a_favor)} a favor, "
                f"{len(self.en_contra)} en contra)")
        if self.faltan:
            partes.append(f"{len(self.faltan)} sin datos")
        return " · ".join(partes)

    @property
    def resumen(self) -> list[str]:
        """El bloque que contesta «¿qué pasa aquí y por qué me importa?».

        Se arma de los factores reales, no de una plantilla: si un valor no
        tiene nada en contra, no aparece una línea de «en contra» vacía.
        """
        L: list[str] = []
        if self.cambios:
            L.append("Cambia: " + "; ".join(self.cambios[:3]) + ".")
        else:
            L.append("Sin cambios medibles respecto al informe anterior.")

        if self.a_favor:
            L.append("Empuja a favor: "
                     + "; ".join(f"{f.titulo.lower()} ({f.dato})" for f in self.a_favor[:3])
                     + ".")
        if self.en_contra:
            L.append("Empuja en contra: "
                     + "; ".join(f"{f.titulo.lower()} ({f.dato})" for f in self.en_contra[:3])
                     + ".")
        if not self.a_favor and not self.en_contra:
            L.append("Ningún bloque con datos apunta en una dirección concreta.")

        if self.hechos:
            h = self.hechos[0]
            L.append(f"Último hecho declarado: {h['f']}, "
                     + ", ".join(i["t"] for i in h["i"]) + ".")
        if self.evento:
            L.append(f"Resultados el {self.evento['fecha']} "
                     f"({self.evento['cuando']}): el movimiento típico de este valor "
                     f"en sus anuncios es de ±{_es(self.evento['tipico'], 1)} %.")
        # El relleno de «ningún riesgo medible» es correcto en su sección y
        # engañoso aquí: un resumen que anuncia un riesgo principal y luego
        # dice que no hay ninguno gasta la línea más leída del informe.
        if self.riesgos and self.riesgos[0] != SIN_RIESGO_MEDIBLE:
            L.append("Riesgo principal: " + self.riesgos[0])
        if self.vigilar:
            L.append("A vigilar: " + "; ".join(self.vigilar[:2]) + ".")
        return L

    @property
    def conclusion(self) -> str:
        """Corta, y honesta cuando no hay nada que concluir."""
        con = [f for f in self.factores if f.signo != "sindato"]
        if not con:
            return ("No hay evidencia suficiente para decir nada de este valor: "
                    "ningún bloque de datos ha podido pronunciarse.")
        # La rama cripto va PRIMERO: las de abajo hablan de eventos de
        # resultados y de un pre-registro que son de acciones, y un perpetuo
        # que caiga en cualquiera de ellas diría algo que no le corresponde.
        if self.mercado == "cripto":
            return ("Este informe no concluye una dirección y no puede: para un "
                    "perpetuo no hay consenso, ni cuentas, ni hechos declarados, y "
                    "la rama cripto de este proyecto se cerró con sus dieciséis "
                    "hipótesis refutadas. Lo que queda es el tamaño del movimiento, "
                    "que es vigilancia de riesgo y no una idea de inversión.")
        if self.exposicion in ("EVITAR", "REDUCIR"):
            return (f"Lo único accionable es la exposición: {self.exposicion.lower()} "
                    f"antes del evento. {self.exposicion_motivo} "
                    "No hay señal de compra ni de venta: el sistema no tiene "
                    "dirección y no la va a tener por acumular datos.")
        if self.contradictorio:
            return ("Los datos se contradicen, y eso es el resultado: no hay una "
                    "lectura única que sostener. Lo que queda son los hechos de "
                    "arriba, cada uno con su fuente.")
        if self.a_favor and not self.en_contra:
            return ("Todos los bloques que se pronuncian van en la misma dirección. "
                    "Coincidir no es haber demostrado ventaja: ninguno de estos "
                    "bloques es una estrategia validada, y el pre-registro que "
                    "podría validarla sigue abierto.")
        if self.en_contra and not self.a_favor:
            return ("Lo que hay son factores en contra y ninguno a favor. Eso "
                    "describe lo ocurrido; no es una señal de venta, que el "
                    "sistema no emite.")
        return ("Los datos están, y no autorizan ninguna acción concreta. "
                "Se deja como seguimiento.")


# --------------------------------------------------------------------------
# Piezas de análisis
# --------------------------------------------------------------------------


def _serie(store: Store, act: str, n: int = 400) -> list[tuple[date, float]]:
    return _serie_mercado(store, act, "stock_us", n)


def _pct(a: float, b: float) -> float | None:
    return (b / a - 1.0) * 100 if a else None


def _f_momentum(serie: list[tuple[date, float]], mercado: str = "acciones") -> Factor:
    """Lo que ha hecho el precio. Descriptivo, y por eso sin signo.

    Marcarlo «a favor» porque subió sería justo la extrapolación que el proyecto
    no puede hacer: en cripto midió momentum transversal con el motor de cartera
    y no cubrió costes, y en acciones no hay ninguna hipótesis de momentum
    validada. Es contexto, no un voto.
    """
    if len(serie) < 30:
        return Factor("momentum", "Comportamiento reciente", "sindato", "—",
                      "Menos de 30 sesiones de precio en la base.",
                      "No se puede describir el recorrido.", "serie de precios")
    px = [c for _, c in serie]
    ultimo = px[-1]
    r21 = _pct(px[-22], ultimo) if len(px) > 22 else None
    r63 = _pct(px[-64], ultimo) if len(px) > 64 else None
    ventana = px[-252:] if len(px) >= 252 else px
    lo, hi = min(ventana), max(ventana)
    sitio = (ultimo - lo) / (hi - lo) * 100 if hi > lo else None

    piezas = []
    if r21 is not None:
        piezas.append(_pc(r21 / 100) + " en un mes")
    if r63 is not None:
        piezas.append(_pc(r63 / 100) + " en tres meses")
    return Factor(
        "momentum", "Comportamiento reciente", "neutral",
        " · ".join(piezas) or "—",
        (f"Está en el {sitio:.0f} % del rango de sus últimas "
         f"{len(ventana)} sesiones ({_es(lo)} a {_es(hi)} $)."
         if sitio is not None else "Sin rango suficiente para situarlo."),
        ("Es contexto. En cripto el momentum se midió —C16, momentum transversal "
         "sobre 549 perpetuos— y quedó refutado: Sharpe 0,479, drawdown 34,3 %. "
         "No es que no se sepa; es que se miró y no bastaba."
         if mercado == "cripto" else
         "Es contexto: el proyecto no tiene ninguna hipótesis de momentum "
         "validada en acciones, así que de aquí no sale ninguna dirección."),
        "serie de precios",
    )


def _f_resultados(res: dict) -> Factor:
    if not res:
        return Factor("resultados", "Resultados", "sindato", "—",
                      "Sin calendario de resultados para este valor.",
                      "No se puede comparar con su consenso.", "calendario de Nasdaq")
    de, bate, ref = res.get("de") or 0, res.get("bate") or 0, res.get("referencia")
    if not de:
        return Factor("resultados", "Resultados", "sindato", "—",
                      "Ningún trimestre con previsión por encima de 0,05 $ de BPA: "
                      "por debajo, el porcentaje de sorpresa no significa nada.",
                      "No se puede medir si cumple lo que le piden.",
                      "calendario de Nasdaq")
    obs = bate / de
    if ref is None:
        return Factor("resultados", "Resultados", "neutral",
                      f"bate {bate} de {de} ({obs * 100:.0f} %)",
                      "No consta cuántos analistas lo cubren, así que no hay tramo "
                      "de referencia contra el que leer ese porcentaje.",
                      "El dato está; la comparación no.", "calendario de Nasdaq")
    d = obs - ref
    signo = "favor" if d >= MARGEN_BATE else "contra" if d <= -MARGEN_BATE else "neutral"
    return Factor(
        "resultados", "Resultados", signo,
        f"bate {bate} de {de} ({obs * 100:.0f} %) frente al {ref * 100:.0f} % "
        f"de su tramo",
        (f"Batir el consenso es lo normal: medido sobre 56.984 anuncios, la tasa "
         f"sube con la cobertura, y con {round(res['cobertura'])} analistas lo "
         f"esperable es {ref * 100:.0f} %. Lo que informa es la diferencia, "
         f"{('+' if d >= 0 else '-') + _es(abs(d) * 100, 0)} puntos."),
        ("Cumple mejor de lo que le corresponde por cobertura."
         if signo == "favor" else
         "Cumple peor de lo que le corresponde por cobertura."
         if signo == "contra" else
         "Está donde le toca: ese porcentaje no distingue a este valor de su tramo."),
        "calendario de Nasdaq",
    )


def _f_fundamentales(fun: dict) -> Factor:
    ing = (fun or {}).get("ingresos")
    if not ing or len(ing.get("anuales") or []) < 2:
        return Factor("fundamentales", "Ingresos", "sindato", "—",
                      "Menos de dos cierres anuales publicados en XBRL.",
                      "Sin dos ejercicios no hay evolución que leer.",
                      "XBRL de EDGAR")
    an = ing["anuales"]
    prev, ult = an[-2]["valor"], an[-1]["valor"]
    if not prev:
        return Factor("fundamentales", "Ingresos", "neutral",
                      f"{_corto(ult)} USD", "El ejercicio anterior no es comparable.",
                      "Sin base de comparación.", "XBRL de EDGAR")
    cre = ult / prev - 1
    signo = "favor" if cre >= 0.05 else "contra" if cre <= -0.05 else "neutral"
    return Factor(
        "fundamentales", "Ingresos", signo,
        _pc(cre) + " en el último ejercicio cerrado",
        (f"Ingresos de {an[-1]['fin'][:4]}: {_corto(ult)} USD frente a "
         f"{_corto(prev)} USD el año anterior. Se usa la PRIMERA publicación de cada "
         f"cierre, que es cuando el dato se supo; la última daría la cifra "
         f"revisada y fecharía mal cuándo estuvo disponible."),
        ("El negocio crece." if signo == "favor"
         else "El negocio encoge." if signo == "contra"
         else "El negocio está plano."),
        "XBRL de EDGAR",
    )


def _f_analistas(ana: dict, precio: float | None) -> Factor:
    if not ana:
        return Factor("analistas", "Analistas", "sindato", "—",
                      "Ninguna casa publica objetivo para este valor.",
                      "Sin consenso que contrastar.", "consenso de Nasdaq")
    n = ana.get("n") or 0
    compra, venta = ana.get("compra") or 0, ana.get("venta") or 0
    pide = (ana["objetivo"] / precio - 1) if precio else None
    # El signo es el de LOS ANALISTAS, y se dice. No es una opinión del sistema:
    # de sus objetivos de PRECIO el proyecto no ha medido nada.
    signo, porque = "neutral", "ni el reparto ni el objetivo son concluyentes"
    if n:
        if compra / n >= 0.6 and (pide is None or pide > 0):
            signo, porque = ("favor",
                             f"{compra} de {n} casas dicen comprar y el objetivo está "
                             f"por encima del precio")
        elif venta / n >= 0.3:
            # El reparto manda sobre el objetivo, y se dice: un objetivo por
            # encima del precio con la mitad de las casas diciendo vender es
            # justo el caso en el que leer solo el objetivo engaña.
            signo, porque = ("contra",
                             f"{venta} de {n} casas dicen vender; eso pesa más que un "
                             f"objetivo por encima del precio, que casi siempre lo está")
        elif pide is not None and pide < -0.05:
            signo, porque = ("contra", "el objetivo de consenso está por debajo del "
                                       "precio de hoy")
    return Factor(
        "analistas", "Consenso de analistas", signo,
        (f"objetivo {_es(ana['objetivo'])} $"
         + (f" ({_pc(pide, 0)} sobre el precio de hoy)" if pide is not None else "")
         + (f" · {compra} comprar / {ana.get('mantener') or 0} mantener / {venta} vender"
            if n else "")),
        (f"Es la opinión publicada de {ana.get('casas') or n} casas, con su mínimo "
         f"({_es(ana.get('minimo') or 0)} $) y su máximo ({_es(ana.get('maximo') or 0)} $). "
         "El proyecto midió que baten su propio consenso de BENEFICIO el 67,5 % de "
         "las veces; de sus objetivos de PRECIO no ha medido nada."),
        f"Lectura de los analistas: {porque}. Es un dato de terceros y cuenta "
        f"como opinión ajena informada, no como evidencia del sistema.",
        "consenso de Nasdaq",
    )


def _f_riesgo(rie: dict) -> Factor:
    if not rie:
        return Factor("riesgo", "Cuánto se mueve", "sindato", "—",
                      "Menos de 60 sesiones medidas.",
                      "No se puede acotar el recorrido diario.", "serie de precios")
    dia = rie["normal"]
    signo = "contra" if dia >= VOL_ALTA else "neutral" if dia >= VOL_MEDIA else "favor"
    extra = ""
    if rie.get("razon"):
        extra = (f" En sesión de resultados se mueve {_es(rie['razon'], 1)} veces más "
                 f"({_es(rie['resultados'] * 100)} %), medido sobre "
                 f"{rie['n_resultados']} anuncios.")
    return Factor(
        "riesgo", "Cuánto se mueve", signo, f"{_es(dia * 100)} % un día normal",
        (f"Es la mediana del recorrido diario en valor absoluto sobre "
         f"{rie['sesiones']} sesiones." + extra),
        ("Movimiento alto: una posición del mismo tamaño arriesga más aquí."
         if signo == "contra" else
         "Movimiento medio." if signo == "neutral" else
         "Movimiento contenido. Es un filtro de riesgo, no una señal: un valor "
         "tranquilo no es un valor que vaya a subir."),
        "serie de precios",
    )


def _f_insiders(ins: dict, senal: dict | None) -> Factor:
    """La actividad declarada, sin convertirla en ventaja.

    `neutral` y no `favor` a propósito: las compras están declaradas y fechadas
    por su día de PRESENTACIÓN, pero que anticipen algo es E7c, que sigue en
    observación con cero operaciones vencidas. Pintarlo a favor diría que el
    pre-registro ya salió.
    """
    if not ins or not ins.get("compras"):
        return Factor("insiders", "Actividad de insiders", "sindato", "—",
                      "Ninguna compra en mercado abierto declarada desde 2010.",
                      "Este valor no entra por esta vía.", "Form 4 de EDGAR")
    detalle = (f"{ins['distintos']} directivos, {ins['compras']} compras, "
               f"{_corto(ins['importe'] or 0)} USD en total. Última presentación: "
               f"{ins['ultima']}.")
    if senal:
        n_ins = senal.get("n_insiders") or 0
        detalle += (f" Anotada en el registro de E7c el {senal['fecha_senal']} con "
                    f"{n_ins} comprador" + ("es" if n_ins != 1 else "")
                    + (" (en banda de liquidez)" if senal.get("en_banda")
                       else " (fuera de la banda de liquidez)") + ".")
    return Factor(
        "insiders", "Actividad de insiders", "neutral",
        (f"anotada en el registro el {senal['fecha_senal']}" if senal
         else f"última compra declarada el {ins['ultima']}"),
        detalle,
        "Dato firme, ventaja no demostrada: el pre-registro de E7c sigue abierto "
        "y ninguna operación ha vencido todavía. No cuenta como señal de compra.",
        "Form 4 de EDGAR",
    )


def _f_hechos(hechos: list[dict]) -> Factor:
    if not hechos:
        return Factor("hechos", "Hechos declarados", "sindato", "—",
                      "Ningún 8-K registrado para este valor.",
                      "Sin hechos relevantes que leer.", "8-K de EDGAR")
    malos = [h for h in hechos if h["mal"]]
    if malos:
        return Factor(
            "hechos", "Hechos declarados", "contra",
            f"{len(malos)} de {len(hechos)} con item adverso",
            ("Adverso lo define el propio formulario —concurso, exclusión de "
             "cotización, deterioro, cuentas no fiables—, no una interpretación "
             "nuestra. El más reciente es del " + malos[0]["f"] + ": "
             + ", ".join(i["t"] for i in malos[0]["i"]) + "."),
            "Es el único bloque de este informe con dirección declarada por la "
            "fuente, y va en contra.",
            "8-K de EDGAR",
        )
    return Factor(
        "hechos", "Hechos declarados", "neutral",
        f"{len(hechos)} hechos, ninguno adverso",
        ("El más reciente es del " + hechos[0]["f"] + ": "
         + ", ".join(i["t"] for i in hechos[0]["i"]) + "."),
        "Un 8-K sin item adverso no tiene dirección: dice que pasó algo material, "
        "no si fue bueno.",
        "8-K de EDGAR",
    )


def _f_horizonte(hz: list[dict], plazo: str = "1 mes") -> Factor:
    """A qué se ha estado expuesto quien tuvo este valor ese plazo.

    Es el bloque «A qué te expones» del cuaderno, que hasta ahora no salía por
    ningún canal. Va SIN signo y no es un descuido: son ventanas solapadas de
    frecuencia histórica, no un pronóstico. Que algo subiera el 58 % de las
    veces no dice que vaya a subir; dice a qué estuvo expuesto quien lo tuvo.

    Se enseña el plazo de un mes porque es el horizonte con el que se juzgan
    los avisos: enseñar otro obligaría a traducir mentalmente.
    """
    h = next((x for x in hz if x["et"] == plazo), None) or (hz[0] if hz else None)
    if not h:
        return Factor("horizonte", "A qué te expones", "sindato", "—",
                      "Menos de 300 cierres en la base para este valor.",
                      "No se puede describir el recorrido de sus ventanas.",
                      "serie de precios")
    return Factor(
        "horizonte", "A qué te expones", "neutral",
        (f"{h['arriba'] * 100:.0f} % de las ventanas de {h['et']} acabaron arriba"),
        (f"En {num_es(h['ventanas'])} ventanas solapadas de {h['et']}, la mediana "
         f"se movió {_pc(h['med'])} y ocho de cada diez quedaron entre "
         f"{_pc(h['p10'])} y {_pc(h['p90'])}. La peor fue {_pc(h['peor'])}."),
        "Es frecuencia histórica, no pronóstico: acota a qué te expones ese "
        "plazo, no hacia dónde va a ir.",
        "serie de precios",
    )


def _f_encaje(enc: dict, ana: dict, plazo: str = "1 mes") -> Factor:
    """Cuánto pide el consenso, medido contra lo que ESE valor ha dado.

    Es la aportación del proyecto sobre el dato de los analistas y no estaba
    llegando al informe: la fuente publica el objetivo, y esto dice si pedirle
    eso a este valor es rutina o es raro. No convierte el objetivo en
    pronóstico ni lo contradice.
    """
    if not enc or not enc.get("por"):
        return Factor("encaje", "Qué pide el consenso", "sindato", "—",
                      "Sin objetivo de consenso o sin serie suficiente para "
                      "situarlo.",
                      "No se puede medir cuánto pide contra lo que este valor da.",
                      "consenso de Nasdaq · serie de precios")
    pide = enc["pide"]
    if pide <= 0:
        # Con el consenso POR DEBAJO del precio, `llega` se dispara y graduarlo
        # diría «rutinario» de un valor que los analistas ven caer. Es otra
        # cosa y se dice aparte.
        return Factor(
            "encaje", "Qué pide el consenso", "contra",
            f"el consenso está un {_es(abs(pide) * 100, 0)} % por debajo del precio",
            "Los analistas sitúan su objetivo por debajo de donde cotiza hoy.",
            "No hay recorrido pedido que medir: piden menos de lo que vale.",
            "consenso de Nasdaq · serie de precios")

    p = enc["por"].get(plazo) or next(iter(enc["por"].values()))
    q = p["llega"]
    # La misma escala que usa el cuaderno, para que no digan cosas distintas.
    grado = ("rutinario" if q >= 0.50 else "alcanzable" if q >= 0.30
             else "raro" if q >= 0.10 else "excepcional")
    return Factor(
        "encaje", "Qué pide el consenso", "neutral",
        f"+{_es(pide * 100, 0)} % · {grado} para este valor",
        (f"El objetivo de {_es(ana['objetivo'])} $ implica subir un "
         f"{_es(pide * 100, 0)} %. Este valor dio esa subida en el "
         f"{q * 100:.0f} % de sus ventanas de {plazo} "
         f"({num_es(p['w'])} medidas)."),
        ("Pedirle eso a este valor entra dentro de lo que suele hacer."
         if q >= 0.30 else
         "Pedirle eso a este valor es poco frecuente en su propia historia."),
        "consenso de Nasdaq · serie de precios",
    )


def num_es(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _evento_proximo(store: Store, act: str, hoy: date, rie: dict) -> dict | None:
    """El próximo anuncio de resultados y cuánto mueve a ESTE valor.

    La magnitud sale de su propio historial si tiene bastante; si no, de la
    referencia general del proyecto, y se dice cuál de las dos es.
    """
    fila = store.con.execute(
        "SELECT fecha, momento, eps_previsto FROM calendario "
        "WHERE activo = ? AND fecha >= ? AND fecha <= ? ORDER BY fecha LIMIT 1",
        [act, hoy, hoy + timedelta(days=DIAS_EVENTO)],
    ).fetchone()
    if not fila:
        return None
    f, momento, eps = fila
    propio = bool(rie and rie.get("resultados")
                  and (rie.get("n_resultados") or 0) >= MUESTRA_MINIMA_ACTIVO)
    tipico = rie["resultados"] * 100 if propio else REFERENCIA_RESULTADOS
    dias = (f - hoy).days
    cuando = {0: "hoy", 1: "mañana"}.get(dias, f"en {dias} días")
    return {
        "fecha": str(f), "dias": dias, "cuando": cuando,
        "momento": {"tras_cierre": "tras el cierre",
                    "antes_apertura": "antes de abrir"}.get(momento, "hora sin confirmar"),
        "tipico": tipico, "propio": propio, "eps_previsto": eps,
        "capturable": tipico * (1 - FRACCION_HUECO),
    }


def _exposicion(evento: dict | None, factor_riesgo: Factor,
                hechos_malos: bool) -> tuple[str, str]:
    """El único estado que este sistema puede emitir, y es sobre exposición.

    Copia deliberada del criterio de `alerta_volatilidad`: es lo único medido, y
    dos criterios distintos para la misma pregunta acabarían diciendo cosas
    distintas según por dónde llegara el aviso.
    """
    if evento:
        t = evento["tipico"]
        if t >= 7:
            return ("EVITAR",
                    f"Movimiento típico de ±{_es(t, 1)} % y dirección desconocida: "
                    f"abrir aquí es apostar a cara o cruz, y el "
                    f"{FRACCION_HUECO:.0%} ocurre en el hueco de apertura, donde "
                    f"un stop no protege.")
        if t >= 4:
            return ("REDUCIR",
                    f"Movimiento típico de ±{_es(t, 1)} % en el anuncio: si hay "
                    f"posición, bajar tamaño antes del evento.")
        return ("VIGILAR",
                f"Hay anuncio {evento['cuando']} pero el movimiento típico de este "
                f"valor (±{_es(t, 1)} %) está dentro de lo normal.")
    if hechos_malos:
        return ("VIGILAR",
                "Sin evento de resultados cerca, pero hay un 8-K con item adverso "
                "reciente.")
    if factor_riesgo.signo == "contra":
        return ("VIGILAR",
                f"Sin evento cerca. El aviso es de tamaño: {factor_riesgo.dato}.")
    return ("SIN AVISO", "Ni evento de resultados próximo ni hecho adverso reciente.")


# --------------------------------------------------------------------------
# Análisis completo
# --------------------------------------------------------------------------


#: Sesiones que entran en el grafico del correo: un año de mercado.
VENTANA_GRAFICO = 252

# --------------------------------------------------------------------------
# Perpetuos
#
# Un perpetuo no es una acción recortada: no hay Form 4, ni 8-K, ni consenso de
# analistas, ni XBRL, ni calendario de resultados. Seis de los nueve bloques no
# existen, y eso NO se rellena con nada.
#
# Lo que queda es el precio y su tamaño. Y da la casualidad de que el tamaño es
# lo único que este proyecto ha validado como útil en cualquiera de sus dos
# ramas, así que el informe de un perpetuo es honesto justo en la medida en que
# se limita a eso: cuánto se mueve, cuánto se ha movido, y cuánto se negocia.
#
# **La rama cripto está cerrada y sus dieciséis hipótesis refutadas.** Esto no
# la reabre: es vigilancia de volatilidad sobre contratos que se siguen
# cotizando, no la búsqueda de una ventaja que ya se descartó. El informe lo
# dice en su propia cara, porque si no lo dijera parecería lo contrario.
# --------------------------------------------------------------------------

#: Corte de movimiento diario mediano para describir el régimen de un perpetuo.
#: Son más altos que los de una acción porque el activo es otro: 1,3 % diario es
#: BTC en calma y sería una acción movida.
VOL_CRIPTO_ALTA = 0.030
VOL_CRIPTO_MEDIA = 0.018


def _movimientos(serie: list[tuple[date, float]]) -> list[float]:
    """Variación diaria en valor absoluto, cierre contra cierre."""
    return [abs(serie[i][1] / serie[i - 1][1] - 1)
            for i in range(1, len(serie)) if serie[i - 1][1]]


def _percentil(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def _f_liquidez(vol: float | None, velas: int) -> Factor:
    """Cuánto se negocia. Sin signo: es contexto de ejecución, no una señal."""
    if not vol:
        return Factor("liquidez", "Volumen", "sindato", "—",
                      "Sin volumen medido para este contrato.",
                      "No se puede situar su liquidez.", "dumps de Binance")
    return Factor(
        "liquidez", "Volumen", "neutral", f"{_corto(vol)} USD/día (mediana)",
        f"Medido sobre {num_es(velas)} velas diarias.",
        "Contexto de ejecución: cuanto más fino, más cuesta entrar y salir. No "
        "dice nada de hacia dónde va el precio.",
        "dumps de Binance")


def _f_riesgo_cripto(serie: list[tuple[date, float]]) -> Factor:
    """El bloque que sostiene el informe entero de un perpetuo.

    Va con signo —contenido es favorable, alto es desfavorable— porque es un
    filtro de riesgo y ahí «favorable» significa algo concreto: la misma
    posición arriesga menos. No significa que vaya a subir.
    """
    movs = _movimientos(serie)
    if len(movs) < 60:
        return Factor("riesgo", "Cuánto se mueve", "sindato", "—",
                      "Menos de 60 velas diarias.",
                      "No se puede acotar el recorrido diario.", "serie de precios")
    med = _percentil(movs, 0.50)
    p90 = _percentil(movs, 0.90)
    signo = ("contra" if med >= VOL_CRIPTO_ALTA
             else "neutral" if med >= VOL_CRIPTO_MEDIA else "favor")
    return Factor(
        "riesgo", "Cuánto se mueve", signo,
        f"{_es(med * 100)} % un día normal · {_es(p90 * 100)} % uno de cada diez",
        (f"Mediana y percentil 90 del recorrido diario en valor absoluto, sobre "
         f"{num_es(len(movs))} días. Un perpetuo cotiza los siete días de la "
         f"semana: aquí no hay huecos de fin de semana que amortigüen."),
        ("Movimiento alto: la misma posición arriesga más aquí."
         if signo == "contra" else
         "Movimiento medio para un perpetuo." if signo == "neutral" else
         "Movimiento contenido para un perpetuo. Es un filtro de riesgo, no una "
         "señal."),
        "serie de precios")


#: El mes de un perpetuo son 30 días naturales y no 21 sesiones: cotiza los
#: siete días de la semana, así que contar «sesiones de mercado» mediría una
#: ventana un 40 % más larga sin que nadie lo hubiera decidido.
HORIZONTE_CRIPTO = 30


def analizar_cripto(store: Store, act: str, hoy: date | None = None,
                    previo: dict | None = None,
                    motivo_inclusion: list[str] | None = None) -> Informe:
    """Informe de un perpetuo: lo que hay, y nada donde no hay nada."""
    hoy = hoy or date.today()
    serie = _serie_mercado(store, act, "crypto_perp")
    if len(serie) < 60:
        raise ValueError(f"{act}: menos de 60 velas diarias en la base")

    fila = store.con.execute(
        "SELECT count(*), median(close * volume) FROM ohlcv WHERE activo = ? "
        "AND mercado = 'crypto_perp' AND timeframe = '1d'", [act]).fetchone()
    hz = F.por_horizonte(store, act, mercado="crypto_perp")

    factores = [
        _f_momentum(serie, "cripto"),
        _f_horizonte(hz),
        _f_riesgo_cripto(serie),
        _f_liquidez(fila[1], fila[0] or 0),
    ]
    por = {f.clave: f for f in factores}

    movs = _movimientos(serie)
    p90 = _percentil(movs, 0.90)
    estado = {"contra": "MOVIMIENTO ALTO", "neutral": "MOVIMIENTO MEDIO",
              "favor": "MOVIMIENTO CONTENIDO"}.get(por["riesgo"].signo,
                                                   "SIN MEDIR")
    motivo = (
        f"Su peor día de cada diez llega al {_es((p90 or 0) * 100)} %. Es lo único "
        f"que este informe afirma: el tamaño. No hay dirección, y la rama cripto "
        f"de este proyecto está cerrada con sus dieciséis hipótesis refutadas."
    )

    inf = Informe(
        activo=act, nombre=act, generado=datetime.now(),
        precio=serie[-1][1], exposicion=estado, exposicion_motivo=motivo,
        factores=factores, motivo_inclusion=motivo_inclusion or [],
        mercado="cripto",
    )
    inf.serie, inf.marcas = ([c for _, c in serie[-VENTANA_GRAFICO:]], [])
    inf.faltan = [f.titulo for f in factores if f.signo == "sindato"]
    inf.vigilar = [
        f"si su recorrido diario se aparta del {_es((p90 or 0) * 100)} % que "
        f"marca su propio percentil 90"]
    inf.riesgos = _riesgos_cripto(inf, por, p90)
    inf.cambios, inf.exposicion_previa = _cambios(inf, por, previo)
    return inf


def _riesgos_cripto(inf: Informe, por: dict[str, Factor],
                    p90: float | None) -> list[str]:
    L = [f"Un día de cada diez se mueve {_es((p90 or 0) * 100)} % o más, y cotiza "
         f"sin cierre: no hay fin de semana que corte una caída."]
    if por["riesgo"].signo == "contra":
        L.append(f"Régimen de volatilidad alto: {por['riesgo'].dato}.")
    if por["liquidez"].signo == "sindato":
        L.append("Sin volumen medido: no se sabe cuánto cuesta entrar o salir.")
    L.append("Un perpetuo paga o cobra funding cada ocho horas, y este informe "
             "no lo incluye: el coste de mantenerlo abierto no está aquí.")
    if inf.faltan:
        L.append("No existe para un perpetuo: " + ", ".join(inf.faltan).lower()
                 + ". No es que falte el dato; es que no hay fuente.")
    return L


def _serie_mercado(store: Store, act: str, mercado: str,
                   n: int = 900) -> list[tuple[date, float]]:
    filas = store.con.execute(
        "SELECT ts::DATE, close FROM ohlcv WHERE activo = ? AND mercado = ? "
        "AND timeframe = '1d' AND close > 0 ORDER BY ts DESC LIMIT ?",
        [act, mercado, n]).fetchall()
    return [(f, c) for f, c in reversed(filas)]


def _ventana_grafico(serie: list[tuple[date, float]],
                     ins: dict) -> tuple[list[float], list[int]]:
    """El ultimo año de cierres, y en que posiciones hubo compras declaradas.

    Las marcas se situan por FECHA DE PRESENTACION, que es la columna con la
    que trabaja el resto del proyecto: marcar el dia de la compra pondria el
    punto hasta dos sesiones antes de que el dato existiera.
    """
    ventana = serie[-VENTANA_GRAFICO:]
    if len(ventana) < 2:
        return ([], [])
    indice = {f: i for i, (f, _) in enumerate(ventana)}
    marcas = []
    for x in (ins or {}).get("recientes") or []:
        try:
            i = indice.get(date.fromisoformat(x["fecha"]))
        except (ValueError, TypeError):
            continue
        if i is not None:
            marcas.append(i)
    return ([c for _, c in ventana], sorted(set(marcas)))


def analizar(store: Store, act: str, hoy: date | None = None,
             previo: dict | None = None, nombre: str = "",
             senal: dict | None = None,
             motivo_inclusion: list[str] | None = None) -> Informe:
    """Reúne todo lo que la base sabe de un valor y lo pone en orden de lectura."""
    hoy = hoy or date.today()
    completa = F.completa(store, act, hoy)
    ins, res = completa["insiders"], completa["resultados"]
    fun, rie = completa["fundamentales"], completa["riesgo"]
    ana, hechos = completa["analistas"], completa["hechos"]

    serie = _serie(store, act)
    precio = serie[-1][1] if serie else None

    recientes = [h for h in hechos
                 if h["f"] >= str(hoy - timedelta(days=DIAS_HECHO))]

    # Los dos bloques que el cuaderno ya enseñaba y no salían por ningún canal.
    hz = F.por_horizonte(store, act)
    enc = F.encaje(store, act, (ana or {}).get("objetivo"))

    factores = [
        _f_momentum(serie),
        _f_horizonte(hz),
        _f_resultados(res),
        _f_analistas(ana, precio),
        _f_encaje(enc, ana or {}),
        _f_fundamentales(fun),
        _f_riesgo(rie),
        _f_insiders(ins, senal),
        _f_hechos(recientes),
    ]
    por_clave = {f.clave: f for f in factores}

    evento = _evento_proximo(store, act, hoy, rie)
    malos = any(h["mal"] for h in recientes)
    exposicion, motivo = _exposicion(evento, por_clave["riesgo"], malos)

    inf = Informe(
        activo=act, nombre=nombre or act, generado=datetime.now(),
        precio=precio, objetivo=(ana or {}).get("objetivo"),
        veredicto=veredicto_analistas(ana),
        exposicion=exposicion, exposicion_motivo=motivo,
        evento=evento, factores=factores, hechos=recientes,
        motivo_inclusion=motivo_inclusion or [],
    )
    inf.serie, inf.marcas = _ventana_grafico(serie, ins)
    inf.faltan = [f.titulo for f in factores if f.signo == "sindato"]
    inf.vigilar = _vigilar(inf, res, senal, hoy)
    inf.riesgos = _riesgos(inf, por_clave, recientes, res, hoy)
    inf.cambios, inf.exposicion_previa = _cambios(inf, por_clave, previo)
    return inf


def _vigilar(inf: Informe, res: dict, senal: dict | None, hoy: date) -> list[str]:
    """Qué mirar la próxima vez, con fecha cuando la hay.

    Solo entran cosas con fecha o con una condición comprobable. «Vigilar el
    sentimiento del mercado» no es vigilar nada.
    """
    L: list[str] = []
    prox = (res or {}).get("proximo")
    if prox and (not inf.evento or prox != inf.evento["fecha"]):
        dias = (date.fromisoformat(prox) - hoy).days
        L.append(f"próximo anuncio de resultados el {prox} "
                 f"({dias} días)")
    if senal:
        L.append(f"la señal de E7c del {senal['fecha_senal']} no tiene resultado "
                 f"hasta que venza su plazo: el registro no se cierra antes")
    for h in inf.hechos[:1]:
        if h["mal"]:
            L.append(f"si al 8-K adverso del {h['f']} le siguen otros")
    if inf.faltan:
        L.append("faltan datos de " + ", ".join(x.lower() for x in inf.faltan)
                 + ": podrían cambiar la lectura sin que nada haya cambiado")
    return L


def _riesgos(inf: Informe, por: dict[str, Factor], hechos: list[dict],
             res: dict, hoy: date) -> list[str]:
    """Siempre hay sección de riesgos, y solo con lo que esté medido."""
    L: list[str] = []
    if inf.evento:
        L.append(f"Anuncio de resultados el {inf.evento['fecha']} "
                 f"({inf.evento['momento']}): movimiento típico ±"
                 f"{_es(inf.evento['tipico'], 1)} %"
                 + ("" if inf.evento["propio"]
                    else ", tomado de la referencia general porque este valor no "
                         "tiene suficientes anuncios propios")
                 + f". Solo ~{_es(inf.evento['capturable'], 1)} % queda después del hueco "
                   f"de apertura.")
    for h in hechos:
        if h["mal"]:
            L.append(f"8-K adverso del {h['f']}: "
                     + ", ".join(i["t"] for i in h["i"]) + ".")
    if por["riesgo"].signo == "contra":
        L.append(f"Volatilidad diaria alta: {por['riesgo'].dato}.")
    if por["fundamentales"].signo == "contra":
        L.append(f"Ingresos a la baja: {por['fundamentales'].dato}.")
    if por["resultados"].signo == "contra":
        L.append(f"Incumple su consenso más de lo que le toca: {por['resultados'].dato}.")
    if por["analistas"].signo == "contra":
        L.append(f"El consenso de analistas no acompaña: {por['analistas'].dato}.")
    if inf.contradictorio:
        L.append("Los bloques se contradicen entre sí: "
                 + ", ".join(f.titulo.lower() for f in inf.a_favor) + " a favor, "
                 + ", ".join(f.titulo.lower() for f in inf.en_contra) + " en contra.")
    if inf.faltan:
        L.append("No se ha podido mirar: " + ", ".join(inf.faltan).lower() + ".")
    if not L:
        L.append(SIN_RIESGO_MEDIBLE)
    return L


def _cambios(inf: Informe, por: dict[str, Factor],
             previo: dict | None) -> tuple[list[str], str | None]:
    """Qué es distinto respecto al informe anterior de este mismo valor."""
    if not previo:
        return ([], None)
    L: list[str] = []
    antes_px = previo.get("precio")
    if antes_px and inf.precio:
        d = inf.precio / antes_px - 1
        if abs(d) >= 0.005:
            L.append(f"el precio va {_pc(d)} desde el informe anterior "
                     f"({_es(antes_px)} → {_es(inf.precio)} $)")
    antes_exp = previo.get("exposicion")
    if antes_exp and antes_exp != inf.exposicion:
        L.append(f"el aviso de exposición pasa de {antes_exp} a {inf.exposicion}")
    for clave, etiqueta in (("resultados", "resultados"), ("analistas", "analistas"),
                            ("fundamentales", "ingresos"), ("riesgo", "volatilidad"),
                            ("hechos", "hechos declarados")):
        antes = (previo.get("signos") or {}).get(clave)
        factor = por.get(clave)
        ahora = factor.signo if factor else None
        if antes and ahora and antes != ahora:
            L.append(f"{etiqueta}: {SIGNOS[antes].lower()} → {SIGNOS[ahora].lower()}")
    vistos = set(previo.get("hechos") or [])
    nuevos = [h for h in inf.hechos if h["f"] not in vistos]
    if nuevos and vistos:
        L.append(f"{len(nuevos)} hecho(s) declarado(s) nuevo(s), el último del "
                 f"{nuevos[0]['f']}")
    antes_obj = previo.get("objetivo")
    if antes_obj and inf.objetivo and abs(inf.objetivo / antes_obj - 1) >= 0.01:
        L.append(f"el objetivo de consenso se mueve de {_es(antes_obj)} a "
                 f"{_es(inf.objetivo)} $")
    return (L, antes_exp)


#: La escala de seis niveles, y de dónde sale cada uno.
#:
#: **No es una escala nuestra.** Es la que publica la fuente —`Strong Buy`,
#: `Buy`, `Hold`, `Underperform`, `Sell`— traducida, y la etiqueta original
#: viaja siempre al lado para que la traducción sea reversible. El único nivel
#: que no está en la fuente es «compra moderada», que separa un `Buy` con dos
#: tercios largos de casas detrás de un `Buy` que apenas pasa de la mitad: esa
#: distinción sale del reparto de casas, que también es dato publicado.
ESCALA = {
    "Strong Buy": "COMPRA FUERTE",
    "Buy": "COMPRA",
    "Hold": "MANTENER",
    "Underperform": "VENTA",
    "Sell": "VENTA INMEDIATA",
}

#: Fracción de casas que dicen comprar por debajo de la cual un `Buy` se lee
#: como compra moderada.
UMBRAL_COMPRA_FIRME = 2 / 3


def veredicto_analistas(ana: dict | None) -> dict | None:
    """La conclusión en escala de compra o venta, FIRMADA POR LOS ANALISTAS.

    Esta es la única conclusión direccional que este informe puede llevar, y el
    motivo no es de estilo. El proyecto no tiene precio objetivo, E7c sigue en
    observación con cero operaciones vencidas, el momentum no tiene hipótesis
    validada en acciones y la dirección en resultados salió simétrica sobre
    10.975 anuncios. De ahí no sale un «compra fuerte» nuestro.

    Lo que sí existe es un consenso publicado, con nombre, número de casas y su
    propio reparto. Decir que LOS ANALISTAS dicen comprar es reportar un hecho
    comprobable. Decir que MoonRocket dice comprar sería fabricar una dirección.
    La diferencia entre esas dos frases es todo el proyecto.

    Réplica de `veredictoAnalistas()` en `dashboard/plantilla.html`. Si
    divergieran, el mismo valor daría dos veredictos según se mirara en el
    correo o en la ficha.
    """
    if not ana:
        return None
    n = ana.get("n") or 0
    compra, mantener, venta = (ana.get("compra") or 0, ana.get("mantener") or 0,
                               ana.get("venta") or 0)
    publicado = (ana.get("consenso") or "").strip()

    if publicado in ESCALA:
        etiqueta = ESCALA[publicado]
        # Un `Buy` con la mitad justa de las casas y otro con nueve de diez no
        # son la misma señal, y la fuente los llama igual. El matiz sale del
        # reparto, no de una opinión nuestra.
        if publicado == "Buy" and n and compra / n < UMBRAL_COMPRA_FIRME:
            etiqueta = "COMPRA MODERADA"
        origen = f"publicado por la fuente como «{publicado}»"
    elif n:
        etiqueta = _de_reparto(compra, venta, n)
        origen = ("la fuente no publica etiqueta de consenso; sale del reparto "
                  "de casas")
    else:
        return None

    return {
        "etiqueta": etiqueta, "origen": origen, "publicado": publicado or None,
        "compra": compra, "mantener": mantener, "venta": venta, "n": n,
        "casas": ana.get("casas"), "objetivo": ana.get("objetivo"),
        "clase": ("up" if etiqueta.startswith("COMPRA")
                  else "dn" if etiqueta.startswith("VENTA") else "med"),
    }


def _de_reparto(compra: int, venta: int, n: int) -> str:
    if venta / n >= 0.5:
        return "VENTA INMEDIATA"
    if venta > compra:
        return "VENTA"
    if compra / n >= 0.8:
        return "COMPRA FUERTE"
    if compra / n >= UMBRAL_COMPRA_FIRME:
        return "COMPRA"
    if compra > venta and compra / n >= 0.4:
        return "COMPRA MODERADA"
    return "MANTENER"


#: El consenso de analistas NO entra en el recuento propio: ya es el titular de
#: la conclusión. Contarlo en las dos mitades haría que un «compra» de los
#: analistas sumara un voto a favor de MoonRocket, que es exactamente la mezcla
#: que las dos cajas existen para evitar.
FUERA_DEL_RECUENTO = {"analistas"}


def recuento_propio(inf: Informe) -> dict:
    """Lo que dicen los bloques de MoonRocket, SIN traducirlo a comprar o vender.

    Se queda en el recuento a propósito. Cuatro bloques a favor no son un
    «compra moderada»: de los que quedan, uno mide una hipótesis sin validar y
    dos no tienen dirección medida, y sumarlos daría una etiqueta con la
    autoridad de algo que nadie ha demostrado.
    """
    cuenta = {"favor": 0, "contra": 0, "neutral": 0, "sindato": 0}
    for f in inf.factores:
        if f.clave in FUERA_DEL_RECUENTO:
            continue
        cuenta[f.signo] = cuenta.get(f.signo, 0) + 1
    return cuenta


def _linea_recuento(inf: Informe) -> str:
    c = recuento_propio(inf)
    return (f"{c['favor']} bloque(s) a favor · {c['contra']} en contra · "
            f"{c['neutral']} sin dirección · {c['sindato']} sin datos · "
            f"exposición {inf.exposicion}"
            + ("" if inf.mercado == "cripto"
               else " (sin contar el consenso, que es el titular de arriba)"))


def instantanea(inf: Informe) -> dict:
    """Lo mínimo para que el informe de mañana pueda decir qué ha cambiado."""
    return {
        "fecha": inf.generado.date().isoformat(),
        "precio": inf.precio,
        "exposicion": inf.exposicion,
        "signos": {f.clave: f.signo for f in inf.factores},
        "hechos": [h["f"] for h in inf.hechos],
        "objetivo": inf.objetivo,
    }


def leer_estado(ruta: Path) -> dict:
    """Caché reescribible. Nada que ver con el registro de solo anexado."""
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def escribir_estado(ruta: Path, estado: dict) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------
# Presentación
# --------------------------------------------------------------------------

AVISO_COMUN = (
    "El estado se refiere a la EXPOSICIÓN, no a la dirección. Este sistema no "
    "emite señales de compra ni de venta y no tiene precio objetivo propio: no "
    "hay ninguna estrategia validada detrás de este informe."
)


def _e(s: str) -> str:
    return _html.escape(str(s), quote=False)


# ---- Telegram -------------------------------------------------------------

def telegram(inf: Informe) -> list[str]:
    """El informe partido en mensajes que se leen en un móvil.

    Se parte por SECCIONES y no por caracteres: cortar a mitad de una lista de
    riesgos deja el informe diciendo media cosa. Cada mensaje de continuación
    repite el símbolo, porque en el hilo de un móvil el anterior ya no se ve.
    """
    bloques: list[str] = []

    cab = [f"<b>{_e(inf.activo)}</b> · {_e(inf.nombre)}",
           f"<i>{inf.generado:%d/%m/%Y %H:%M}</i>"]
    if inf.precio:
        cab.append(f"Precio: <b>{_es(inf.precio)} $</b>")
    marca = {"EVITAR": "🚫", "REDUCIR": "⚠️", "VIGILAR": "👁", "SIN AVISO": "✅"}
    linea = f"{marca.get(inf.exposicion, '•')} Exposición: <b>{inf.exposicion}</b>"
    if inf.exposicion_previa and inf.exposicion_previa != inf.exposicion:
        linea += f"  (antes {_e(inf.exposicion_previa)})"
    cab.append(linea)
    cab.append(_e(inf.exposicion_motivo))
    if inf.motivo_inclusion:
        cab.append(f"<i>En el informe por: {_e(', '.join(inf.motivo_inclusion))}</i>")
    bloques.append("\n".join(cab))

    bloques.append("<b>▸ EN CORTO</b>\n"
                   + "\n".join("• " + _e(x) for x in inf.resumen))

    tesis = ["<b>▸ POR QUÉ</b>"]
    for f in inf.factores:
        if f.signo == "sindato":
            continue
        tesis.append(f"\n{ICONO[f.signo]} <b>{_e(f.titulo)}</b> — {_e(f.dato)}")
        tesis.append(f"   {_e(f.interpretacion)}")
        tesis.append(f"   <i>{_e(f.impacto)}</i>")
    bloques.append("\n".join(tesis))

    if inf.mercado == "cripto":
        pass        # un perpetuo no tiene SEC: la seccion no existe
    elif inf.hechos:
        h = ["<b>▸ HECHOS DECLARADOS</b>",
             "<i>8-K de la SEC: hechos, no titulares.</i>"]
        for x in inf.hechos[:5]:
            h.append(f"• <b>{_e(x['f'])}</b> — "
                     + _e(", ".join(i["t"] for i in x["i"]))
                     + (" ⚠️ <i>adverso por definición del formulario</i>" if x["mal"] else ""))
        bloques.append("\n".join(h))
    else:
        bloques.append("<b>▸ HECHOS DECLARADOS</b>\nNinguno en los últimos "
                       f"{DIAS_HECHO} días. El sistema no usa prensa: su única "
                       "noticia verificable es el 8-K.")

    bloques.append("<b>▸ RIESGOS</b>\n"
                   + "\n".join("• " + _e(x) for x in inf.riesgos))

    if inf.cambios:
        bloques.append("<b>▸ QUÉ HA CAMBIADO</b>\n"
                       + "\n".join("• " + _e(x) for x in inf.cambios))
    else:
        bloques.append("<b>▸ QUÉ HA CAMBIADO</b>\nNada medible desde el informe "
                       "anterior.")

    fin = ["<b>▸ CONCLUSIÓN</b>"]
    v = inf.veredicto
    if v:
        marca = {"up": "🟢", "dn": "🔴", "med": "🟡"}[v["clase"]]
        fin.append(f"{marca} <b>Consenso de analistas: {_e(v['etiqueta'])}</b>")
        fin.append(_e(_detalle_veredicto(v)))
        fin.append("<i>La firman los analistas, no MoonRocket.</i>")
    else:
        fin.append("<i>" + _e(_sin_veredicto(inf)) + "</i>")
    fin.append(f"\n<b>Lectura propia de MoonRocket</b> <i>(sin dirección)</i>\n"
               f"{_e(_linea_recuento(inf))}")
    fin.append(f"\n{_e(inf.conclusion)}")
    fin.append(f"\n<b>Evidencia:</b> {_e(inf.evidencia)}\n<i>{_e(AVISO_COMUN)}</i>")
    bloques.append("\n".join(fin))

    return _empaquetar(bloques, inf.activo)


def _empaquetar(bloques: list[str], activo: str, tope: int = 3900) -> list[str]:
    """Agrupa bloques enteros en mensajes que quepan, sin partir ninguno.

    Un bloque que por sí solo no quepa se parte por líneas, que es el último
    recurso antes de perder contenido. Lo que no se hace nunca es tirar el
    final: el informe termina en la conclusión y en el aviso, y esos son
    precisamente los que el recorte antiguo se comía.
    """
    trozos: list[str] = []
    actual = ""
    for b in bloques:
        for pieza in _partir(b, tope):
            if not actual:
                actual = pieza
            elif len(actual) + 2 + len(pieza) <= tope:
                actual += "\n\n" + pieza
            else:
                trozos.append(actual)
                actual = pieza
    if actual:
        trozos.append(actual)

    if len(trozos) > 1:
        trozos = [t if i == 0
                  else f"<b>{_e(activo)}</b> <i>({i + 1}/{len(trozos)})</i>\n\n{t}"
                  for i, t in enumerate(trozos)]
    return trozos


def _partir(bloque: str, tope: int) -> list[str]:
    if len(bloque) <= tope:
        return [bloque]
    fuera, actual = [], ""
    for linea in bloque.split("\n"):
        if len(actual) + 1 + len(linea) > tope:
            fuera.append(actual)
            actual = linea
        else:
            actual = f"{actual}\n{linea}" if actual else linea
    if actual:
        fuera.append(actual)
    return fuera


# ---- texto plano ----------------------------------------------------------

def texto(informes: list[Informe], hoy: date) -> str:
    L = ["=" * 64,
         f"  MOONROCKET · INFORME DE ANALISIS · {hoy}",
         f"  {len(informes)} valor(es)",
         "=" * 64, "", AVISO_COMUN, ""]
    for inf in informes:
        L += ["-" * 64,
              f"{inf.activo} · {inf.nombre}"
              + (f" · {_es(inf.precio)} $" if inf.precio else ""),
              f"Exposicion: {inf.exposicion}"
              + (f" (antes {inf.exposicion_previa})"
                 if inf.exposicion_previa and inf.exposicion_previa != inf.exposicion
                 else ""),
              f"  {inf.exposicion_motivo}", ""]
        if inf.motivo_inclusion:
            L.append("En el informe por: " + ", ".join(inf.motivo_inclusion))
            L.append("")
        L.append("EN CORTO")
        L += ["  - " + x for x in inf.resumen]
        L.append("")
        L.append("POR QUE")
        for f in inf.factores:
            if f.signo == "sindato":
                continue
            L += [f"  [{SIGNOS[f.signo]}] {f.titulo}: {f.dato}   ({f.fuente})",
                  f"      {f.interpretacion}",
                  f"      -> {f.impacto}"]
        L.append("")
        # Un perpetuo no tiene SEC: la seccion no existe, y decir «ninguno»
        # insinuaria que podria haberlos.
        if inf.mercado != "cripto":
            L.append("HECHOS DECLARADOS (8-K)")
            if inf.hechos:
                for x in inf.hechos[:6]:
                    L.append(f"  - {x['f']}: " + ", ".join(i["t"] for i in x["i"])
                             + ("  [adverso]" if x["mal"] else ""))
            else:
                L.append(f"  - Ninguno en los ultimos {DIAS_HECHO} dias.")
            L.append("")
        L.append("RIESGOS")
        L += ["  - " + x for x in inf.riesgos]
        L.append("")
        if inf.vigilar:
            L.append("A VIGILAR")
            L += ["  - " + x for x in inf.vigilar]
            L.append("")
        L.append("QUE HA CAMBIADO")
        L += (["  - " + x for x in inf.cambios] if inf.cambios
              else ["  - Nada medible desde el informe anterior."])
        L.append("")
        L.append("CONCLUSION")
        if inf.veredicto:
            L += [f"  Consenso de analistas: {inf.veredicto['etiqueta']}",
                  "    " + _detalle_veredicto(inf.veredicto),
                  "    La firman los analistas, no MoonRocket."]
        else:
            L.append("  " + _sin_veredicto(inf))
        L += ["", "  Lectura propia de MoonRocket (sin direccion):",
              "    " + _linea_recuento(inf), "",
              "  " + inf.conclusion, "",
              "EVIDENCIA", "  " + inf.evidencia, ""]
    return "\n".join(L)


# ---- correo ---------------------------------------------------------------

#: Los estilos van EN LÍNEA. Gmail borra la etiqueta <style> del cuerpo en su
#: cliente web, así que una hoja de estilos aquí sería un informe sin formato
#: justo en el cliente para el que se escribe.
_COL = {"favor": "#1a7f4b", "contra": "#b3261e",
        "neutral": "#6b6b76", "sindato": "#9aa0a6"}
_FONDO = {"EVITAR": "#fdecea", "REDUCIR": "#fff4e5",
          "VIGILAR": "#eef4fb", "SIN AVISO": "#edf7ee"}
_BORDE = {"EVITAR": "#b3261e", "REDUCIR": "#b26b00",
          "VIGILAR": "#2f6fb0", "SIN AVISO": "#1a7f4b"}

_FUENTE = ("font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
           "Helvetica,Arial,sans-serif")


def html(informes: list[Informe], hoy: date) -> str:
    """El mismo informe con jerarquía visual, que es lo que el correo permite.

    Tablas solo donde una tabla informa —los factores y los 8-K, que son filas
    de verdad—; el resto son tarjetas. Todo el ancho está topado a 680 px y las
    tablas usan `width:100%`, de modo que el correo se lee igual en el móvil,
    que es donde se abre la mitad de las veces.
    """
    p = [
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>MoonRocket · informe {hoy}</title></head>",
        f"<body style=\"margin:0;padding:0;background:#f4f4f6;{_FUENTE}\">",
        "<div style='max-width:680px;margin:0 auto;padding:18px 14px 40px'>",
        "<div style='padding:18px 20px;background:#14161a;border-radius:8px;color:#fff'>",
        "<div style=\"font-size:19px;font-weight:700;letter-spacing:-.01em\">MoonRocket</div>",
        f"<div style='font-size:13px;color:#aeb4bd;margin-top:3px'>"
        f"Informe de análisis · {hoy} · {len(informes)} valor(es)</div>",
        "</div>",
        f"<p style='font-size:12px;line-height:1.6;color:#5f6368;margin:14px 2px 20px'>"
        f"{_e(AVISO_COMUN)}</p>",
    ]
    for inf in informes:
        p.append(_html_valor(inf))
    p += [
        "<p style='font-size:11px;color:#80868b;line-height:1.6;margin-top:24px;"
        "border-top:1px solid #dadce0;padding-top:14px'>"
        + _e(_fuentes(informes)) +
        " Ningún dato de este informe procede de prensa ni de una estimación "
        "del sistema.</p>",
        "</div></body></html>",
    ]
    return "\n".join(p)


def _html_valor(inf: Informe) -> str:
    marca = {"EVITAR": "🚫", "REDUCIR": "⚠️", "VIGILAR": "👁", "SIN AVISO": "✅"}
    fondo = _FONDO.get(inf.exposicion, "#f1f3f4")
    borde = _BORDE.get(inf.exposicion, "#9aa0a6")

    p = ["<div style='background:#fff;border:1px solid #dadce0;border-radius:8px;"
         "padding:18px 20px;margin-bottom:18px'>"]

    # --- cabecera ---
    p.append("<div style='border-bottom:1px solid #e8eaed;padding-bottom:12px'>")
    p.append(f"<span style=\"font-size:22px;font-weight:700;letter-spacing:.01em\">"
             f"{_e(inf.activo)}</span>")
    p.append(f"<span style='font-size:13px;color:#5f6368;margin-left:9px'>"
             f"{_e(inf.nombre)}</span>")
    if inf.precio:
        p.append(f"<span style='float:right;font-size:17px;font-weight:600'>"
                 f"{_es(inf.precio)} $</span>")
    p.append(f"<div style='font-size:11px;color:#80868b;margin-top:4px'>"
             f"{inf.generado:%d/%m/%Y %H:%M}"
             + (f" · en el informe por: {_e(', '.join(inf.motivo_inclusion))}"
                if inf.motivo_inclusion else "")
             + "</div>")
    p.append("</div>")

    # --- estado ---
    cambio = ""
    if inf.exposicion_previa and inf.exposicion_previa != inf.exposicion:
        cambio = (f" <span style='font-weight:400;color:#5f6368'>"
                  f"(antes {_e(inf.exposicion_previa)})</span>")
    p.append(f"<div style='margin-top:14px;padding:12px 14px;background:{fondo};"
             f"border-left:4px solid {borde};border-radius:4px'>"
             f"<div style='font-size:15px;font-weight:700'>"
             f"{marca.get(inf.exposicion, '•')} Exposición: {_e(inf.exposicion)}{cambio}</div>"
             f"<div style='font-size:12.5px;color:#3c4043;margin-top:5px;line-height:1.55'>"
             f"{_e(inf.exposicion_motivo)}</div></div>")

    if inf.serie and len(inf.serie) >= 2:
        p.append(_seccion("Un año de precio", _html_grafico(inf)))

    p.append(_seccion("Resumen ejecutivo",
                      _lista([_e(x) for x in inf.resumen])))

    # --- tesis: tabla, porque son filas de verdad ---
    filas = []
    for f in inf.factores:
        if f.signo == "sindato":
            continue
        filas.append(
            "<tr>"
            f"<td style='padding:10px 8px 10px 0;vertical-align:top;width:1%;"
            f"white-space:nowrap'>"
            f"<div style=\"font-size:12.5px;font-weight:700;color:{_COL[f.signo]}\">"
            f"{ICONO[f.signo]} {_e(f.titulo)}</div>"
            f"<div style='font-size:10.5px;color:#80868b;margin-top:2px'>"
            f"{_e(f.fuente)}</div></td>"
            f"<td style='padding:10px 0;vertical-align:top;border-bottom:1px solid #f1f3f4'>"
            f"<div style='font-size:13.5px;font-weight:600'>{_e(f.dato)}</div>"
            f"<div style='font-size:12.5px;color:#3c4043;line-height:1.55;margin-top:3px'>"
            f"{_e(f.interpretacion)}</div>"
            f"<div style=\"font-size:12px;color:{_COL[f.signo]};margin-top:3px\">"
            f"→ {_e(f.impacto)}</div></td></tr>")
    p.append(_seccion(
        "Tesis · qué sostiene el estado",
        "<table style='width:100%;border-collapse:collapse'>" + "".join(filas) + "</table>"
        + "<div style='font-size:11px;color:#80868b;margin-top:8px;line-height:1.55'>"
          "Cada fila lleva el dato, qué significa y qué se sigue de él, separados "
          "a propósito. Un color no es «comprar»: dice respecto a qué mide ese "
          "bloque.</div>"))

    # --- hechos ---
    if inf.hechos:
        fh = "".join(
            "<tr>"
            f"<td style='padding:7px 10px 7px 0;font-size:12.5px;white-space:nowrap;"
            f"vertical-align:top;border-bottom:1px solid #f1f3f4'>{_e(x['f'])}</td>"
            f"<td style='padding:7px 0;font-size:12.5px;border-bottom:1px solid #f1f3f4'>"
            + _e(", ".join(i["t"] for i in x["i"]))
            + ("<span style=\"color:#b3261e;font-weight:600\"> · adverso por "
               "definición del formulario</span>" if x["mal"] else "")
            + "</td></tr>"
            for x in inf.hechos[:6])
        cuerpo = ("<table style='width:100%;border-collapse:collapse'>" + fh + "</table>"
                  "<div style='font-size:11px;color:#80868b;margin-top:8px;line-height:1.55'>"
                  "Un 8-K es de presentación obligatoria y va fechado por la SEC. "
                  "Es el equivalente verificable de una noticia: el sistema no usa "
                  "prensa.</div>")
    else:
        cuerpo = ("<div style='font-size:12.5px;color:#3c4043;line-height:1.6'>"
                  f"Ningún hecho relevante declarado en los últimos {DIAS_HECHO} días. "
                  "El sistema no usa prensa: su única noticia verificable es el 8-K, "
                  "y no hay ninguno.</div>")
    # Un perpetuo no tiene SEC. La seccion se omite en vez de decir «ninguno»,
    # que insinuaria que podria haberlos y que el sistema no los encontro.
    if inf.mercado != "cripto":
        p.append(_seccion("Hechos y eventos relevantes", cuerpo))

    cuerpo_r = _lista([_e(x) for x in inf.riesgos])
    if inf.vigilar:
        cuerpo_r += ("<div style='font-size:10.5px;font-weight:700;letter-spacing:.08em;"
                     "text-transform:uppercase;color:#80868b;margin:12px 0 6px'>"
                     "A vigilar</div>" + _lista([_e(x) for x in inf.vigilar]))
    p.append(_seccion("Riesgos y factores en contra", cuerpo_r))

    p.append(_seccion("Qué ha cambiado",
                      _lista([_e(x) for x in inf.cambios]) if inf.cambios
                      else "<div style='font-size:12.5px;color:#5f6368'>"
                           "Nada medible desde el informe anterior.</div>"))

    p.append(_seccion("Conclusión", _html_conclusion(inf)))

    p.append("</div>")
    return "\n".join(p)


#: Colores de las dos mitades de la conclusión. El titular va en el color del
#: veredicto de los analistas; el recuento propio va SIEMPRE en gris, porque no
#: tiene dirección y pintarlo de verde sería dársela por la puerta de atrás.
_VER_FONDO = {"up": "#edf7ee", "dn": "#fdecea", "med": "#fff8e1"}
_VER_BORDE = {"up": "#1a7f4b", "dn": "#b3261e", "med": "#b26b00"}


def _fuentes(informes: list[Informe]) -> str:
    """De dónde sale lo que hay dentro, y solo lo que hay dentro.

    Un correo de perpetuos que dijera «a partir de EDGAR» estaría declarando
    una fuente que no ha tocado: para un perpetuo no hay SEC.
    """
    hay_cripto = any(i.mercado == "cripto" for i in informes)
    hay_acciones = any(i.mercado != "cripto" for i in informes)
    partes = []
    if hay_acciones:
        partes.append("EDGAR (Form 4, 8-K y XBRL) y del calendario y consenso "
                      "de Nasdaq")
    if hay_cripto:
        partes.append("los dumps de Binance y las velas de Kraken")
    return ("Generado por MoonRocket a partir de " + " y de ".join(partes)
            + ", y de la serie de precios propia.")


def _sin_veredicto(inf: Informe) -> str:
    """Por qué no hay conclusión en escala, que no es lo mismo en cada rama.

    En una acción es una laguna: alguien podría cubrirla mañana. En un perpetuo
    no hay nada que cubrir —nadie publica objetivos de precio de un contrato
    perpetuo— y decirlo como si fuera un dato que falta sugeriría que el sistema
    se ha quedado corto.
    """
    if inf.mercado == "cripto":
        return ("No hay conclusión en escala de compra o venta, y no puede "
                "haberla: nadie publica consenso ni precio objetivo de un "
                "perpetuo.")
    return ("Sin consenso de analistas publicado para este valor: no hay "
            "conclusión en escala de compra o venta.")


def _detalle_veredicto(v: dict) -> str:
    partes = []
    if v.get("casas"):
        partes.append(f"{v['casas']} casas")
    if v.get("n"):
        partes.append(f"{v['compra']} comprar / {v['mantener']} mantener / "
                      f"{v['venta']} vender")
    if v.get("objetivo"):
        partes.append(f"objetivo {_es(v['objetivo'])} $")
    partes.append(v["origen"])
    return " · ".join(partes)


def _html_conclusion(inf: Informe) -> str:
    """El titular de los analistas y el recuento propio, separados de verdad.

    Dos cajas y no una: quien lee tiene que poder decir de un vistazo cuál de
    las dos frases firma MoonRocket, y no es la de arriba.
    """
    v = inf.veredicto
    if v:
        marca = {"up": "🟢", "dn": "🔴", "med": "🟡"}[v["clase"]]
        titular = (
            f"<div style=\"padding:14px 16px;background:{_VER_FONDO[v['clase']]};"
            f"border-left:4px solid {_VER_BORDE[v['clase']]};border-radius:4px\">"
            f"<div style='font-size:10.5px;font-weight:700;letter-spacing:.09em;"
            f"text-transform:uppercase;color:#5f6368'>Consenso de analistas</div>"
            f"<div style=\"font-size:20px;font-weight:700;margin-top:4px;"
            f"color:{_VER_BORDE[v['clase']]}\">{marca} {_e(v['etiqueta'])}</div>"
            f"<div style='font-size:12px;color:#3c4043;margin-top:5px;line-height:1.55'>"
            f"{_e(_detalle_veredicto(v))}</div>"
            f"<div style='font-size:11px;color:#5f6368;margin-top:6px'>"
            f"La firman los analistas, no MoonRocket.</div></div>")
    else:
        titular = ("<div style='padding:14px 16px;background:#f1f3f4;"
                   "border-left:4px solid #9aa0a6;border-radius:4px;font-size:12.5px;"
                   "color:#3c4043'>" + _e(_sin_veredicto(inf)) + "</div>")

    c = recuento_propio(inf)
    propio = (
        "<div style='margin-top:12px;padding:12px 14px;background:#f8f9fa;"
        "border-left:4px solid #9aa0a6;border-radius:4px'>"
        "<div style='font-size:10.5px;font-weight:700;letter-spacing:.09em;"
        "text-transform:uppercase;color:#5f6368'>Lectura propia de MoonRocket "
        "· sin dirección</div>"
        f"<div style='font-size:13px;color:#202124;margin-top:5px'>"
        f"<b>{c['favor']}</b> a favor · <b>{c['contra']}</b> en contra · "
        f"<b>{c['neutral']}</b> sin dirección · <b>{c['sindato']}</b> sin datos"
        f" · exposición <b>{_e(inf.exposicion)}</b></div>"
        "<div style='font-size:11px;color:#5f6368;margin-top:6px;line-height:1.55'>"
        "Este recuento NO se traduce a comprar ni a vender: uno de los bloques "
        "mide una hipótesis todavía sin validar y varios no tienen dirección "
        "medida, así que sumarlos daría una etiqueta con una autoridad que no "
        "tiene."
        + ("" if inf.mercado == "cripto"
           else " El consenso de analistas no entra aquí porque ya es el titular "
                "de arriba.")
        + "</div></div>")

    return (titular + propio
            + f"<div style='font-size:13px;line-height:1.6;color:#202124;"
              f"margin-top:12px'>{_e(inf.conclusion)}</div>"
            + f"<div style='margin-top:10px;padding:9px 12px;background:#f8f9fa;"
              f"border-radius:4px;font-size:12px;color:#3c4043'>"
              f"<b>Nivel de evidencia:</b> {_e(inf.evidencia)}</div>")


def cid_grafico(inf: Informe) -> str:
    """Identificador del PNG dentro del correo. Uno por valor y por informe."""
    return f"g-{inf.activo.lower()}"


def imagenes(informes: list[Informe]) -> tuple[tuple[str, bytes], ...]:
    """Los PNG que el HTML referencia. Un valor sin serie no produce ninguno."""
    from core.delivery import grafico as GR

    fuera = []
    for inf in informes:
        png = GR.serie(inf.serie, referencia=inf.serie[0] if inf.serie else None,
                       marcas=inf.marcas) if inf.serie else None
        if png:
            fuera.append((cid_grafico(inf), png))
    return tuple(fuera)


def _html_grafico(inf: Informe) -> str:
    """La imagen, y debajo en TEXTO lo que la imagen no dice.

    El PNG no lleva ni un número a propósito —ver `delivery/grafico.py`—, así
    que los valores van aquí: se pueden seleccionar, buscar y leer con un lector
    de pantalla, y siguen estando si el cliente bloquea las imágenes, que es lo
    que hace Gmail por defecto con un remitente nuevo.
    """
    ini, fin = inf.serie[0], inf.serie[-1]
    var = fin / ini - 1 if ini else None
    pie = [f"de {_es(ini)} $ a {_es(fin)} $"]
    if var is not None:
        pie.append(f"{_pc(var)} en las {len(inf.serie)} sesiones dibujadas")
    if inf.marcas:
        pie.append(f"{len(inf.marcas)} día(s) con compras de insiders, en ámbar")
    return (
        f"<img src=\"cid:{cid_grafico(inf)}\" width=\"640\" alt=\""
        f"Precio de {_e(inf.activo)} en el último año\" "
        f"style=\"display:block;width:100%;max-width:640px;height:auto;"
        f"border:1px solid #e8eaed;border-radius:4px\">"
        f"<div style='font-size:11.5px;color:#5f6368;margin-top:7px;line-height:1.55'>"
        f"{_e(' · '.join(pie))}. La línea discontinua es el precio de partida.</div>")


def _seccion(titulo: str, cuerpo: str) -> str:
    return (f"<div style='margin-top:18px'>"
            f"<div style='font-size:10.5px;font-weight:700;letter-spacing:.1em;"
            f"text-transform:uppercase;color:#80868b;padding-bottom:6px;"
            f"border-bottom:1px solid #e8eaed'>{_e(titulo)}</div>"
            f"<div style='margin-top:10px'>{cuerpo}</div></div>")


def _lista(items: list[str]) -> str:
    return ("<ul style='margin:0;padding-left:18px;font-size:12.5px;line-height:1.65;"
            "color:#3c4043'>"
            + "".join(f"<li style=\"margin:4px 0\">{x}</li>" for x in items)
            + "</ul>")
