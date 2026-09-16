"""El indice diario de EDGAR y el fechado por publicacion.

Aqui se decide si el universo es point-in-time o solo lo parece: un dato
fechado por el cierre contable, y no por cuando se publico, mete look-ahead en
TODO el corte transversal, no solo en esa empresa.
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from brc.datos import presentaciones as P

# Trozo real del formato de EDGAR, con la cabecera variable incluida.
IDX = """Description:           Daily Index of EDGAR Dissemination Feed
Last Data Received:    September 15, 2026
Comments:              webmaster@sec.gov

Form Type   Company Name                  CIK         Date Filed  File Name
---------------------------------------------------------------------------
10-K        ACME CORP                     1750        20260915    edgar/data/1750/0000001750-26-000012.txt
4           BETA INDUSTRIES INC           1800        20260915    edgar/data/1800/0000001800-26-000044.txt
8-K         GAMMA HOLDINGS                 320193     20260915    edgar/data/320193/0000320193-26-000081.txt
"""


class TestParseo:
    def test_saca_las_presentaciones(self):
        df = P._parsear(IDX, date(2026, 9, 15))
        assert df.height == 3
        assert set(df["formulario"]) == {"10-K", "4", "8-K"}

    def test_el_accession_sale_del_nombre_del_fichero(self):
        df = P._parsear(IDX, date(2026, 9, 15))
        assert "0000320193-26-000081" in df["accession"].to_list()

    def test_el_cik_es_un_entero_y_no_el_del_nombre(self):
        df = P._parsear(IDX, date(2026, 9, 15)).sort("cik")
        assert df["cik"].to_list() == [1750, 1800, 320193]

    def test_la_cabecera_se_localiza_por_los_guiones(self):
        """EDGAR ha cambiado esa cabecera; saltar N lineas fijas funciona hasta
        el dia que deja de funcionar, y devuelve basura que parece datos."""
        con_mas_cabecera = IDX.replace("Comments:", "Extra: linea nueva\nComments:")
        assert P._parsear(con_mas_cabecera, date(2026, 9, 15)).height == 3

    def test_sin_separador_devuelve_vacio_en_vez_de_inventar(self):
        assert P._parsear("basura sin formato", date(2026, 9, 15)).is_empty()


class TestFechado:
    @staticmethod
    def _datos():
        acciones = pl.DataFrame({
            "cik": [1750, 1800],
            "fin": [date(2026, 6, 30), date(2026, 6, 30)],
            "accn": ["0000001750-26-000012", "NO-EXISTE"],
            "acciones": [1000.0, 2000.0],
        })
        pres = pl.DataFrame({
            "accession": ["0000001750-26-000012"],
            "cik": [1750],
            "formulario": ["10-K"],
            "presentado": [date(2026, 8, 14)],
        })
        return acciones, pres

    def test_añade_la_fecha_de_publicacion(self):
        a, p = self._datos()
        fechados, _ = P.fechar(a, p)
        assert fechados.height == 1
        assert fechados["publicado"][0] == date(2026, 8, 14)

    def test_los_que_no_cruzan_se_devuelven_aparte_y_no_se_tiran(self):
        """Cuantos hay dice si falta bajar dias del indice."""
        a, p = self._datos()
        _, huerfanos = P.fechar(a, p)
        assert huerfanos.height == 1
        assert huerfanos["accn"][0] == "NO-EXISTE"

    def test_el_retraso_mide_el_look_ahead_que_se_habria_colado(self):
        a, p = self._datos()
        fechados, _ = P.fechar(a, p)
        r = P.retraso(fechados)
        # 45 dias entre el cierre del trimestre y su publicacion.
        assert r["retraso_dias"][0] == 45

    def test_un_retraso_negativo_delataria_un_cruce_mal_hecho(self):
        a, p = self._datos()
        p = p.with_columns(pl.lit(date(2026, 1, 1)).alias("presentado"))
        fechados, _ = P.fechar(a, p)
        assert P.retraso(fechados)["retraso_dias"][0] < 0
