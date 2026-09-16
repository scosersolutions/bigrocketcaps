"""CostesPorAccion tenia que declarar si el spread esta medido, igual que sus
hermanas CostesPorcentuales y CostesLibro: sin ese campo, `validar()` no puede
distinguir un coste de acciones medido de uno supuesto.
"""
from __future__ import annotations

from core.quant.costes import CostesPorAccion


class TestCostesPorAccion:
    def test_por_defecto_el_spread_es_supuesto(self):
        c = CostesPorAccion()
        assert c.spread_medido is False
        assert c.origen_spread == "supuesto"

    def test_declara_spread_medido_con_su_origen(self):
        c = CostesPorAccion(spread_bps=4.2, spread_medido=True,
                            origen_spread="tiingo iex p90, 2026-09-16")
        assert c.spread_medido is True
        assert c.origen_spread == "tiingo iex p90, 2026-09-16"

    def test_sigue_cobrando_con_el_spread_medido(self):
        """El campo es declarativo: no debe alterar el calculo de costes."""
        a = CostesPorAccion(spread_bps=4.2)
        b = CostesPorAccion(spread_bps=4.2, spread_medido=True, origen_spread="x")
        assert a.slippage(10_000.0) == b.slippage(10_000.0)
        assert a.comision(10_000.0) == b.comision(10_000.0)
