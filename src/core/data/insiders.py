"""Compras de insiders declaradas a la SEC en los Forms 3, 4 y 5.

Fuente: **Insider Transactions Data Sets** de la División de Análisis Económico
y Riesgo (DERA) de la SEC. Son ficheros trimestrales ya aplanados a TSV, lo que
evita descargar y parsear cientos de miles de XML individuales.

Verificado el 2026-09-02: el trimestre 2024Q1 pesa 13,8 MB y contiene ocho
tablas. Cobertura desde 2006Q1.

A diferencia del endpoint de Yahoo que se usa para precios, **esta sí es una
fuente oficial y estable**, publicada por el propio regulador y documentada.

## Lo único que se conserva

Del total de movimientos no derivados solo interesa el código **P**, compra en
mercado abierto. Reparto medido sobre los 111.404 movimientos de 2024Q1:

    F  24,7 %   entrega de acciones para pagar impuestos
    S  24,4 %   venta
    A  23,0 %   concesión de la empresa
    M  15,8 %   ejercicio de opciones
    P   5,3 %   COMPRA EN MERCADO ABIERTO
    resto 6,8 %

El 63,5 % (F, A, M) es mecánica de retribución: le ocurre al directivo aunque
esté dormido y no expresa ninguna opinión sobre el precio. Contar
"transacciones de insiders" sin separar por código es medir el calendario de
nóminas de la empresa.

Las ventas se descartan aunque sean casi una cuarta parte: un insider vende
para diversificar, pagar impuestos o comprarse una casa, y a menudo por
calendario automático (planes 10b5-1). Comprar con dinero propio pudiendo no
hacerlo tiene muchas menos lecturas.

## La fecha que se usa, y por qué no la otra

`TRANS_DATE` es el día en que el insider compró. **No es utilizable**: nadie de
fuera lo sabía. El Form 4 se presenta hasta dos días hábiles después, y solo
entonces la información es pública.

Se conserva `presentado` (`FILING_DATE`) como la fecha accionable. El dataset
da fecha sin hora y muchos Form 4 entran después del cierre, así que quien
consuma esto debe entrar en la **apertura de la sesión siguiente**, nunca en la
misma. Misma disciplina que `filed` frente a `end` en los XBRL.

Se guarda también `transaccion` para poder medir el desfase, que es un dato
interesante por sí mismo, pero no para operar con él.
"""

from __future__ import annotations

import zipfile
from datetime import date
from io import BytesIO
from pathlib import Path

import httpx
import polars as pl

from core.data.binance_dumps import DescargaError
from core.obs.logging import get_logger

log = get_logger(__name__)

BASE = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets"
SOURCE = "sec_dera_form345"

import os

#: La SEC exige identificarse con algo que permita contactar, pero el correo
#: no se incrusta en el código: en un repositorio público acabaría en los
#: rastreadores de spam y quedaría en el historial de git para siempre.
#: Se toma de MR_SEC_USER_AGENT, y el valor por defecto es suficiente para
#: que funcione sin configurar nada.
USER_AGENT = os.getenv("MR_SEC_USER_AGENT", "MoonRocket research contact@example.com")

#: La SEC exige identificarse con algo que permita contactar. Sin esto devuelve
#: 403 y con un User-Agent genérico de navegador puede bloquear por abuso.
_CABECERAS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

#: Compra en mercado abierto. El resto de códigos son retribución, ventas o
#: movimientos exentos, y ninguno contiene una decisión sobre el precio.
CODIGO_COMPRA = "P"


def url_trimestre(anio: int, trimestre: int) -> str:
    return f"{BASE}/{anio}q{trimestre}_form345.zip"


