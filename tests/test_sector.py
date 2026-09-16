"""Sector de cada empresa: sustituto de coste 0 de GICS.

`sector_de_sic` es pura y se prueba sin red. `obtener_sic` solo hace el bucle
HTTP -mismo patron que `presentaciones.py`- y no se prueba con red real aqui.
"""
from __future__ import annotations

from brc.datos.sector import OTRO, SECTORES, _extraer_sic, sector_de_sic


class TestSectorDeSic:
    def test_un_sic_de_tecnologia_cae_en_buseq(self):
        assert sector_de_sic(3571) == "BusEq"  # Electronic Computers, ver AAPL

    def test_un_sic_de_petroleo_cae_en_enrgy(self):
        assert sector_de_sic(1311) == "Enrgy"

    def test_un_sic_de_banca_cae_en_money(self):
        assert sector_de_sic(6021) == "Money"

    def test_el_borde_inferior_de_un_rango_cuenta(self):
        assert sector_de_sic(100) == "NoDur"

    def test_el_borde_superior_de_un_rango_cuenta(self):
        assert sector_de_sic(999) == "NoDur"

    def test_justo_fuera_del_rango_no_cuenta(self):
        assert sector_de_sic(1000) == OTRO

    def test_sin_sic_cae_en_otro(self):
        assert sector_de_sic(None) == OTRO

    def test_un_sic_no_clasificado_cae_en_otro(self):
        """1000-1199 es mineria metalica: no esta en ninguno de los 11 cubos
        nombrados, y por eso Fama-French lo deja en el cubo 12."""
        assert sector_de_sic(1040) == OTRO


class TestSectores:
    def test_son_doce(self):
        assert len(SECTORES) == 12

    def test_no_hay_nombres_repetidos(self):
        assert len(set(SECTORES)) == len(SECTORES)


class TestExtraerSic:
    def test_extrae_cik_y_sic(self):
        cruda = {"cik": 320193, "sic": "3571", "sicDescription": "Electronic Computers"}
        assert _extraer_sic(cruda) == (320193, 3571)

    def test_sin_sic_devuelve_none_en_el_campo(self):
        cruda = {"cik": 320193, "sic": ""}
        assert _extraer_sic(cruda) == (320193, None)

    def test_sin_cik_no_hay_nada_que_extraer(self):
        assert _extraer_sic({"sic": "3571"}) is None
