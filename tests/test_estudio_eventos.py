"""Estudio de eventos: las tres formas de engañarse aqui.

1. Entrar antes de que el hecho fuera publico.
2. Comparar contra el indice equivocado.
3. Confundir la cola con el centro.
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from brc.estudio.eventos import EstudioError, medir, sesion_de_entrada

SESIONES = [date(2024, 1, d) for d in (2, 3, 4, 5, 8, 9, 10, 11, 12, 15, 16, 17)]


def _precios(rets: dict[str, list[float]]) -> pl.DataFrame:
    filas = []
    for act, serie in rets.items():
        precio = 100.0
        for i, f in enumerate(SESIONES):
            if i:
                precio *= 1 + serie[i - 1]
            filas.append({"activo": act, "fecha": f, "close": precio})
    return pl.DataFrame(filas)


class TestSesionDeEntrada:
    def test_antes_del_cierre_entra_el_mismo_dia(self):
        ev = pl.DataFrame({"ticker": ["A"], "presentado": [date(2024, 1, 4)],
                           "tras_cierre": [False]})
        assert sesion_de_entrada(ev, SESIONES)["entrada"][0] == date(2024, 1, 4)

    def test_tras_el_cierre_entra_al_dia_siguiente(self):
        ev = pl.DataFrame({"ticker": ["A"], "presentado": [date(2024, 1, 4)],
                           "tras_cierre": [True]})
        assert sesion_de_entrada(ev, SESIONES)["entrada"][0] == date(2024, 1, 5)

    def test_un_evento_en_viernes_tras_cierre_entra_el_lunes(self):
        """Sumar un dia lo habria mandado a un sabado que no existe."""
        ev = pl.DataFrame({"ticker": ["A"], "presentado": [date(2024, 1, 5)],
                           "tras_cierre": [True]})
        assert sesion_de_entrada(ev, SESIONES)["entrada"][0] == date(2024, 1, 8)

    def test_un_evento_en_sabado_entra_el_lunes(self):
        ev = pl.DataFrame({"ticker": ["A"], "presentado": [date(2024, 1, 6)],
                           "tras_cierre": [False]})
        assert sesion_de_entrada(ev, SESIONES)["entrada"][0] == date(2024, 1, 8)


class TestMedicion:
    def test_el_exceso_es_contra_la_mediana_del_universo_no_contra_cero(self):
        """Si todo el mercado sube un 1 %, un valor que sube un 1 % no destaca."""
        rets = {a: [0.01] * 11 for a in ("A", "B", "C", "D", "E")}
        px = _precios(rets)
        ev = pl.DataFrame({"ticker": ["A"], "presentado": [date(2024, 1, 3)],
                           "tras_cierre": [False]})
        r = medir(ev, px, horizonte=5)
        assert abs(r.exceso_medio_pct) < 1e-6

    def test_un_valor_que_cae_mas_que_el_mercado_da_exceso_negativo(self):
        rets = {a: [0.0] * 11 for a in ("B", "C", "D", "E")}
        rets["A"] = [-0.01] * 11
        ev = pl.DataFrame({"ticker": ["A"], "presentado": [date(2024, 1, 3)],
                           "tras_cierre": [False]})
        r = medir(ev, _precios(rets), horizonte=5)
        assert r.exceso_medio_pct < 0

    def test_se_devuelve_la_mediana_ademas_de_la_media(self):
        """Media negativa con mediana positiva es cola, no efecto."""
        rets = {a: [0.0] * 11 for a in ("B", "C", "D", "E")}
        rets["A"] = [0.001] * 11
        rets["Z"] = [-0.10] * 11
        px = _precios(rets)
        ev = pl.DataFrame({"ticker": ["A", "A", "A", "Z"],
                           "presentado": [date(2024, 1, 3)] * 4,
                           "tras_cierre": [False] * 4})
        r = medir(ev, px, horizonte=5)
        assert r.exceso_medio_pct < r.exceso_mediano_pct

    def test_se_reparte_por_tramos_de_años(self):
        rets = {a: [0.0] * 11 for a in ("A", "B", "C")}
        ev = pl.DataFrame({"ticker": ["A"], "presentado": [date(2024, 1, 3)],
                           "tras_cierre": [False]})
        r = medir(ev, _precios(rets), horizonte=5)
        assert "2023-2024" in r.por_tramo

    def test_los_tramos_se_pueden_pedir_a_medida(self):
        """B1 compara 2018-2021 contra 2022-2024, no el reparto por defecto.

        Todos los eventos de la muestra caen en 2024, así que solo se
        rellena el tramo que los contiene -mismo criterio que
        `test_se_reparte_por_tramos_de_años`-; lo que prueba este test es que
        la etiqueta sea la del reparto A MEDIDA (2022-2024) y no la del
        reparto por defecto (2023-2024), que solapa pero no es igual.
        """
        rets = {a: [0.0] * 11 for a in ("A", "B", "C")}
        ev = pl.DataFrame({"ticker": ["A"], "presentado": [date(2024, 1, 3)],
                           "tras_cierre": [False]})
        r = medir(ev, _precios(rets), horizonte=5,
                  tramos=((2018, 2021), (2022, 2024)))
        assert set(r.por_tramo) == {"2022-2024"}

    def test_sin_eventos_no_se_inventa_un_resultado(self):
        with pytest.raises(EstudioError):
            medir(pl.DataFrame(), _precios({"A": [0.0] * 11}), horizonte=5)

    def test_un_evento_sin_precio_no_cuenta(self):
        ev = pl.DataFrame({"ticker": ["NOEXISTE"], "presentado": [date(2024, 1, 3)],
                           "tras_cierre": [False]})
        with pytest.raises(EstudioError):
            medir(ev, _precios({"A": [0.0] * 11}), horizonte=5)
