"""El único camino de unión permitido para features.

Un `join` por `ts` no puede saber si el `ts` de la derecha es un instante de
disponibilidad o la etiqueta de una ventana que aún no había terminado. Por eso
no se usa: se usa esto, que exige el contrato de la tabla y comprueba lo que
sale.
"""

from __future__ import annotations

import polars as pl

from core.tiempo.contratos import FuturoEnLosDatos, disponible_en


def unir_causal(
    velas: pl.DataFrame,
    tabla: pl.DataFrame,
    nombre_tabla: str,
    *,
    momento_decision: str = "cierre",
    por: str | None = "activo",
    sufijo: str = "_x",
) -> pl.DataFrame:
    """Une `tabla` a `velas` usando solo lo que se sabía al decidir.

    `velas` decide en su CIERRE, que es `ts + duración`. Cada fila de `tabla`
    recibe su `availability_time` del contrato, y se empareja la última
    observación cuyo `availability_time` es menor o igual que ese cierre.

    `momento_decision` nombra la columna de `velas` con el instante en que se
    decide; si es "cierre" y no existe, se calcula desde el contrato de las
    propias velas. Debe existir alguna: decidir "en la vela" sin decir en qué
    instante de la vela es el origen de casi todo el look-ahead.

    Devuelve `velas` con las columnas de `tabla` añadidas, sufijadas. Las filas
    sin ninguna observación anterior quedan a nulo, que es lo correcto: al
    principio de la serie todavía no se sabía nada.
    """
    if velas.is_empty():
        return velas

    izq = velas
    if momento_decision not in izq.columns:
        if momento_decision != "cierre":
            raise KeyError(
                f"`{momento_decision}` no está en las velas. Columnas: {izq.columns}")
        raise KeyError(
            "las velas no traen `cierre`. Pásalas por `disponible_en(velas, "
            "'ohlcv_1h')` y renombra `availability_time` a `cierre`, o di con "
            "`momento_decision` qué columna es el instante de la decisión. "
            "Decidir «en la vela» sin decir en qué instante es de donde sale "
            "casi todo el look-ahead.")

    der = disponible_en(tabla, nombre_tabla)

    agrupa = por if por and por in izq.columns and por in der.columns else None

    # `join_asof` exige las dos partes ordenadas por su clave, y CON `by` el
    # orden tiene que ser dentro de cada grupo: polars avisa de que no puede
    # comprobarlo, y si no lo está empareja mal en silencio.
    izq = izq.sort([agrupa, momento_decision] if agrupa else momento_decision)
    der = der.sort([agrupa, "availability_time"] if agrupa else "availability_time")

    unido = izq.join_asof(
        der,
        left_on=momento_decision,
        right_on="availability_time",
        by=agrupa,
        strategy="backward",       # solo hacia atrás: nunca una observación futura
        suffix=sufijo,
    )

    # Comprobación de salida. `strategy="backward"` ya lo garantiza, pero esto
    # cuesta una comparación y protege de un cambio futuro en polars o de que
    # alguien toque las claves: el coste de que falle en silencio es un
    # backtest que parece bueno.
    if "availability_time" in unido.columns:
        futuro = unido.filter(
            pl.col("availability_time").is_not_null()
            & (pl.col("availability_time") > pl.col(momento_decision))
        )
        if not futuro.is_empty():
            raise FuturoEnLosDatos(
                f"{futuro.height} filas de `{nombre_tabla}` quedaron emparejadas con "
                f"una decisión anterior a su disponibilidad. La unión no es causal.")

    return unido
