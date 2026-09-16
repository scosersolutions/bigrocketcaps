"""Registro de predicciones a prueba de manipulación posterior.

## El problema que resuelve

Una hipótesis se valida comparando lo que se predijo con lo que pasó. Eso solo
significa algo si la predicción es **anterior** al resultado y **no se puede
retocar** después. Un fichero normal no da ninguna de las dos garantías: nada
impide reescribir ayer para que cuadre con hoy.

Aquí el sospechoso habitual soy yo. Ocho hipótesis han caído por controles que
detectaron formas de engañarse a uno mismo, y la más difícil de detectar es
ajustar el recuerdo de lo que se esperaba. Este módulo lo hace imposible sin
que se note.

## Cómo

Fichero **solo de anexado** donde cada registro incluye el hash del anterior.
Cambiar un registro pasado cambia su hash, que rompe el encadenamiento de todos
los posteriores. No impide editar; hace que editar sea **evidente**.

Los resultados **nunca modifican la predicción**: se anexan como registros
nuevos que la referencian. Así la predicción original sigue ahí, con su fecha,
diga lo que diga el resultado.

## Escrituras simultáneas

`anexar` lee el último hash y luego escribe. Si dos procesos hacen eso a la vez
—dos sesiones trabajando en el mismo repo, que ya ha pasado— el segundo escribe
con un `hash_previo` que ya no es el último y la cadena queda rota. Rota de
forma detectable, que es lo que promete el módulo, pero rota igual y sin que
nadie lo haya editado.

Por eso `anexar` toma un cerrojo de fichero: leer el último hash y escribir el
registro nuevo pasan a ser una sola operación indivisible.

## Lo que esto no es

No es criptografía seria: quien controle el fichero puede recalcular toda la
cadena. Protege contra el descuido y contra la tentación, que es de lo que se
trata, no contra un adversario.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Literal

GENESIS = "0" * 64
Tipo = Literal[
    "prediccion", "resultado",
    # Diario de la cartera de demostración. Comparte fichero y cadena con las
    # predicciones a propósito: es el mismo experimento visto de dos maneras.
    "cartera_abierta", "posicion_abierta", "posicion_cerrada",
    "valoracion", "cartera_cerrada",
    # Contabilidad de contrastes. Vive en su propio fichero, pero con la misma
    # cadena: el contador que entra en el Deflated Sharpe solo vale si tampoco
    # se puede retocar hacia atrás, y una propuesta gasta presupuesto aunque
    # nunca llegue a ejecutarse.
    "experimento", "propuesta",
    # Retirada de un pre-registro sellado que no se va a medir. NO edita el
    # fichero sellado ni su registro `propuesta`: los dos siguen ahí con su
    # sha256 intacto, y esto se anexa detrás referenciándolos. Retirar no
    # devuelve presupuesto: la hipótesis formó parte de la búsqueda, y poder
    # descontarla después sería poder ajustar el denominador a conveniencia.
    "retirada",
    # Segunda medición de E7c contra su quintil de liquidez. Referencia a la
    # predicción, nunca la modifica: la comparación contra IWM sigue siendo la
    # que se pre-registró y con la que se juzgará.
    "control_liquidez",
    # Papel hacia delante de cripto. Fichero propio (`data/papel/cripto.jsonl`)
    # y cadena propia: comparte formato con e7c pero no historia, porque son dos
    # experimentos distintos y mezclarlos haría que el hash de uno dependiera de
    # cuándo se ejecutó el otro.
    "papel_cripto_abierta", "papel_cripto_cerrada",
    # Volumen que faltaba el día de la señal porque la descarga falló. Completa
    # la predicción sin reescribirla; el dato se recalcula con la ventana de la
    # señal, no con la de hoy, para que sea el mismo número que se habría
    # anotado entonces.
    "liquidez",
    # Marcador de los avisos del informe diario. Fichero propio
    # (`data/papel/avisos.jsonl`) y cadena propia, por lo mismo que cripto: es
    # otro experimento y mezclarlo haría que el hash de E7c dependiera de
    # cuándo se mandó un correo. El `aviso` lleva dentro los criterios con los
    # que se va a juzgar, escritos antes de que exista el resultado; el
    # `resultado_aviso` lo referencia y nunca lo modifica.
    "aviso", "resultado_aviso",
]


def huella_fichero(ruta: Path) -> str:
    """sha256 del contenido de un pre-registro, **normalizando los saltos de
    línea**.

    Sin normalizar, la huella depende de `core.autocrlf` de git y no del
    contenido: C6 se selló con LF, git lo dejó en CRLF en el siguiente
    checkout, y la comprobación de integridad pasó a decir «alterado» sobre un
    fichero que nadie había tocado —un solo commit en su historial—.

    Una detección de manipulación que salta sin manipulación es peor que no
    tenerla: enseña a ignorarla. Normalizar la hace invariante a la
    configuración del cliente, que es lo que siempre debió ser.

    Compatible hacia atrás: de los doce sellados, once dan la misma huella con
    y sin normalizar, y el duodécimo (C6) solo cuadra normalizando, que era
    justamente el valor anotado en la cadena.
    """
    return hashlib.sha256(ruta.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


class CadenaRota(Exception):
    """El fichero fue alterado después de escribirse."""


class CerrojoOcupado(Exception):
    """Otro proceso está escribiendo en la cadena y no soltó a tiempo."""


class EscritorEquivocado(Exception):
    """Esta máquina no es la que escribe en esta cadena."""


#: Cadenas cuyo único escritor es el job de Actions.
#:
#: El cerrojo de fichero protege de dos procesos del MISMO disco, y contra eso
#: funciona. No puede hacer nada contra dos máquinas: la tarea del Programador
#: de Windows y el job registraban LAS MISMAS señales con marcas de tiempo
#: distintas, y a partir de ahí hay dos cadenas igual de válidas que ya no se
#: pueden fusionar —los hashes divergen desde el primer registro doble—.
#:
#: Pasó el 2026-09-12 (local 166, origin 196) y el 2026-09-15 (las mismas 16
#: predicciones del día 14, local a las 15:03 y origin a las 12:26). Las dos
#: veces se descartó la local, porque el job hace además las valoraciones y
#: mueve la cartera: origin ha sido siempre superconjunto en contenido.
#:
#: Gana el job y no el portátil porque el job corre aunque el portátil esté
#: apagado, que es la mitad de los días.
SOLO_CI = frozenset({"e7c.jsonl"})

#: Escape para cuando Actions lleva días caído y hay que registrar a mano. Es
#: deliberadamente incómodo: quien lo use está creando la bifurcación a
#: sabiendas, y luego tendrá que resolverla.
VARIABLE_ESCAPE = "MR_FORZAR_ESCRITURA"


def _puede_escribir(ruta: Path) -> bool:
    if ruta.name not in SOLO_CI:
        return True
    if os.environ.get(VARIABLE_ESCAPE) == "1":
        return True
    return os.environ.get("GITHUB_ACTIONS") == "true"


# Un cerrojo abandonado por un proceso muerto no puede bloquear el registro para
# siempre; pasado este tiempo se considera huérfano. Es holgado a propósito:
# anexar tarda milisegundos, así que un cerrojo de más de un minuto es un
# proceso muerto, no uno lento.
CERROJO_CADUCA_S = 60
CERROJO_ESPERA_S = 15


@contextmanager
def _cerrojo(ruta: Path, espera: float = CERROJO_ESPERA_S) -> Iterator[None]:
    """Exclusión mutua entre procesos, por creación atómica de un fichero.

    `O_CREAT | O_EXCL` falla si el fichero existe, y esa comprobación la hace el
    sistema de ficheros en un solo paso: no hay ventana entre mirar y crear. Es
    la parte que un `if not existe: crear` no puede dar, y funciona igual en
    Windows que en POSIX, a diferencia de `fcntl`.
    """
    candado = ruta.with_suffix(ruta.suffix + ".lock")
    limite = time.monotonic() + espera
    fd = None
    while fd is None:
        try:
            fd = os.open(candado, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                edad = time.time() - candado.stat().st_mtime
            except FileNotFoundError:
                continue                      # lo soltaron entre el fallo y el stat
            if edad > CERROJO_CADUCA_S:
                candado.unlink(missing_ok=True)
                continue
            if time.monotonic() > limite:
                raise CerrojoOcupado(
                    f"{candado} lleva {edad:.0f} s ocupado. Si ninguna otra sesión "
                    "está escribiendo, bórralo a mano.") from None
            time.sleep(0.05)
    try:
        os.write(fd, f"{os.getpid()} {datetime.now(UTC).isoformat()}\n".encode())
        os.close(fd)
        fd = None
        yield
    finally:
        if fd is not None:
            os.close(fd)
        candado.unlink(missing_ok=True)


def _huella(hash_previo: str, cuerpo: dict[str, Any]) -> str:
    # `sort_keys` hace que la huella no dependa del orden en que se
    # construyó el diccionario, que en Python puede variar.
    serie = json.dumps(cuerpo, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{hash_previo}{serie}".encode()).hexdigest()


class Registro:
    """Diario de predicciones y resultados, solo de anexado."""

    def __init__(self, ruta: Path | str) -> None:
        self.ruta = Path(ruta)
        self.ruta.parent.mkdir(parents=True, exist_ok=True)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        if not self.ruta.exists():
            return
        with self.ruta.open(encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if linea:
                    yield json.loads(linea)

    def _ultimo_hash(self) -> str:
        ultimo = GENESIS
        for r in self:
            ultimo = r["hash"]
        return ultimo

    def anexar(self, tipo: Tipo, cuerpo: dict[str, Any]) -> dict[str, Any]:
        """Añade un registro encadenado al último.

        Leer el hash anterior y escribir el registro nuevo van dentro del mismo
        cerrojo: entre las dos operaciones no puede colarse otro proceso, que es
        justo lo que dejaría dos registros apuntando al mismo predecesor.

        El cerrojo no alcanza a dos máquinas distintas; de eso se ocupa
        `SOLO_CI`, que decide quién es el escritor de cada cadena.
        """
        if not _puede_escribir(self.ruta):
            raise EscritorEquivocado(
                f"{self.ruta.name} la escribe el job de Actions, no esta "
                f"máquina. Escribir las dos bifurca la cadena y la bifurcación "
                f"no se puede fusionar. Si Actions está caído y hace falta "
                f"registrar a mano: {VARIABLE_ESCAPE}=1, sabiendo que después "
                f"habrá dos cadenas y habrá que descartar una."
            )
        with _cerrojo(self.ruta):
            previo = self._ultimo_hash()
            cuerpo = {
                "tipo": tipo,
                "ts": datetime.now(UTC).isoformat(),
                **cuerpo,
            }
            registro = {**cuerpo, "hash_previo": previo, "hash": _huella(previo, cuerpo)}
            with self.ruta.open("a", encoding="utf-8") as f:
                f.write(json.dumps(registro, ensure_ascii=False, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())    # que el siguiente en entrar lo lea de verdad
        return registro

    def verificar(self) -> int:
        """Recorre la cadena. Devuelve cuántos registros hay; falla si se tocó."""
        previo, n = GENESIS, 0
        for i, r in enumerate(self, start=1):
            cuerpo = {k: v for k, v in r.items() if k not in ("hash", "hash_previo")}
            if r["hash_previo"] != previo:
                raise CadenaRota(f"registro {i}: no enlaza con el anterior")
            if r["hash"] != _huella(previo, cuerpo):
                raise CadenaRota(f"registro {i}: el contenido no cuadra con su huella")
            previo, n = r["hash"], n + 1
        return n

    def predicciones(self) -> list[dict[str, Any]]:
        """Predicciones con la liquidez que llegó tarde ya aplicada.

        Cuando la descarga de precios falla el día de la señal, la predicción
        se anota sin volumen y sin clasificar en variantes: sin ese dato no se
        puede saber si entra en la banda, y rellenarlo con la única variante
        que no lo exige afirmaría algo que no se sabe.

        El dato se completa después con un registro `liquidez` aparte, porque
        la cadena es de solo anexado y la predicción original tiene que seguir
        siendo verificable letra por letra. Aquí se fusionan las dos caras: el
        fichero conserva la historia —qué se supo y cuándo— y quien lee las
        predicciones ve el estado actual sin tener que reconstruirlo.
        """
        tardias = {r["id_prediccion"]: r for r in self if r["tipo"] == "liquidez"}
        salida = []
        for p in self:
            if p["tipo"] != "prediccion":
                continue
            completa = tardias.get(p["id"])
            if completa is None:
                salida.append(p)
                continue
            salida.append({**p,
                           "dolares_dia": completa["dolares_dia"],
                           "precio_referencia": completa["precio_referencia"],
                           "en_banda": completa["en_banda"],
                           "variantes": completa["variantes"],
                           "liquidez_pendiente": False,
                           "no_operable": completa.get("no_operable", False),
                           "motivo_no_operable": completa.get("motivo"),
                           "liquidez_completada": completa["ts"]})
        return salida

    def sin_liquidez(self) -> list[dict[str, Any]]:
        """Predicciones a las que aún les falta el volumen del día de la señal.

        Se mira el dato y no solo la marca: las señales anotadas antes de que
        existiera `liquidez_pendiente` tienen el mismo agujero sin declararlo.

        Las ya cerradas como no operables quedan fuera: su volumen seguirá
        siendo `None` para siempre y volver a pedirlo cada mañana es gastar
        peticiones en un símbolo que el proveedor no reconoce o en un valor
        que no negocia.
        """
        return [p for p in self.predicciones()
                if not p.get("no_operable")
                and (p.get("liquidez_pendiente") or p.get("dolares_dia") is None)]

    def resueltas(self) -> set[str]:
        """Identificadores de predicción que ya tienen resultado anotado."""
        return {r["id_prediccion"] for r in self if r["tipo"] == "resultado"}

    def pendientes(self) -> list[dict[str, Any]]:
        ya = self.resueltas()
        return [p for p in self.predicciones() if p["id"] not in ya]
