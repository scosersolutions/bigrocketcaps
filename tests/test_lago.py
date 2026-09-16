"""El archivo maestro esta en Hugging Face; lo local es cache borrable.

Estos tests NO tocan la red: comprueban la logica que decide de donde se lee y
que lo cacheado se puede tirar sin perder nada. La prueba contra el lago real
se hace a mano, porque depende de credenciales.
"""
from __future__ import annotations

import pytest

from brc.datos import lago


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(lago, "CACHE", tmp_path / "cache")
    return tmp_path / "cache"


class TestRuta:
    def test_la_url_apunta_al_dataset(self):
        u = lago.ruta("insiders", 2024)
        assert u.startswith("hf://datasets/")
        assert u.endswith("/insiders/2024.parquet")


class TestDeDondeSeLee:
    def test_sin_cache_se_lee_del_remoto(self, cache):
        sql = lago.leer(None, "insiders", [2024])
        assert "hf://" in sql

    def test_con_cache_se_lee_de_local(self, cache):
        f = cache / "insiders" / "2024.parquet"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"no importa el contenido")
        sql = lago.leer(None, "insiders", [2024])
        assert "hf://" not in sql
        assert "2024.parquet" in sql

    def test_se_mezclan_los_años_cacheados_con_los_que_no(self, cache):
        """Bajar 2024 no obliga a bajar 2023 para poder consultar los dos."""
        f = cache / "insiders" / "2024.parquet"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
        sql = lago.leer(None, "insiders", [2023, 2024])
        assert "hf://" in sql          # 2023 remoto
        assert str(f.as_posix()) in sql  # 2024 local

    def test_sin_años_y_sin_cache_se_usa_el_comodin_remoto(self, cache):
        assert "hf://" in lago.leer(None, "eventos", None)


class TestCacheBorrable:
    def test_lo_ocupado_se_cuenta(self, cache):
        f = cache / "ohlcv" / "2024.parquet"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"0" * 5_000)
        assert lago.ocupado() == 5_000

    def test_vaciar_libera_y_deja_la_cuenta_a_cero(self, cache):
        f = cache / "ohlcv" / "2024.parquet"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"0" * 5_000)
        assert lago.vaciar() == 5_000
        assert lago.ocupado() == 0

    def test_se_puede_vaciar_una_sola_tabla(self, cache):
        for t in ("ohlcv", "insiders"):
            f = cache / t / "2024.parquet"
            f.parent.mkdir(parents=True)
            f.write_bytes(b"0" * 1_000)
        assert lago.vaciar("ohlcv") == 1_000
        assert lago.ocupado() == 1_000      # insiders sigue

    def test_vaciar_algo_que_no_esta_no_es_un_error(self, cache):
        assert lago.vaciar() == 0


class TestCredencial:
    def test_sin_token_se_dice_que_falta_en_vez_de_fallar_por_dentro(
            self, monkeypatch, tmp_path):
        monkeypatch.delenv("HF_TOKEN", raising=False)
        monkeypatch.setattr(lago.Path, "home", staticmethod(lambda: tmp_path))
        with pytest.raises(lago.LagoError, match="Hugging Face"):
            lago._token()
