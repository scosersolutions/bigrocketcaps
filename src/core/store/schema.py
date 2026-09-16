"""Esquema de datos. Toda marca temporal es UTC con zona horaria explícita.

La mezcla de zonas horarias es el error más caro y más silencioso de un sistema
de trading, así que el esquema lo prohíbe en vez de confiar en la disciplina.
"""

from __future__ import annotations

from enum import StrEnum


class Mercado(StrEnum):
    CRYPTO_PERP = "crypto_perp"
    CRYPTO_SPOT = "crypto_spot"
    STOCK_US = "stock_us"


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


#: Minutos que dura cada vela. Sirve para detectar gaps.
DURACION_MINUTOS: dict[Timeframe, int] = {
    Timeframe.M1: 1,
    Timeframe.M5: 5,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 60,
    Timeframe.H4: 240,
    Timeframe.D1: 1440,
}

COLUMNAS_OHLCV = (
    "mercado",
    "activo",
    "timeframe",
    "ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trades",
    "source",
)

DDL = """
CREATE TABLE IF NOT EXISTS ohlcv (
    mercado   VARCHAR    NOT NULL,
    activo    VARCHAR    NOT NULL,
    timeframe VARCHAR    NOT NULL,
    ts        TIMESTAMPTZ NOT NULL,
    open      DOUBLE     NOT NULL,
    high      DOUBLE     NOT NULL,
    low       DOUBLE     NOT NULL,
    close     DOUBLE     NOT NULL,
    volume    DOUBLE     NOT NULL,
    trades    BIGINT,
    source    VARCHAR    NOT NULL,
    PRIMARY KEY (mercado, activo, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS funding (
    activo VARCHAR     NOT NULL,
    ts     TIMESTAMPTZ NOT NULL,
    rate   DOUBLE      NOT NULL,
    source VARCHAR     NOT NULL,
    PRIMARY KEY (activo, ts)
);

CREATE TABLE IF NOT EXISTS open_interest (
    activo VARCHAR     NOT NULL,
    ts     TIMESTAMPTZ NOT NULL,
    oi     DOUBLE      NOT NULL,
    source VARCHAR     NOT NULL,
    PRIMARY KEY (activo, ts)
);

-- Registro append-only de decisiones. Nunca se actualiza ni se borra:
-- el resultado se enlaza desde outcomes.
CREATE TABLE IF NOT EXISTS decisions (
    id            VARCHAR     PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL,
    mercado       VARCHAR     NOT NULL,
    activo        VARCHAR     NOT NULL,
    tipo          VARCHAR     NOT NULL,   -- signal | event_alert
    direccion     VARCHAR,                -- long | short | none
    entrada       DOUBLE,
    stop          DOUBLE,
    objetivo      DOUBLE,
    rr            DOUBLE,
    coste_ratio   DOUBLE,
    estrategia    VARCHAR,
    estrategia_v  VARCHAR,
    regimen       VARCHAR,
    estado        VARCHAR     NOT NULL,   -- emitida | vetada | caducada | ejecutada
    razon         VARCHAR,
    inputs_hash   VARCHAR     NOT NULL,
    payload       JSON
);

CREATE TABLE IF NOT EXISTS outcomes (
    decision_id     VARCHAR     PRIMARY KEY,
    ts_cierre       TIMESTAMPTZ NOT NULL,
    precio_entrada  DOUBLE,
    precio_salida   DOUBLE,
    r_multiple      DOUBLE,
    pnl_bruto       DOUBLE,
    costes          DOUBLE,
    pnl_neto        DOUBLE,
    slippage_real   DOUBLE,
    motivo_salida   VARCHAR,
    payload         JSON
);

-- Posicionamiento: interés abierto y ratios long/short. Ver binance_metrics.py.
CREATE TABLE IF NOT EXISTS metrics (
    activo            VARCHAR     NOT NULL,
    ts                TIMESTAMPTZ NOT NULL,
    open_interest     DOUBLE,
    open_interest_usd DOUBLE,
    top_ls_cuentas    DOUBLE,
    top_ls_posiciones DOUBLE,
    ls_cuentas        DOUBLE,
    taker_ls_volumen  DOUBLE,
    source            VARCHAR     NOT NULL,
    PRIMARY KEY (activo, ts)
);

-- Profundidad del libro de Binance, una fila por instantanea (~1/min) con las
-- diez bandas en columnas. Ver binance_book.py. Es el UNICO dato de libro real
-- del proyecto: `taker_ls_volumen` era un proxy, y el control interno de C7
-- demostro que ese proxy no aportaba nada.
CREATE TABLE IF NOT EXISTS book_depth (
    activo VARCHAR     NOT NULL,
    ts     TIMESTAMPTZ NOT NULL,
    bid_1  DOUBLE, bid_2 DOUBLE, bid_3 DOUBLE, bid_4 DOUBLE, bid_5 DOUBLE,
    ask_1  DOUBLE, ask_2 DOUBLE, ask_3 DOUBLE, ask_4 DOUBLE, ask_5 DOUBLE,
    source VARCHAR     NOT NULL,
    PRIMARY KEY (activo, ts)
);

-- Fundamentales de SEC EDGAR. `publicado` es la fecha en que el dato se hizo
-- publico: usar `fin` (fin del periodo contable) seria look-ahead puro.
CREATE TABLE IF NOT EXISTS fundamentales (
    activo     VARCHAR NOT NULL,
    cik        VARCHAR NOT NULL,
    concepto   VARCHAR NOT NULL,
    etiqueta   VARCHAR NOT NULL,
    fin        DATE    NOT NULL,
    publicado  DATE    NOT NULL,
    formulario VARCHAR,
    valor      DOUBLE,
    unidad     VARCHAR,
    source     VARCHAR NOT NULL,
    PRIMARY KEY (activo, concepto, fin, publicado)
);

-- Eventos corporativos de SEC EDGAR. `aceptado` lleva hora: decide en que
-- sesion puede reaccionar el mercado.
CREATE TABLE IF NOT EXISTS eventos (
    activo      VARCHAR     NOT NULL,
    cik         VARCHAR     NOT NULL,
    accession   VARCHAR     NOT NULL,
    formulario  VARCHAR     NOT NULL,
    clase       VARCHAR     NOT NULL,
    items       VARCHAR,
    aceptado    TIMESTAMPTZ NOT NULL,
    presentado  DATE        NOT NULL,
    periodo     DATE,
    tras_cierre BOOLEAN     NOT NULL,
    source      VARCHAR     NOT NULL,
    PRIMARY KEY (activo, accession)
);

-- Compras de insiders en mercado abierto (código P de los Forms 3/4/5).
-- `presentado` es la fecha accionable: `transaccion` es anterior y no era
-- pública. Se guardan las dos para poder medir el desfase, nunca para operar
-- con la segunda.
CREATE TABLE IF NOT EXISTS insiders (
    activo      VARCHAR NOT NULL,
    cik_empresa VARCHAR NOT NULL,
    accession   VARCHAR NOT NULL,
    linea       VARCHAR NOT NULL,
    cik_insider VARCHAR NOT NULL,
    relacion    VARCHAR,
    titulo      VARCHAR,
    presentado  DATE    NOT NULL,
    transaccion DATE,
    desfase     BIGINT,
    acciones    DOUBLE  NOT NULL,
    precio      DOUBLE  NOT NULL,
    valor       DOUBLE  NOT NULL,
    source      VARCHAR NOT NULL,
    PRIMARY KEY (accession, linea, cik_insider)
);

-- Toda configuración probada en backtest, para que el Deflated Sharpe sea real.
CREATE TABLE IF NOT EXISTS experimentos (
    id          VARCHAR     PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    estrategia  VARCHAR     NOT NULL,
    hipotesis   VARCHAR     NOT NULL,
    parametros  JSON        NOT NULL,
    metricas    JSON,
    notas       VARCHAR
);
"""
