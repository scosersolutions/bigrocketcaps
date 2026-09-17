"""Lo que se publica tiene que poder leerse sin el codigo al lado.

El Centro de Control pinta tablas con LISTAS de objetos y volcados crudos con
los diccionarios. Un `{"2018-2021": 0.464}` en pantalla no dice ni que es un
por ciento ni de que lado cae, asi que lo que viaja son filas.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import estimar_horquilla as H  # noqa: E402
import juzgar_b1 as J  # noqa: E402


class TestTramos:
    def test_cada_tramo_es_una_fila_con_su_lectura(self):
        f = J.tabla_de_tramos({"2018-2021": 0.464, "2022-2024": -0.17}, "corto")
        assert [x["periodo"] for x in f] == ["2018-2021", "2022-2024"]
        assert f[0]["exceso_medio"] == "+0.464 %"
        assert f[1]["exceso_medio"] == "-0.170 %"

    def test_para_una_hipotesis_corta_el_negativo_es_el_que_acierta(self):
        f = J.tabla_de_tramos({"a": 0.5, "b": -0.5}, "corto")
        assert f[0]["lectura"].startswith("en contra")
        assert f[1]["lectura"].startswith("a favor")

    def test_para_una_larga_es_al_reves(self):
        f = J.tabla_de_tramos({"a": 0.5, "b": -0.5}, "largo")
        assert f[0]["lectura"].startswith("a favor")
        assert f[1]["lectura"].startswith("en contra")


class TestCriterios:
    def test_el_veredicto_va_delante_y_el_nombre_sin_barras_bajas(self):
        f = J.tabla_de_criterios({
            "1_exceso_negativo": {"supera": False, "valor": "+0.2%",
                                  "refuta_si": "exceso >= 0"},
            "5_al_menos_20_empresas": {"supera": True, "valor": "671 empresas",
                                       "refuta_si": "< 20"},
        })
        assert f[0]["resultado"] == "REFUTA"
        assert f[0]["criterio"] == "Exceso negativo"
        assert f[1]["resultado"] == "PASA"
        assert f[1]["criterio"] == "Al menos 20 empresas"


class TestValoresDeLaHorquilla:
    def test_es_una_lista_ordenada_de_barato_a_caro(self):
        f = H.tabla_de_valores({
            "SOUN": {"n": 33, "mediana_bps": 228.8, "p90_bps": 396.5, "max_bps": 615.3},
            "AAPL": {"n": 84, "mediana_bps": 49.1, "p90_bps": 75.5, "max_bps": 208.8},
        })
        assert [x["valor"] for x in f] == ["AAPL", "SOUN"]
        assert f[0]["horquilla_tipica_bps"] == 49.1
        assert f[0]["meses"] == 84

    def test_las_columnas_se_llaman_como_lo_que_son(self):
        f = H.tabla_de_valores({"A": {"n": 1, "mediana_bps": 1.0,
                                      "p90_bps": 2.0, "max_bps": 3.0}})
        assert set(f[0]) == {"valor", "horquilla_tipica_bps", "mal_mes_bps",
                             "peor_mes_bps", "meses"}
