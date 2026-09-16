"""El universo point-in-time, que es donde se cuela el survivorship bias.

MoonRocket ya lo sufrio: una lista fija de 110 simbolos "con historia desde
2021-09" tenia CERO muertos, cuando el universo real tenia 136 deslistados. El
sesgo lo habia metido el descargador.
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from brc.datos.universo import Trimestre, UniversoError, acciones_en_circulacion, deciles


class TestTrimestre:
    def test_la_clave_lleva_la_I_de_instantaneo(self):
        assert Trimestre(2024, 1).clave == "CY2024Q1I"

    def test_el_rango_cubre_los_cuatro_trimestres(self):
        r = Trimestre.rango(2012, 2013)
        assert len(r) == 8
        assert r[0].clave == "CY2012Q1I" and r[-1].clave == "CY2013Q4I"


class TestUserAgent:
    def test_sin_correo_real_no_se_pide_nada(self):
        """Con un UA inventado la SEC no avisa: bloquea."""
        with pytest.raises(UniversoError, match="correo de contacto"):
            acciones_en_circulacion(Trimestre(2024, 1), user_agent="MoonRocket/0.1")


class TestDeciles:
    """El universo a una fecha usa el ultimo dato PUBLICADO antes de ella."""

    @staticmethod
    def _caso():
        historico = pl.DataFrame({
            "cik": [1, 2, 3, 4],
            "publicado": [date(2014, 11, 5)] * 4,
            "acciones": [100.0, 200.0, 50.0, 10.0],
        })
        precios = pl.DataFrame({
            "cik": [1, 2, 3, 4],
            "fecha": [date(2015, 1, 2)] * 4,
            "close": [10.0, 20.0, 5.0, 1.0],
        })
        return historico, precios

    def test_la_capitalizacion_es_acciones_por_precio(self):
        h, p = self._caso()
        d = deciles(h, p).sort("cik")
        assert d["capitalizacion"].to_list() == [1000.0, 4000.0, 250.0, 10.0]

    def test_el_decil_se_calcula_dentro_de_cada_fecha(self):
        """Si no, una empresa pareceria grande en 2012 por el mercado de 2026."""
        h = pl.DataFrame({"cik": [1, 2], "publicado": [date(2011, 1, 1)] * 2,
                          "acciones": [100.0, 100.0]})
        p = pl.DataFrame({
            "cik": [1, 2, 1, 2],
            "fecha": [date(2015, 1, 2), date(2015, 1, 2), date(2020, 1, 2), date(2020, 1, 2)],
            "close": [100.0, 1.0, 1.0, 100.0],
        })
        d = deciles(h, p)
        en2015 = d.filter(pl.col("fecha") == date(2015, 1, 2)).sort("cik")
        en2020 = d.filter(pl.col("fecha") == date(2020, 1, 2)).sort("cik")
        assert en2015["decil"][0] > en2015["decil"][1]
        assert en2020["decil"][1] > en2020["decil"][0]

    def test_no_se_usa_un_dato_publicado_DESPUES_de_la_fecha(self):
        """El look-ahead entra por aqui: el dato de noviembre no existia en enero."""
        h = pl.DataFrame({"cik": [1], "publicado": [date(2015, 11, 5)],
                          "acciones": [100.0]})
        p = pl.DataFrame({"cik": [1], "fecha": [date(2015, 1, 2)], "close": [10.0]})
        assert deciles(h, p).is_empty()

    def test_el_hueco_del_cuarto_trimestre_se_rellena_con_el_ultimo_conocido(self):
        """Q4 trae un 44 % menos de empresas: 2.911 frente a 5.401 en Q1.

        No es un hueco de la descarga, es que las empresas de año fiscal en
        diciembre presentan el 10-K en el Q1 siguiente. Si el universo se
        construyera por trimestre, cada Q4 tendria solo empresas de calendario
        atipico.
        """
        h = pl.DataFrame({
            "cik": [1, 1, 2],
            # La 1 declara en Q3 y en el Q1 siguiente; en Q4 no declara nadie.
            "publicado": [date(2015, 8, 5), date(2016, 2, 20), date(2015, 8, 5)],
            "acciones": [100.0, 150.0, 200.0],
        })
        p = pl.DataFrame({"cik": [1, 2], "fecha": [date(2015, 12, 15)] * 2,
                          "close": [10.0, 10.0]})
        d = deciles(h, p).sort("cik")
        assert d.height == 2                      # nadie se cae en diciembre
        assert d["capitalizacion"][0] == 1000.0   # la cifra de agosto, no la de febrero

    def test_una_empresa_sin_precio_ese_dia_se_queda_fuera(self):
        """No cotizar no es cotizar a su ultimo precio conocido."""
        h = pl.DataFrame({"cik": [1, 2], "publicado": [date(2014, 1, 1)] * 2,
                          "acciones": [100.0, 100.0]})
        p = pl.DataFrame({"cik": [1], "fecha": [date(2015, 1, 2)], "close": [10.0]})
        assert deciles(h, p)["cik"].to_list() == [1]

    def test_una_empresa_muerta_sigue_en_el_universo_de_cuando_vivia(self):
        h = pl.DataFrame({"cik": [1, 99], "publicado": [date(2014, 1, 1)] * 2,
                          "acciones": [100.0, 100.0]})
        p = pl.DataFrame({
            "cik": [1, 99, 1],
            "fecha": [date(2015, 1, 2), date(2015, 1, 2), date(2020, 1, 2)],
            "close": [10.0, 10.0, 10.0],
        })
        d = deciles(h, p)
        assert 99 in d.filter(pl.col("fecha") == date(2015, 1, 2))["cik"].to_list()
        assert 99 not in d.filter(pl.col("fecha") == date(2020, 1, 2))["cik"].to_list()

    def test_sin_datos_devuelve_vacio_y_no_revienta(self):
        vacio = pl.DataFrame(schema={"cik": pl.Int64, "publicado": pl.Date,
                                     "acciones": pl.Float64})
        assert deciles(vacio, vacio).is_empty()
