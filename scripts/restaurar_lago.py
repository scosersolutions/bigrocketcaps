"""Reconstruye la base desde el lago de Parquet, verificando cada pieza.

Esta es la TASK 0.3 del plan, y es la única que de verdad cuenta: **una copia
que no se ha restaurado nunca no es una copia**. Exportar es fácil de hacer mal
de una forma que solo se descubre el día que hace falta.

Qué comprueba, y en este orden:

1. Que el manifiesto no se ha tocado (su propia huella).
2. Que cada Parquet tiene el `sha256` que decía el manifiesto.
3. Que al cargarlo salen las filas que decía.
4. Que el XOR de hashes de fila coincide con el original. Esto último es lo que
   distingue «se copiaron los bytes» de «están los mismos datos»: sobrevive a
   que Parquet reordene o a que DuckDB cambie de versión.

Falla ruidosamente y a la primera. Una restauración a medias es peor que
ninguna, porque parece que hay copia.

## No compares la copia con SUM() ni AVG() de columnas DOUBLE

Medido el 2026-09-15 sobre esta misma copia: `SUM(valor)` de insiders daba
9162117670530844 en la base y ...894 en la restaurada, y `AVG(bid_1)` difería
en el dígito 16. **No había ni un dato distinto**: la suma de coma flotante no
es asociativa, y la exportación reordena las filas para ser idempotente, así
que el error de acumulación se reparte distinto.

Con `SUM(valor::DECIMAL(38,6))` y con `BIT_XOR(hash(valor))` coinciden exactas.
Para comparar dos copias se usa aritmética exacta o hashes; con dobles se
persiguen fantasmas.

Uso:
    python scripts/restaurar_lago.py --destino C:/tmp/prueba.duckdb
    python scripts/restaurar_lago.py --solo-verificar
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import duckdb

LAGO = Path.home() / "moonrocket-lago"


class LagoRoto(RuntimeError):
    """El lago no contiene lo que el manifiesto dice que contiene."""


def _sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with ruta.open("rb") as f:
        for trozo in iter(lambda: f.read(1024 * 1024), b""):
            h.update(trozo)
    return h.hexdigest()


def leer_manifiesto(lago: Path) -> dict:
    ruta = lago / "MANIFIESTO.json"
    if not ruta.exists():
        raise LagoRoto(f"no hay MANIFIESTO.json en {lago}")
    completo = json.loads(ruta.read_text(encoding="utf-8"))
    cuerpo = {k: v for k, v in completo.items()
              if k not in ("huella", "exportado")}
    esperada = hashlib.sha256(
        json.dumps(cuerpo, indent=2, sort_keys=True,
                   ensure_ascii=False).encode()).hexdigest()
    if esperada != completo.get("huella"):
        raise LagoRoto(
            "el manifiesto no coincide con su propia huella: alguien lo editó")
    return completo


def verificar_ficheros(lago: Path, manifiesto: dict) -> int:
    """Comprueba los sha256 antes de tocar nada. Devuelve ficheros verificados."""
    n = 0
    for tabla, info in manifiesto["tablas"].items():
        for clave, meta in info["ficheros"].items():
            ruta = lago / tabla / f"{clave}.parquet"
            if not ruta.exists():
                raise LagoRoto(f"falta {ruta}")
            real = _sha256(ruta)
            if real != meta["sha256"]:
                raise LagoRoto(
                    f"{ruta} está corrupto\n  esperado {meta['sha256']}\n"
                    f"  real     {real}")
            n += 1
    return n


def restaurar(lago: Path, destino: Path, manifiesto: dict) -> None:
    if destino.exists():
        raise LagoRoto(
            f"{destino} ya existe. Restaurar sobre una base viva podría "
            f"machacarla: bórrala a mano si de verdad quieres reemplazarla.")
    con = duckdb.connect(str(destino))

    for tabla, info in manifiesto["tablas"].items():
        if info.get("ddl"):
            con.execute(info["ddl"])
        if info.get("vacia"):
            print(f"  {tabla:<20} vacía (solo esquema)")
            continue

        for clave in sorted(info["ficheros"]):
            ruta = lago / tabla / f"{clave}.parquet"
            con.execute(
                f'INSERT INTO "{tabla}" SELECT * FROM '
                f"read_parquet('{ruta.as_posix()}')")

        filas = con.execute(f'SELECT COUNT(*) FROM "{tabla}"').fetchone()[0]
        if filas != info["filas"]:
            raise LagoRoto(
                f"{tabla}: restauradas {filas:,} filas, el manifiesto dice "
                f"{info['filas']:,}")

        # La comprobación que de verdad importa: los mismos DATOS, no los
        # mismos bytes. Se recalcula por año para localizar dónde falla.
        cols = ", ".join(
            f'"{c[0]}"' for c in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [tabla]).fetchall())
        columna = info["columna_tiempo"]
        for clave, meta in info["ficheros"].items():
            if clave == "sin_fecha":
                continue
            real = con.execute(
                f'SELECT COALESCE(BIT_XOR(hash({cols})), 0) FROM "{tabla}" '
                f'WHERE EXTRACT(year FROM "{columna}") = ?', [int(clave)]
            ).fetchone()[0]
            if str(int(real)) != meta["contenido"]:
                raise LagoRoto(
                    f"{tabla}/{clave}: el contenido no coincide. Los bytes "
                    f"estaban bien, los datos no.")
        print(f"  {tabla:<20} {filas:>12,} filas  verificada")

    con.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lago", type=Path, default=LAGO)
    p.add_argument("--destino", type=Path,
                   default=Path("data/restaurada.duckdb"))
    p.add_argument("--solo-verificar", action="store_true",
                   help="comprueba los sha256 y no reconstruye nada")
    args = p.parse_args()

    try:
        manifiesto = leer_manifiesto(args.lago)
        print(f"lago    {args.lago}")
        print(f"huella  {manifiesto['huella']}")
        print(f"exportado {manifiesto.get('exportado', '?')}\n")

        n = verificar_ficheros(args.lago, manifiesto)
        print(f"{n} ficheros con su sha256 correcto\n")
        if args.solo_verificar:
            return 0

        restaurar(args.lago, args.destino, manifiesto)
        total = sum(t["filas"] for t in manifiesto["tablas"].values())
        print(f"\nrestaurada en {args.destino}  ({total:,} filas)")
        return 0
    except LagoRoto as e:
        print(f"\nLAGO ROTO: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
