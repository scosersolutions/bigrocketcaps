"""Eventos societarios: la clasificacion y, sobre todo, la hora.

Medido sobre los 85.598 eventos que ya tenia MoonRocket: el 58 % se acepta
DESPUES del cierre. Tratar un 8-K de las 20:30 UTC como operable ese mismo dia
regala media sesion que nadie tuvo, y en un estudio de eventos esa media sesion
ES el resultado.
"""
from __future__ import annotations

from datetime import date

import polars as pl

from brc.datos.eventos import eventos_de


def _empresa(presentaciones: list[dict], cik: int = 320193,
             tickers: list[str] | None = None) -> dict:
    campos = ("form", "items", "acceptanceDateTime", "filingDate",
              "reportDate", "accessionNumber")
    recent = {c: [p.get(c, "") for p in presentaciones] for c in campos}
    return {"cik": cik, "tickers": tickers or ["AAPL"],
            "filings": {"recent": recent}}


UNA = {
    "form": "8-K", "items": "5.02", "accessionNumber": "0000320193-26-000018",
    "acceptanceDateTime": "2026-07-30T14:05:00.000Z",
    "filingDate": "2026-07-30", "reportDate": "2026-07-28",
}


class TestClasificacion:
    def test_un_cambio_de_directivo_se_reconoce(self):
        df = eventos_de(_empresa([UNA]))
        assert df["clase"].to_list() == ["cambio_directivo"]

    def test_un_8k_puede_ser_dos_eventos_a_la_vez(self):
        p = dict(UNA, items="2.02,5.02,9.01")
        df = eventos_de(_empresa([p]))
        assert sorted(df["clase"].to_list()) == ["cambio_directivo", "resultados"]

    def test_el_item_de_anexos_solo_no_es_un_evento(self):
        """9.01 acompaña a casi todo y no dice nada por si mismo."""
        assert eventos_de(_empresa([dict(UNA, items="9.01")])).is_empty()

    def test_los_formularios_que_valen_por_si_mismos_no_miran_items(self):
        p = dict(UNA, form="SC 13D", items="")
        assert eventos_de(_empresa([p]))["clase"].to_list() == ["posicion_activista"]

    def test_un_formulario_que_no_interesa_se_descarta(self):
        assert eventos_de(_empresa([dict(UNA, form="10-Q", items="")])).is_empty()

    def test_una_empresa_sin_presentaciones_no_revienta(self):
        assert eventos_de({"cik": 1, "filings": {"recent": {}}}).is_empty()


class TestHora:
    def test_antes_del_cierre_es_operable_ese_dia(self):
        df = eventos_de(_empresa([dict(UNA, acceptanceDateTime="2026-07-30T14:05:00.000Z")]))
        assert df["tras_cierre"][0] is False

    def test_despues_del_cierre_no(self):
        df = eventos_de(_empresa([dict(UNA, acceptanceDateTime="2026-07-30T20:30:00.000Z")]))
        assert df["tras_cierre"][0] is True

    def test_sin_hora_se_asume_que_NO_era_operable(self):
        """El supuesto que no regala ventaja."""
        df = eventos_de(_empresa([dict(UNA, acceptanceDateTime="")]))
        assert df["tras_cierre"][0] is True

    def test_se_guarda_la_fecha_del_hecho_aparte_de_la_de_presentacion(self):
        df = eventos_de(_empresa([UNA]))
        assert df["periodo"][0] == date(2026, 7, 28)
        assert df["presentado"][0] == date(2026, 7, 30)
