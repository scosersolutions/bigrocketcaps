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
                             "peor_mes_bps", "meses", "estimaciones_absurdas_pct"}

    def test_sin_diagnostico_la_columna_existe_pero_esta_vacia(self):
        """Que falte el dato y que el dato sea cero no son lo mismo."""
        f = H.tabla_de_valores({"A": {"n": 1, "mediana_bps": 1.0,
                                      "p90_bps": 2.0, "max_bps": 3.0}})
        assert f[0]["estimaciones_absurdas_pct"] is None

    def test_el_diagnostico_viaja_con_su_valor(self):
        f = H.tabla_de_valores(
            {"A": {"n": 1, "mediana_bps": 1.0, "p90_bps": 2.0, "max_bps": 3.0}},
            {"A": {"negativos_pct": 42.4, "fuera_de_zona_buena": True}})
        assert f[0]["estimaciones_absurdas_pct"] == 42.4


class TestEnCristianoHorquilla:
    def _resumen(self, **medianas):
        return {k: {"n": 84, "mediana_bps": v, "p90_bps": v * 2, "max_bps": v * 4}
                for k, v in medianas.items()}

    def test_el_umbral_es_la_horquilla_entera_en_por_ciento(self):
        """Ida y vuelta se paga media horquilla al entrar y media al salir."""
        f = H.lectura_por_valor(self._resumen(AAPL=49.14))
        assert f[0]["cuesta_entrar_y_salir_pct"] == 0.49
        assert f[0]["hay_que_ganar_mas_de"] == "0.49 % por operacion"

    def test_ordena_de_barato_a_caro(self):
        f = H.lectura_por_valor(self._resumen(SOUN=228.8, AAPL=49.1))
        assert [x["valor"] for x in f] == ["AAPL", "SOUN"]

    def test_la_lectura_cambia_con_el_coste(self):
        f = H.lectura_por_valor(self._resumen(A=40.0, B=100.0, C=300.0))
        assert "baratos" in f[0]["lectura"]
        assert "caro" in f[1]["lectura"]
        assert "carisimo" in f[2]["lectura"]


class TestEnCristianoVeredicto:
    class _R:
        n = 3852
        n_empresas = 671
        exceso_medio_pct = 0.2013
        por_tramo = {"2018-2021": 0.464, "2022-2024": -0.17}

    def test_contesta_que_no_dice_en_que_invertir(self):
        """La pregunta que de verdad se hace cualquiera, contestada donde se ve."""
        filas = J.en_cristiano(self._R(), True, 69.5)
        invertir = next(f for f in filas if "invierta" in f["pregunta"])
        assert invertir["respuesta"].startswith("En ninguna")

    def test_el_porque_enumera_los_motivos_reales(self):
        filas = J.en_cristiano(self._R(), True, 69.5)
        porque = filas[-1]["respuesta"]
        assert "contrario" in porque
        assert "30 %" in porque          # 100 - 69.5, redondeado
        assert "cambia de signo" in porque

    def test_una_hipotesis_que_pasa_todo_no_se_anuncia_como_ganadora(self):
        class Buena(self._R):
            exceso_medio_pct = -0.5
            por_tramo = {"2018-2021": -0.4, "2022-2024": -0.6}
        filas = J.en_cristiano(Buena(), False, 99.0)
        assert "no es lo mismo que" in filas[-1]["respuesta"]
