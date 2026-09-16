"""Contraste contra el azar de un estudio de eventos.

No se prueba que la distribucion nula sea estadisticamente perfecta -eso es
trabajo de `scripts/calibrar_juez.py`-, solo que el percentil se calcula en la
direccion correcta y que los bordes no revientan.
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from brc.estudio.azar import percentil_contra_azar

SESIONES = [date(2024, 1, 1) + timedelta(days=d) for d in range(120)
            if (date(2024, 1, 1) + timedelta(days=d)).weekday() < 5]


def _mercado_plano(tickers: int = 10) -> pl.DataFrame:
    filas = []
    for t in range(tickers):
        for f in SESIONES:
            filas.append({"activo": f"T{t}", "fecha": f, "close": 100.0})
    return pl.DataFrame(filas)


class TestPercentilContraAzar:
    def test_direccion_invalida_falla(self):
        with pytest.raises(ValueError, match="corto"):
            percentil_contra_azar(10, _mercado_plano(), horizonte=5,
                                  exceso_real=-1.0, direccion="bajista")

    def test_sin_eventos_falla(self):
        with pytest.raises(ValueError):
            percentil_contra_azar(0, _mercado_plano(), horizonte=5,
                                  exceso_real=-1.0, direccion="corto")

    def test_sin_precios_no_revienta(self):
        assert percentil_contra_azar(
            10, pl.DataFrame({"activo": [], "fecha": [], "close": []}),
            horizonte=5, exceso_real=-1.0, direccion="corto") == 0.0

    def test_un_exceso_corto_absurdamente_negativo_bate_casi_todo_el_azar(self):
        """En un mercado plano el azar da ~0 % de exceso: -999 % lo bate todo."""
        p = percentil_contra_azar(20, _mercado_plano(), horizonte=5,
                                  exceso_real=-999.0, direccion="corto",
                                  simulaciones=50)
        assert p >= 95.0

    def test_un_exceso_corto_positivo_no_bate_el_azar(self):
        """La hipotesis predice negativo: un resultado muy positivo no supera nada."""
        p = percentil_contra_azar(20, _mercado_plano(), horizonte=5,
                                  exceso_real=999.0, direccion="corto",
                                  simulaciones=50)
        assert p <= 5.0

    def test_es_reproducible_con_la_misma_semilla(self):
        kwargs = dict(n_eventos=15, precios=_mercado_plano(), horizonte=5,
                      exceso_real=-2.0, direccion="corto", simulaciones=30,
                      semilla=7)
        assert percentil_contra_azar(**kwargs) == percentil_contra_azar(**kwargs)
