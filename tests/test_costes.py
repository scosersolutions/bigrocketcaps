"""CostesPorAccion tenia que declarar si el spread esta medido, igual que sus
hermanas CostesPorcentuales y CostesLibro: sin ese campo, `validar()` no puede
distinguir un coste de acciones medido de uno supuesto.
"""
from __future__ import annotations

import pytest

from core.quant.costes import CostesError, CostesPorAccion


class TestCostesPorAccion:
    def test_por_defecto_el_spread_es_supuesto(self):
        c = CostesPorAccion()
        assert c.spread_medido is False
        assert c.origen_spread == "supuesto"

    def test_declara_spread_medido_con_su_origen(self):
        c = CostesPorAccion(spread_bps=4.2, procedencia="medido",
                            origen_spread="tiingo iex p90, 2026-09-16")
        assert c.spread_medido is True
        assert c.origen_spread == "tiingo iex p90, 2026-09-16"

    def test_un_spread_estimado_no_es_medido_pero_tampoco_inventado(self):
        """El tercer estado: pasa la puerta de `validar()` y no se hace pasar
        por una medicion."""
        c = CostesPorAccion(spread_bps=58.0, procedencia="estimado",
                            origen_spread="corwin-schultz sobre el rango diario")
        assert c.procedencia == "estimado"
        assert c.spread_medido is True

    def test_no_se_puede_decir_medido_y_supuesto_a_la_vez(self):
        """`spread_medido` se deriva: un modelo no puede pasar la puerta
        mintiendo, que es justo lo que la puerta existe para impedir."""
        c = CostesPorAccion(spread_bps=2.0, spread_medido=True)
        assert c.spread_medido is False
        assert c.procedencia == "supuesto"

    def test_una_procedencia_que_no_existe_se_rechaza(self):
        with pytest.raises(CostesError):
            CostesPorAccion(procedencia="inventado", origen_spread="x")

    def test_lo_que_no_es_supuesto_tiene_que_decir_de_donde_sale(self):
        with pytest.raises(CostesError):
            CostesPorAccion(spread_bps=4.2, procedencia="medido")

    def test_sigue_cobrando_con_el_spread_medido(self):
        """El campo es declarativo: no debe alterar el calculo de costes."""
        a = CostesPorAccion(spread_bps=4.2)
        b = CostesPorAccion(spread_bps=4.2, procedencia="medido", origen_spread="x")
        assert a.slippage(10_000.0) == b.slippage(10_000.0)
        assert a.comision(10_000.0) == b.comision(10_000.0)
