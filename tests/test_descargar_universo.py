"""La tuberia de descarga: que guarde, que no repita y que no mienta.

No toca EDGAR. Lo que se prueba aqui es lo que pasa DESPUES de la respuesta,
que es donde un fallo se disfraza de hueco de datos.
"""
from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import descargar_universo as D  # noqa: E402
from brc.datos.esquema import DDL  # noqa: E402


@pytest.fixture()
def con(tmp_path):
    c = duckdb.connect(str(tmp_path / "t.duckdb"))
    c.execute(DDL)
    yield c
    c.close()


def _df(n: int, trimestre: str = "CY2024Q1I") -> pl.DataFrame:
    return pl.DataFrame({
        "cik": list(range(1, n + 1)),
        "nombre": [f"EMPRESA {i}" for i in range(1, n + 1)],
        "fin": [date(2024, 3, 31)] * n,
        "accn": [f"0000000000-24-{i:06d}" for i in range(1, n + 1)],
        "acciones": [1000.0 * i for i in range(1, n + 1)],
        "trimestre": [trimestre] * n,
    })


def test_guarda_lo_que_le_dan(con):
    assert D.guardar(con, _df(3), datetime.now(UTC)) == 3
    assert con.execute("SELECT COUNT(*) FROM acciones_circulacion").fetchone()[0] == 3


def test_vuelve_a_bajar_el_mismo_trimestre_sin_duplicar(con):
    """La clave es (cik, trimestre): reejecutar no infla la tabla."""
    D.guardar(con, _df(3), datetime.now(UTC))
    D.guardar(con, _df(3), datetime.now(UTC))
    assert con.execute("SELECT COUNT(*) FROM acciones_circulacion").fetchone()[0] == 3


def test_sabe_que_trimestres_ya_tiene(con):
    D.guardar(con, _df(2, "CY2024Q1I"), datetime.now(UTC))
    D.guardar(con, _df(2, "CY2024Q2I"), datetime.now(UTC))
    assert D.ya_descargados(con) == {"CY2024Q1I", "CY2024Q2I"}


def test_un_trimestre_vacio_no_cuenta_como_descargado(con):
    """Si no, un fallo de red quedaria como 'ese trimestre no tenia empresas'."""
    vacio = _df(0)
    assert D.guardar(con, vacio, datetime.now(UTC)) == 0
    assert D.ya_descargados(con) == set()


def test_se_guarda_el_accn_porque_sin_el_el_dato_no_es_accionable(con):
    D.guardar(con, _df(1), datetime.now(UTC))
    accn = con.execute("SELECT accn FROM acciones_circulacion").fetchone()[0]
    assert accn == "0000000000-24-000001"


def test_no_se_descarga_con_un_user_agent_de_ejemplo(monkeypatch):
    for falso in ("MoonRocket/0.1 (tu-email@ejemplo.com)",
                  "BRC/0.1 (algo@ejemplo.com)", "sin-arroba"):
        monkeypatch.setenv("MR_SEC_USER_AGENT", falso)
        with pytest.raises(Exception, match="correo real"):
            D._user_agent()
