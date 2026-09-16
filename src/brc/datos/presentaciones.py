"""El índice de presentaciones de EDGAR: cuándo se supo cada cosa.

## Para qué sirve, exactamente

`acciones_circulacion` guarda el valor y la fecha a la que se refiere, más
`accn`, el número de la presentación de la que sale. Falta la pieza que lo
convierte en dato utilizable: **cuándo se hizo público**. Eso vive aquí.

Sin este cruce, el universo parecería conocerse el día del cierre contable, y
no lo es: una empresa declara sus acciones en circulación semanas después del
trimestre al que se refieren. Usar la primera fecha es look-ahead, y en un
estudio transversal contamina TODO el corte, no solo esa empresa.

## La fuente

El índice diario de EDGAR (`daily-index`), que publica un fichero por día hábil
con todas las presentaciones aceptadas. Es el registro oficial: no hay que
inferir nada ni reconstruirlo desde los documentos.

Alternativa descartada: `submissions.zip`, que trae lo mismo empaquetado por
empresa. Pesa varios gigabytes y hay que bajarlo entero para actualizar un día;
el índice diario pide solo el día que falta.

## La hora importa

`aceptado` lleva hora y no solo fecha. Un 8-K aceptado a las 16:05 no se pudo
operar en la sesión de ese día, y tratarlo como si sí daría media jornada de
ventaja que nadie tuvo. El índice diario da la fecha; la hora exacta sale del
propio documento cuando hace falta afinar, y mientras tanto se asume el cierre,
que es el supuesto conservador.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime

import httpx
import polars as pl

#: Índice diario en formato tabular. `form.idx` viene ordenado por tipo de
#: formulario, que es como se va a filtrar.
IDX = ("https://www.sec.gov/Archives/edgar/daily-index/"
       "{anyo}/QTR{q}/form.{fecha}.idx")

PAUSA_S = 0.2


class PresentacionesError(RuntimeError):
    """No se pudo leer el índice, y no se devuelve uno a medias."""


@dataclass(frozen=True)
class Presentacion:
    accession: str
    cik: int
    formulario: str
    presentado: date


def _trimestre(d: date) -> int:
    return (d.month - 1) // 3 + 1


def indice_del_dia(
    dia: date, *, user_agent: str, cliente: httpx.Client | None = None
) -> pl.DataFrame:
    """Todas las presentaciones aceptadas ese día hábil.

    Un día sin índice —fin de semana, festivo, o un día que EDGAR no publicó—
    devuelve vacío. Eso NO es un error: es que no hubo. Distinguirlo de un
    fallo de red es justo lo que evita que un hueco pase por dato.
    """
    if "@" not in user_agent:
        raise PresentacionesError(
            "la SEC exige un User-Agent con correo de contacto REAL")
    propio = cliente is None
    cliente = cliente or httpx.Client(timeout=60, follow_redirects=True)
    url = IDX.format(anyo=dia.year, q=_trimestre(dia), fecha=dia.strftime("%Y%m%d"))
    try:
        r = cliente.get(url, headers={"User-Agent": user_agent,
                                      "Accept-Encoding": "gzip, deflate"})
        if r.status_code == 404:
            return _vacio()
        r.raise_for_status()
        texto = r.text
    except httpx.HTTPError as e:
        raise PresentacionesError(f"{dia}: {type(e).__name__}: {e}") from e
    finally:
        if propio:
            cliente.close()
        time.sleep(PAUSA_S)
    return _parsear(texto, dia)


def _vacio() -> pl.DataFrame:
    return pl.DataFrame(schema={"accession": pl.Utf8, "cik": pl.Int64,
                                "formulario": pl.Utf8, "presentado": pl.Date})


def _parsear(texto: str, dia: date) -> pl.DataFrame:
    """El .idx es de ancho fijo, pero con una cabecera de longitud variable.

    Se localiza la línea de guiones que separa cabecera de datos en vez de
    saltar un número fijo de líneas: EDGAR ha cambiado esa cabecera más de una
    vez, y saltar 11 líneas funciona hasta el día que deja de funcionar, sin
    avisar y devolviendo basura que parece datos.
    """
    lineas = texto.splitlines()
    inicio = next((i + 1 for i, l in enumerate(lineas) if l.startswith("-----")), None)
    if inicio is None:
        return _vacio()

    formularios, ciks, accesos = [], [], []
    for linea in lineas[inicio:]:
        if not linea.strip():
            continue
        # Formato: Form Type | Company Name | CIK | Date Filed | File Name
        partes = linea.rsplit(None, 2)
        if len(partes) < 3:
            continue
        ruta = partes[-1]
        if not ruta.endswith(".txt"):
            continue
        acc = ruta.rsplit("/", 1)[-1].removesuffix(".txt")
        cabeza = partes[0].split()
        if not cabeza:
            continue
        # El CIK es el último número entero de la parte izquierda.
        cik = next((int(x) for x in reversed(cabeza) if x.isdigit()), None)
        if cik is None:
            continue
        formularios.append(cabeza[0])
        ciks.append(cik)
        accesos.append(acc)

    if not accesos:
        return _vacio()
    return pl.DataFrame({
        "accession": accesos,
        "cik": ciks,
        "formulario": formularios,
        "presentado": [dia] * len(accesos),
    }).unique(subset=["accession"], keep="first")


def fechar(
    acciones: pl.DataFrame, presentaciones: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Añade `publicado` a cada dato cruzando por `accn`.

    Devuelve (fechados, sin_fechar). Lo segundo NO se descarta en silencio: un
    dato sin fecha de publicación no es utilizable, y cuántos hay dice si el
    índice está completo o si falta bajar días.
    """
    if acciones.is_empty():
        return acciones, acciones
    j = acciones.join(
        presentaciones.select(
            pl.col("accession").alias("accn"),
            pl.col("presentado").alias("publicado")),
        on="accn", how="left")
    fechados = j.filter(pl.col("publicado").is_not_null())
    huerfanos = j.filter(pl.col("publicado").is_null())
    return fechados, huerfanos


def retraso(fechados: pl.DataFrame) -> pl.DataFrame:
    """Días entre la fecha del dato y su publicación.

    Es la medida de cuánto look-ahead se habría colado usando `fin`. Si sale
    cero o negativo en alguna fila, el cruce está mal: un dato no se publica
    antes de la fecha a la que se refiere.
    """
    return fechados.with_columns(
        (pl.col("publicado") - pl.col("fin")).dt.total_days().alias("retraso_dias")
    )
