"""Los dos numeros que resumen la horquilla, y quien manda si difieren.

No toca Tiingo: lo que se prueba es lo que pasa DESPUES de las instantaneas,
que es lo que acaba en el modelo de costes y en la serie del Centro de Control.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import calibrar_costes_us as C  # noqa: E402
import medir_spread_us as M  # noqa: E402


def _activos(**p90s: float) -> dict[str, dict]:
    return {t: {"n": 60, "mediana_bps": v / 2, "p90_bps": v, "max_bps": v * 2}
            for t, v in p90s.items()}


class TestAgregados:
    def test_la_mediana_y_el_maximo_de_los_p90(self):
        a = M.agregados(_activos(AAPL=1.0, MSFT=3.0, CLOV=20.0))
        assert a == {"mediana_de_p90s": 3.0, "maximo_de_p90s": 20.0}

    def test_con_un_solo_activo_los_dos_son_el_suyo(self):
        assert M.agregados(_activos(AAPL=2.5)) == {
            "mediana_de_p90s": 2.5, "maximo_de_p90s": 2.5}

    def test_agrega_el_p90_y_no_la_mediana_de_cada_activo(self):
        """`mediana_bps` es la mitad de `p90_bps` en esta muestra: si se
        colara el campo equivocado, los numeros saldrian a la mitad."""
        a = M.agregados(_activos(AAPL=4.0, MSFT=8.0))
        assert a["mediana_de_p90s"] == 6.0

    def test_un_numero_de_activos_par_promedia_los_dos_centrales(self):
        a = M.agregados(_activos(A=1.0, B=2.0, C=4.0, D=9.0))
        assert a["mediana_de_p90s"] == 3.0


class TestCalibrarLeeLoEscrito:
    def test_manda_el_campo_escrito_por_la_medicion(self):
        """Si la medicion ya dijo su numero, el calibrador no lo discute."""
        r = C.calibrar({"activos": _activos(A=1.0, B=3.0),
                        "mediana_de_p90s": 99.0, "maximo_de_p90s": 123.0})
        assert r["mediana_de_p90s"] == 99.0
        assert r["modelo_principal"].spread_bps == 99.0
        assert r["maximo_de_p90s"] == 123.0

    def test_una_medicion_sin_el_campo_se_sigue_pudiendo_leer(self):
        r = C.calibrar({"activos": _activos(A=1.0, B=3.0)})
        assert r["mediana_de_p90s"] == 2.0
        assert r["maximo_de_p90s"] == 3.0

    def test_el_origen_sale_del_fichero_y_no_esta_escrito_a_mano(self):
        """Poner la fuente a mano firmaria un origen falso en el modelo de
        costes el dia que la horquilla cambie de procedencia. Y ha cambiado."""
        r = C.calibrar({"activos": _activos(A=5.0), "fuente": "corwin-schultz"})
        assert r["modelo_principal"].spread_medido is True
        assert "corwin-schultz" in r["modelo_principal"].origen_spread

    def test_una_medicion_sin_fuente_lo_dice_en_vez_de_callarselo(self):
        r = C.calibrar({"activos": _activos(A=5.0)})
        assert "sin declarar" in r["modelo_principal"].origen_spread
