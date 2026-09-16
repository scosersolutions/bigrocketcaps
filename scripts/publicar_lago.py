"""Sube el lago a un dataset PRIVADO de Hugging Face.

TASK 0.2 del plan. La copia vive fuera de esta casa, que es lo único que
protege de perder el disco, el portátil o la carpeta.

## Por qué privado, y no es una preferencia

El lago lleva precios derivados del endpoint de gráficos de Yahoo, que no es
una API oficial. Sus términos prohíben la recolección automatizada y la
redistribución: publicar esto en abierto sería redistribuir datos de un tercero
sin licencia, y una reclamación se llevaría por delante justamente la copia de
seguridad. En privado no hay redistribución, y la cuenta gratuita da 100 GB.

Lo que sí puede publicarse algún día es lo derivado de EDGAR, que es dominio
público. Eso es otro dataset y otra decisión.

## Qué sube y qué no

`upload_folder` se salta los ficheros ya commiteados y deduplica los datos ya
subidos, así que un lago de 2,4 GB del que cambia un año de una tabla sube ese
año y no el resto. Verificado en la documentación de `huggingface_hub` 1.31,
no supuesto.

## El límite

La cuenta gratuita da **100 GB de almacenamiento privado**, cifra publicada. El
lago ocupa hoy 2,4 GB. El público es «best-effort», sin cifra garantizada y
condicionado a que el contenido sirva a la comunidad: por eso la copia de
seguridad va en privado, además de por los términos de Yahoo.

Requiere:
    pip install -e ".[lago]"
    hf auth login            (o exportar HF_TOKEN)

Uso:
    python scripts/publicar_lago.py --repo tu-usuario/moonrocket-lago
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

LAGO = Path.home() / "moonrocket-lago"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lago", type=Path, default=LAGO)
    p.add_argument("--repo", default=None,
                   help="usuario/nombre. Por defecto <tu-usuario>/moonrocket-lago")
    p.add_argument("--publico", action="store_true",
                   help="NO usar con datos de precios. Ver el docstring.")
    args = p.parse_args()

    try:
        from huggingface_hub import HfApi
    except ImportError:
        print('falta la dependencia: pip install -e ".[lago]"')
        return 1

    if not (args.lago / "MANIFIESTO.json").exists():
        print(f"no hay lago en {args.lago}. Ejecuta antes exportar_lago.py")
        return 1

    if args.publico:
        print("AVISO: un dataset público con precios de Yahoo redistribuye")
        print("datos de terceros sin licencia. Ver el docstring de este script.")
        if not sys.stdin.isatty():
            print("y esto no es una terminal: no se pide confirmación a ciegas.")
            return 1
        if input("escribe 'entiendo el riesgo' para seguir: ") != \
                "entiendo el riesgo":
            return 1

    # Quién soy, antes de crear nada. Sin esto, la falta de credenciales
    # aparece como un 401 en mitad de la subida y parece un fallo de red.
    token = os.environ.get("HF_TOKEN")
    api = HfApi(token=token)
    try:
        quien = api.whoami()
    except Exception as e:
        print("No hay sesión de Hugging Face en esta máquina.")
        print(f"  ({type(e).__name__}: {str(e)[:100]})")
        print()
        print("Para iniciarla, una de las dos:")
        print("  hf auth login                 # pega el token, queda guardado")
        print("  export HF_TOKEN=hf_...        # solo para esta sesión")
        print()
        print("El token se crea en https://huggingface.co/settings/tokens")
        print("con permiso de ESCRITURA. Uno de solo lectura no puede subir.")
        return 1
    usuario = quien.get("name", "")
    print(f"sesión  {usuario or '?'}")
    repo = args.repo or f"{usuario}/moonrocket-lago"
    if "/" not in repo:
        print(f"el repo debe ser usuario/nombre, no '{repo}'")
        return 1
    api.create_repo(repo, repo_type="dataset",
                    private=not args.publico, exist_ok=True)

    manifiesto = json.loads(
        (args.lago / "MANIFIESTO.json").read_text(encoding="utf-8"))
    total = sum(f["bytes"] for t in manifiesto["tablas"].values()
                for f in t["ficheros"].values())
    print(f"lago   {args.lago}  ({total / 1e9:.2f} GB)")
    print(f"repo   {repo}  ({'PÚBLICO' if args.publico else 'privado'})")

    # upload_folder ya sube solo lo que cambió: compara hashes contra el remoto
    # y parte las carpetas grandes en varios commits.
    api.upload_folder(
        folder_path=str(args.lago),
        repo_id=repo,
        repo_type="dataset",
        commit_message=f"lago {manifiesto['huella'][:12]}",
    )
    print(f"\nsubido. huella {manifiesto['huella']}")
    print("Comprueba la copia de verdad con:")
    print("  python scripts/restaurar_lago.py --destino data/prueba.duckdb")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