def descargar_trimestre(
    anio: int, trimestre: int, cache_dir: Path, *, cliente: httpx.Client | None = None
) -> Path:
    """Descarga un trimestre y lo deja en caché. Si ya está, no vuelve a pedirlo.

    Los ficheros son inmutables una vez publicados —la SEC no reescribe
    trimestres cerrados—, así que la caché no necesita fecha de caducidad.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    destino = cache_dir / f"{anio}q{trimestre}_form345.zip"
    if destino.exists() and destino.stat().st_size > 0:
        return destino

    propio = cliente is None
    cliente = cliente or httpx.Client(timeout=120, follow_redirects=True)
    try:
        r = cliente.get(url_trimestre(anio, trimestre), headers=_CABECERAS)
        if r.status_code == 404:
            raise DescargaError(f"trimestre no publicado: {anio}Q{trimestre}")
        r.raise_for_status()
        if not r.content.startswith(b"PK"):
            raise DescargaError(f"{anio}Q{trimestre}: la respuesta no es un ZIP")
        destino.write_bytes(r.content)
    finally:
        if propio:
            cliente.close()
    return destino


def _fecha(col: str) -> pl.Expr:
    # El dataset usa "28-FEB-2024". El mes viene en mayúsculas y en inglés, así
    # que se normaliza antes de convertir: con la configuración regional
    # española el parseo directo falla en mayo, agosto y diciembre.
    return (
        pl.col(col)
        .str.strip_chars()
        .str.to_titlecase()
        .str.to_date("%d-%b-%Y", strict=False)
    )


def parsear(ruta_zip: Path) -> pl.DataFrame:
    """Extrae las compras en mercado abierto de un trimestre.

    Une las tres tablas necesarias: la transacción (qué y cuándo), la
    presentación (cuándo se hizo pública y de qué empresa) y el declarante
    (quién y con qué cargo).
    """
    with zipfile.ZipFile(ruta_zip) as z:
        def leer(nombre: str, columnas: list[str]) -> pl.DataFrame:
            with z.open(nombre) as f:
                return pl.read_csv(
                    BytesIO(f.read()),
                    separator="\t",
                    columns=columnas,
                    schema_overrides={c: pl.String for c in columnas},
                    quote_char=None,          # hay comillas sueltas en nombres
                    truncate_ragged_lines=True,
                    infer_schema_length=0,
                )

        trans = leer(
            "NONDERIV_TRANS.tsv",
            ["ACCESSION_NUMBER", "NONDERIV_TRANS_SK", "TRANS_DATE", "TRANS_CODE",
             "TRANS_SHARES", "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD"],
        )
        subm = leer(
            "SUBMISSION.tsv",
            ["ACCESSION_NUMBER", "FILING_DATE", "ISSUERCIK", "ISSUERTRADINGSYMBOL"],
        )
        duenos = leer(
            "REPORTINGOWNER.tsv",
            ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"],
        )

    compras = trans.filter(
        (pl.col("TRANS_CODE") == CODIGO_COMPRA)
        # "A" de adquirida. Una compra que figure como disposición es un error
        # de quien rellenó el formulario, y son raras pero existen.
        & (pl.col("TRANS_ACQUIRED_DISP_CD") == "A")
    )
    if compras.is_empty():
        return _vacio()

    df = (
        compras.join(subm, on="ACCESSION_NUMBER", how="inner")
        .join(duenos, on="ACCESSION_NUMBER", how="inner")
        .with_columns(
            pl.col("ISSUERTRADINGSYMBOL").str.strip_chars().str.to_uppercase().alias("activo"),
            pl.col("ISSUERCIK").str.strip_chars().str.zfill(10).alias("cik_empresa"),
            pl.col("ACCESSION_NUMBER").alias("accession"),
            pl.col("NONDERIV_TRANS_SK").alias("linea"),
            pl.col("RPTOWNERCIK").str.strip_chars().str.zfill(10).alias("cik_insider"),
            pl.col("RPTOWNER_RELATIONSHIP").fill_null("").alias("relacion"),
            pl.col("RPTOWNER_TITLE").fill_null("").alias("titulo"),
            _fecha("FILING_DATE").alias("presentado"),
            _fecha("TRANS_DATE").alias("transaccion"),
            pl.col("TRANS_SHARES").cast(pl.Float64, strict=False).alias("acciones"),
            pl.col("TRANS_PRICEPERSHARE").cast(pl.Float64, strict=False).alias("precio"),
            pl.lit(SOURCE).alias("source"),
        )
    )

    antes = df.height
    df = df.filter(
        pl.col("activo").is_not_null()
        & (pl.col("activo").str.len_chars() > 0)
        # Hay filas con símbolo "NONE" o "N/A" cuando la empresa no cotiza.
        & (~pl.col("activo").is_in(["NONE", "N/A", "NA", "-"]))
        & pl.col("presentado").is_not_null()
        & pl.col("acciones").is_not_null()
        & (pl.col("acciones") > 0)
        # Sin precio no se puede saber cuánto puso el insider. Ocurre en
        # compras informadas por rangos, que no son valorables.
        & pl.col("precio").is_not_null()
        & (pl.col("precio") > 0)
        # Sin fecha de transacción no se puede medir el desfase con la
        # publicación, que es lo que distingue una convicción reciente de una
        # declarada dos años tarde. Una fila así parece utilizable y no lo es,
        # así que se descarta en vez de dejarla pasar con un nulo dentro.
        & pl.col("transaccion").is_not_null()
        # Publicado antes de ocurrir es imposible: son erratas de quien rellenó
        # el formulario. En PAYX alguien tecleó agosto en vez de febrero. Son 3
        # de 8.136 en 2024Q1, pero una fecha futura envenenaría el análisis
        # justo por el lado del look-ahead.
        & (pl.col("transaccion") <= pl.col("presentado"))
    )
    if df.height < antes:
        log.info(
            "compras descartadas por datos incompletos",
            extra={"descartadas": antes - df.height, "total": antes},
        )

    return (
        df.with_columns(
            (pl.col("acciones") * pl.col("precio")).alias("valor"),
            (pl.col("presentado") - pl.col("transaccion")).dt.total_days().alias("desfase"),
        )
        .select(
            "activo", "cik_empresa", "accession", "linea", "cik_insider",
            "relacion", "titulo", "presentado", "transaccion", "desfase",
            "acciones", "precio", "valor", "source",
        )
        .unique(subset=["accession", "linea", "cik_insider"])
        .sort("presentado")
    )


def _vacio() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "activo": pl.String, "cik_empresa": pl.String, "accession": pl.String,
            "linea": pl.String, "cik_insider": pl.String, "relacion": pl.String,
            "titulo": pl.String, "presentado": pl.Date, "transaccion": pl.Date,
            "desfase": pl.Int64, "acciones": pl.Float64, "precio": pl.Float64,
            "valor": pl.Float64, "source": pl.String,
        }
    )


def trimestres(desde: date, hasta: date) -> list[tuple[int, int]]:
    """Lista de (año, trimestre) que cubre el intervalo, ambos incluidos."""
    salida = []
    a, t = desde.year, (desde.month - 1) // 3 + 1
    while (a, t) <= (hasta.year, (hasta.month - 1) // 3 + 1):
        salida.append((a, t))
        a, t = (a + 1, 1) if t == 4 else (a, t + 1)
    return salida
