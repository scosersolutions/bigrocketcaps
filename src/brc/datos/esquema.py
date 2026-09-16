"""Tablas propias de Big Rocket Caps, encima de las que trae el núcleo.

El núcleo aporta `ohlcv`, `insiders`, `eventos`, `fundamentales` y
`experimentos`, que sirven igual aquí. Lo que falta es lo que hace falta para
reconstruir el universo y para fechar cada dato por cuándo SE SUPO.

## Las tres fechas, y por qué no sobra ninguna

Cada hecho tiene hasta tres:

- `fin` / `periodo`: a qué fecha se refiere el dato. **No es accionable**: el
  cierre del primer trimestre no se conoce el 31 de marzo.
- `aceptado` / `publicado`: cuándo lo recibió la SEC y se hizo público. **Es la
  única con la que se opera.**
- `ingerido`: cuándo lo bajamos nosotros. Sirve para depurar la tubería, no
  para decidir nada.

El esquema del núcleo ya lleva esa distinción en `fundamentales` e `insiders`,
con un comentario que dice que usar `fin` sería look-ahead puro. Aquí se
mantiene, y se extiende a lo nuevo.

## Por qué `acciones_circulacion` guarda `accn`

La API `frames` de XBRL da el valor y la fecha a la que se refiere, pero no
cuándo se publicó. Lo que sí da es `accn`, el número de la presentación de la
que sale. Cruzándolo con `presentaciones` se obtiene `aceptado`, que es la
fecha buena. Sin ese cruce, el universo parecería conocerse antes de tiempo, y
un universo con look-ahead contamina cualquier estudio transversal que lo use.
"""
from __future__ import annotations

DDL = """
-- Presentaciones de EDGAR. Es el índice con el que se fecha todo lo demás:
-- `aceptado` lleva hora porque un 8-K presentado a las 16:05 no se pudo
-- operar en la sesión de ese día.
CREATE TABLE IF NOT EXISTS presentaciones (
    accession   VARCHAR NOT NULL,
    cik         BIGINT  NOT NULL,
    formulario  VARCHAR NOT NULL,
    items       VARCHAR,
    aceptado    TIMESTAMPTZ NOT NULL,
    presentado  DATE    NOT NULL,
    periodo     DATE,
    ingerido    TIMESTAMPTZ NOT NULL,
    source      VARCHAR NOT NULL,
    PRIMARY KEY (accession)
);

-- Acciones en circulación declaradas, tal como las da la API `frames`.
-- `accn` es lo que permite fecharlas por publicación; sin él, el dato no es
-- accionable y NO debe usarse.
CREATE TABLE IF NOT EXISTS acciones_circulacion (
    cik         BIGINT  NOT NULL,
    nombre      VARCHAR,
    trimestre   VARCHAR NOT NULL,
    fin         DATE    NOT NULL,
    accn        VARCHAR NOT NULL,
    acciones    DOUBLE  NOT NULL,
    ingerido    TIMESTAMPTZ NOT NULL,
    source      VARCHAR NOT NULL,
    PRIMARY KEY (cik, trimestre)
);

-- Universo point-in-time ya resuelto: qué decil de capitalización ocupaba cada
-- empresa cada día, con los datos disponibles ESE día.
--
-- Se materializa en vez de calcularse al vuelo por una razón de método: así
-- queda un registro de qué universo vio cada estudio. Recalcularlo con datos
-- de hoy daría un universo distinto, y nadie se enteraría.
CREATE TABLE IF NOT EXISTS universo_decil (
    cik            BIGINT NOT NULL,
    fecha          DATE   NOT NULL,
    capitalizacion DOUBLE NOT NULL,
    decil          INTEGER NOT NULL,
    version        VARCHAR NOT NULL,
    PRIMARY KEY (cik, fecha, version)
);

-- Ticker <-> CIK, que no es una correspondencia estable: los tickers se
-- reciclan. Se guarda con la fecha en que se comprobó para poder detectar el
-- día en que un ticker cambió de dueño.
CREATE TABLE IF NOT EXISTS tickers (
    ticker      VARCHAR NOT NULL,
    cik         BIGINT  NOT NULL,
    comprobado  DATE    NOT NULL,
    source      VARCHAR NOT NULL,
    PRIMARY KEY (ticker, cik, comprobado)
);
"""

#: Tablas del núcleo que aquí NO se usan: son de perpetuos. Se declaran para
#: que su ausencia sea una decisión escrita y no un olvido.
SIN_USO = ("funding", "open_interest", "metrics", "book_depth")
