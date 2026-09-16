"""El anillo que reporta al Centro de Control.

Lo que se prueba es lo que decide QUE se envia: que numero es una metrica y
que resumen sale de un resultado. La tuberia HTTP es del SDK, que es una copia
verbatim y se prueba en su repositorio.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import con_control as C  # noqa: E402


class TestMetricas:
    def test_los_numeros_de_primer_nivel_son_metricas(self):
        m = C.metricas({"v": {"n": 3852, "exceso_medio_pct": 0.2013}})
        assert m == {"v.n": 3852, "v.exceso_medio_pct": 0.2013}

    def test_un_booleano_no_es_una_medida(self):
        """`refutada = 1` en una grafica de serie temporal mentiria."""
        m = C.metricas({"v": {"refutada_sin_coste": True, "n": 10}})
        assert m == {"v.n": 10}

    def test_lo_anidado_no_se_aplana_a_la_fuerza(self):
        m = C.metricas({"v": {"por_tramo": {"2018-2021": 0.464}, "t": 1.4}})
        assert m == {"v.t": 1.4}

    def test_dos_resultados_no_se_pisan(self):
        m = C.metricas({"a": {"n": 1}, "b": {"n": 2}})
        assert m == {"a.n": 1, "b.n": 2}

    def test_un_resultado_que_no_es_objeto_se_ignora(self):
        assert C.metricas({"a": [1, 2, 3], "b": 7}) == {}


class TestResumen:
    def test_el_hueco_se_rellena(self):
        r = C.rellenar("exceso {v.exceso_medio_pct}% en {v.n} eventos",
                       {"v": {"exceso_medio_pct": 0.2013, "n": 3852}})
        assert r == "exceso 0.2013% en 3852 eventos"

    def test_un_campo_que_no_existe_se_queda_a_la_vista(self):
        """Ensena que el campo se llama de otra forma. `None` lo esconderia."""
        r = C.rellenar("percentil {v.percentil}", {"v": {"percentil_azar": 69.5}})
        assert r == "percentil {v.percentil}"

    def test_una_clave_que_no_se_publico_tampoco_se_inventa(self):
        assert C.rellenar("{otro.campo}", {"v": {"campo": 1}}) == "{otro.campo}"

    def test_un_resumen_sin_huecos_pasa_tal_cual(self):
        assert C.rellenar("horquilla medida", {}) == "horquilla medida"
