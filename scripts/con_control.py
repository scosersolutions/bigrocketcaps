"""Ejecuta un comando y lo reporta al Centro de Control.

    python scripts/con_control.py --slug brc-veredictos
        --resultado veredicto=data/experimentos/veredicto_b1.json
        --resumen "exceso {veredicto.exceso_medio_pct}% en {veredicto.n} eventos"
        -- python scripts/juzgar_b1.py

## Por que un envoltorio y no reportar desde cada script

`juzgar_b1.py` y `medir_spread_us.py` miden; que alguien lo este mirando no es
asunto suyo. Metiendoles el SDK dentro habria que pasarles credenciales,
decidir que hacen sin ellas y probar esa rama en cada uno. Fuera, el reporte
es un anillo alrededor: los dos comandos siguen corriendo igual en un portatil
sin token, y el dia que haya un tercero no hay que tocarlo.

## Sin credenciales se mide igual

Si faltan `CONTROL_CENTER_URL` o `CONTROL_CENTER_TOKEN` el comando se ejecuta
y no se reporta nada. No poder avisar no es motivo para no medir, y si lo
fuera, un token caducado pararia la horquilla sin que nadie entendiera por que.

## Pausada de verdad

Si la automatizacion esta pausada en el Centro, el comando NO se ejecuta: el
run se cierra como `skipped` y se sale con 0. Es lo que significa pausar algo
que cuesta cinco minutos de runner y peticiones a un tercero.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from brc.control_center import ControlCenter, ControlCenterError

#: `{clave.campo}` dentro de --resumen. Solo un nivel: si el dato que quieres
#: resumir esta a tres de profundidad, el resumen no es el sitio.
HUECO = re.compile(r"\{([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)\}")


def metricas(resultados: dict[str, Any]) -> dict[str, float]:
    """Los campos numericos de primer nivel de cada resultado, como metricas.

    `bool` queda fuera aunque en Python sea un `int`: "refutada = 1" no es una
    medida, es una etiqueta, y en una grafica de serie temporal mentiria.
    """
    salida: dict[str, float] = {}
    for clave, payload in resultados.items():
        if not isinstance(payload, dict):
            continue
        for campo, valor in payload.items():
            if isinstance(valor, bool) or not isinstance(valor, (int, float)):
                continue
            salida[f"{clave}.{campo}"] = valor
    return salida


def rellenar(plantilla: str, resultados: dict[str, Any]) -> str:
    """Sustituye `{clave.campo}` por su valor. Un hueco sin dato se queda tal cual.

    Se deja visible a proposito: un resumen que dice `{veredicto.percentil}`
    ensena que el campo se llama de otra forma. Uno que dijera `None` o que se
    comiera el hueco lo escondería.
    """
    def uno(m: re.Match[str]) -> str:
        payload = resultados.get(m.group(1))
        if not isinstance(payload, dict) or m.group(2) not in payload:
            return m.group(0)
        return str(payload[m.group(2)])
    return HUECO.sub(uno, plantilla)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--slug", required=True)
    p.add_argument("--resultado", action="append", default=[], metavar="CLAVE=RUTA",
                   help="JSON a publicar bajo esa clave. Repetible.")
    p.add_argument("--resumen", default="", help="Admite huecos {clave.campo}.")
    p.add_argument("orden", nargs=argparse.REMAINDER,
                   help="-- y el comando a ejecutar")
    a = p.parse_args()

    orden = a.orden[1:] if a.orden and a.orden[0] == "--" else a.orden
    if not orden:
        p.error("falta el comando: ... -- python scripts/lo_que_sea.py")

    pares = []
    for crudo in a.resultado:
        clave, sep, ruta = crudo.partition("=")
        if not sep:
            p.error(f"--resultado quiere CLAVE=RUTA, no {crudo!r}")
        pares.append((clave, Path(ruta)))

    if not (os.environ.get("CONTROL_CENTER_URL")
            and os.environ.get("CONTROL_CENTER_TOKEN")):
        print("[control] sin credenciales: se ejecuta sin reportar")
        return subprocess.run(orden).returncode

    cc = ControlCenter()
    with cc.run(a.slug) as r:
        if r.skipped:
            print(f"[control] '{a.slug}' esta pausada: no se ejecuta nada")
            return 0

        codigo = subprocess.run(orden).returncode
        if codigo != 0:
            r.event("critical", f"{a.slug}: el comando fallo",
                    f"codigo {codigo}: " + " ".join(orden))
            # El run se cierra como error por la excepcion, no a mano. Y se
            # sale con el codigo del comando, no con un 1 de cortesia: quien
            # llame a esto vera el fallo de verdad.
            raise SystemExit(codigo)

        resultados: dict[str, Any] = {}
        for clave, ruta in pares:
            if not ruta.exists():
                r.event("warn", f"{a.slug}: falta {ruta}",
                        "el comando termino bien pero no dejo el fichero")
                continue
            resultados[clave] = json.loads(ruta.read_text(encoding="utf-8"))
            r.result(clave, resultados[clave])

        r.metrics.update(metricas(resultados))
        if a.resumen:
            r.summary = rellenar(a.resumen, resultados)
            print(f"[control] {r.summary}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ControlCenterError as e:
        print(f"[control] {e}", file=sys.stderr)
        raise SystemExit(1) from e
