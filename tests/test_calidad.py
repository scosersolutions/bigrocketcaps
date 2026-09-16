"""Control de calidad de precios: coger lo que arruina un backtest en silencio.

No hay segunda fuente --Stooq devuelve un desafio anti-bot desde el
2026-09-16-- asi que esto es lo unico que separa una serie usable de una con
un -90 % que nunca ocurrio.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from brc.datos.calidad import dias_a_excluir, revisar


def _serie(cierres: list[float], volumen: float = 1_000.0) -> pl.DataFrame:
    t0 = datetime(2024, 1, 2, tzinfo=UTC)
    return pl.DataFrame({
        "ts": [t0 + timedelta(days=i) for i in range(len(cierres))],
        "open": cierres,
        "high": [c * 1.01 for c in cierres],
        "low": [c * 0.99 for c in cierres],
        "close": cierres,
        "volume": [volumen] * len(cierres),
    })


class TestSerieSana:
    def test_una_serie_normal_no_da_incidencias(self):
        assert revisar(_serie([100.0, 101.0, 99.5, 102.0, 103.0])).is_empty()

    def test_una_serie_vacia_no_revienta(self):
        assert revisar(pl.DataFrame(schema={"ts": pl.Datetime(time_zone="UTC")})).is_empty()


class TestGraves:
    def test_un_maximo_menor_que_el_cierre_es_imposible(self):
        df = _serie([100.0, 101.0]).with_columns(
            pl.when(pl.col("close") == 101.0).then(pl.lit(50.0))
            .otherwise(pl.col("high")).alias("high"))
        inc = revisar(df)
        assert "ohlc_incoherente" in inc["tipo"].to_list()
        assert inc.filter(pl.col("tipo") == "ohlc_incoherente")["severidad"][0] == "grave"

    def test_un_precio_cero_no_es_un_precio(self):
        inc = revisar(_serie([100.0, 0.0, 101.0]))
        assert "precio_no_positivo" in inc["tipo"].to_list()

    def test_una_caida_a_la_mitad_exacta_es_un_split_sin_ajustar(self):
        """No es una perdida del 50 %: es la misma empresa con el doble de
        acciones. Contarlo como retorno inventa una perdida que nunca existio."""
        inc = revisar(_serie([100.0, 50.0, 50.5]))
        assert "posible_split_sin_ajustar" in inc["tipo"].to_list()
        assert inc.filter(
            pl.col("tipo") == "posible_split_sin_ajustar")["severidad"][0] == "grave"

    def test_un_desplome_que_NO_cuadra_con_un_split_se_marca_pero_no_se_tira(self):
        """Una caida del 62 % puede ser real: hay empresas que se desploman."""
        inc = revisar(_serie([100.0, 38.0, 37.0]))
        assert "salto_grande" in inc["tipo"].to_list()
        assert inc.filter(pl.col("tipo") == "salto_grande")["severidad"][0] == "sospechoso"

    def test_dos_velas_con_el_mismo_instante(self):
        df = _serie([100.0, 101.0])
        doble = pl.concat([df, df.head(1)]).sort("ts")
        assert "ts_duplicado" in revisar(doble)["tipo"].to_list()


class TestSospechosos:
    def test_cinco_dias_al_mismo_precio_con_volumen_es_una_serie_congelada(self):
        inc = revisar(_serie([100.0] * 6 + [101.0]))
        assert "serie_congelada" in inc["tipo"].to_list()

    def test_sin_volumen_no_se_marca_congelada(self):
        """Un valor que no negocia puede cerrar igual legitimamente."""
        inc = revisar(_serie([100.0] * 6, volumen=0.0))
        assert "serie_congelada" not in inc["tipo"].to_list()


class TestExclusion:
    def test_solo_se_excluyen_los_dias_graves(self):
        inc = revisar(_serie([100.0, 0.0, 101.0, 38.0]))
        fuera = dias_a_excluir(inc)
        # El precio cero se va; la caida del 62 % se queda marcada.
        assert len(fuera) >= 1
        graves = inc.filter(pl.col("severidad") == "grave")["ts"].dt.date().to_list()
        assert set(graves) == fuera

    def test_sin_incidencias_no_se_excluye_nada(self):
        assert dias_a_excluir(revisar(_serie([100.0, 101.0]))) == set()


class TestCerosYSaltos:
    """Un cero en la serie no puede envenenar el calculo de saltos."""

    def test_un_cierre_a_cero_no_genera_un_ratio_infinito(self):
        """Dividir por cero da inf, y formatear inf hacia reventar a polars.
        El cero se coge como precio no positivo, que es donde corresponde."""
        inc = revisar(_serie([100.0, 0.0, 101.0]))
        assert "precio_no_positivo" in inc["tipo"].to_list()
        assert not any("inf" in d.lower() for d in inc["detalle"].to_list())

    def test_el_salto_se_mide_entre_precios_validos(self):
        inc = revisar(_serie([100.0, 0.0, 50.0, 51.0]))
        # De 0 a 50 no hay ratio; de 100 a 0 tampoco. Solo queda 50->51.
        assert "salto_grande" not in inc["tipo"].to_list()


def test_el_split_2a1_cae_justo_en_el_umbral_y_aun_asi_se_coge():
    """Un 2:1 deja el cierre en exactamente la mitad: movimiento 0,50 clavado.
    Con un umbral estricto se colaba justo el caso mas comun."""
    inc = revisar(_serie([100.0, 50.0, 50.5]))
    assert "posible_split_sin_ajustar" in inc["tipo"].to_list()
