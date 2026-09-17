"""El estimador de Corwin-Schultz, contra casos donde se sabe la respuesta.

La prueba fuerte es la primera: si el precio verdadero no se mueve dentro del
dia, el maximo es el ask y el minimo es el bid, y entonces el estimador tiene
que devolver la horquilla EXACTA, no una aproximacion. Sale del algebra del
articulo -con volatilidad cero, alpha se reduce a ln((1+s/2)/(1-s/2)) y S
vuelve a ser s-, no de haber ejecutado esto y copiado el numero.
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from brc.estudio.horquilla import (abdi_ranaldo, corwin_schultz,
                                   por_mes, resumen_por_activo)


def _sesiones(activo: str, precios_y_spread: list[tuple[float, float]]) -> pl.DataFrame:
    """Un dia por (precio, spread): high y low son el ask y el bid de ese precio.

    El cierre va en el precio medio, que es donde esta el precio verdadero:
    asi no se dispara el ajuste por salto y se ve el estimador solo.
    """
    filas = []
    for i, (p, s) in enumerate(precios_y_spread):
        filas.append({"activo": activo, "fecha": date(2024, 1, 1 + i),
                      "high": p * (1 + s / 2), "low": p * (1 - s / 2), "close": p})
    return pl.DataFrame(filas)


def _dia(dia: int, high: float, low: float, close: float) -> dict:
    return {"activo": "A", "fecha": date(2024, 1, dia),
            "high": high, "low": low, "close": close}


class TestCorwinSchultz:
    @pytest.mark.parametrize("s", [0.0005, 0.01, 0.05])
    def test_sin_volatilidad_devuelve_la_horquilla_exacta(self, s):
        r = corwin_schultz(_sesiones("A", [(100.0, s), (100.0, s)]))
        assert r.height == 1
        assert r["horquilla_pct"][0] == pytest.approx(s * 100, rel=1e-9)

    def test_el_precio_del_activo_no_cambia_el_resultado(self):
        """Es una horquilla PROPORCIONAL: 10 $ o 1.000 $ dan lo mismo."""
        barato = corwin_schultz(_sesiones("A", [(10.0, 0.01), (10.0, 0.01)]))
        caro = corwin_schultz(_sesiones("A", [(1000.0, 0.01), (1000.0, 0.01)]))
        assert barato["horquilla_pct"][0] == pytest.approx(caro["horquilla_pct"][0])

    def test_la_primera_sesion_de_cada_activo_no_tiene_estimacion(self):
        """Hacen falta dos sesiones: la primera no forma par con nada."""
        r = corwin_schultz(_sesiones("A", [(100.0, 0.01)] * 3))
        assert r["fecha"].to_list() == [date(2024, 1, 2), date(2024, 1, 3)]

    def test_no_se_forman_pares_entre_activos_distintos(self):
        d = pl.concat([_sesiones("A", [(100.0, 0.01)] * 2),
                       _sesiones("B", [(50.0, 0.02)] * 2)])
        r = corwin_schultz(d).sort("activo")
        assert r.height == 2
        assert r["horquilla_pct"].to_list() == pytest.approx([1.0, 2.0], rel=1e-9)

    def test_el_salto_se_mide_contra_el_CIERRE_del_dia_anterior(self):
        """El par desplazado y el par ya pegado al cierre son el mismo par.

        Es la prueba de que el ajuste existe y de contra que se hace: si
        midiera contra el maximo del dia anterior, o si no ajustara nada, los
        dos casos darian numeros distintos.
        """
        pegado = corwin_schultz(pl.DataFrame([
            _dia(1, 100.5, 99.5, 100.0),
            _dia(2, 101.0, 100.0, 100.5),
        ]))
        desplazado = corwin_schultz(pl.DataFrame([
            _dia(1, 100.5, 99.5, 100.0),
            _dia(2, 111.0, 110.0, 110.5),
        ]))
        assert desplazado["horquilla_pct"][0] == pytest.approx(
            pegado["horquilla_pct"][0], rel=1e-9)

    def test_una_estimacion_negativa_se_queda_en_cero(self):
        """Dos rangos que quedan pegados pero sin solapar dan alpha negativo.
        Una horquilla negativa no existe: es ruido del estimador, y el
        articulo manda dejarla en cero antes de promediar."""
        r = corwin_schultz(pl.DataFrame([
            _dia(1, 100.5, 99.5, 100.0),
            _dia(2, 105.0, 104.0, 104.5),
        ]))
        assert r["horquilla_pct"][0] == 0.0

    def test_nunca_devuelve_una_horquilla_negativa(self):
        d = _sesiones("A", [(100.0, 0.01), (150.0, 0.2), (80.0, 0.001),
                            (81.0, 0.05), (200.0, 0.0001)])
        assert (corwin_schultz(d)["horquilla_pct"] >= 0).all()

    def test_sin_datos_no_se_inventa_una_tabla(self):
        assert corwin_schultz(pl.DataFrame()).is_empty()


class TestAbdiRanaldo:
    @pytest.mark.parametrize("s", [0.001, 0.01])
    def test_con_el_cierre_en_el_ask_recupera_la_horquilla(self, s):
        """c - eta sale exactamente ln((1+s/2)/(1-s/2))/2, asi que S = ese
        logaritmo, que es s hasta el tercer orden. Derivado, no copiado."""
        import math
        k = math.log((1 + s / 2) / (1 - s / 2))
        d = pl.DataFrame([
            _dia(1, 100 * (1 + s / 2), 100 * (1 - s / 2), 100 * (1 + s / 2)),
            _dia(2, 100 * (1 + s / 2), 100 * (1 - s / 2), 100 * (1 + s / 2)),
        ])
        r = abdi_ranaldo(d)
        assert r["horquilla_pct"][0] == pytest.approx(k * 100, rel=1e-9)

    def test_una_sesion_sin_rango_no_cuenta(self):
        """High == Low es un dia sin libro observable: la referencia lo
        descarta en vez de dividir por un rango de cero."""
        d = pl.DataFrame([_dia(1, 100.0, 100.0, 100.0), _dia(2, 101.0, 99.0, 100.0)])
        assert abdi_ranaldo(d).is_empty()

    def test_una_estimacion_negativa_se_queda_en_cero(self):
        """S^2 sale negativo cuando el cierre queda por DEBAJO del medio de su
        dia y por ENCIMA del medio del siguiente: los dos factores tienen
        signo distinto. No hay raiz que sacar, asi que cero."""
        d = pl.DataFrame([_dia(1, 101.0, 99.0, 99.1), _dia(2, 98.0, 96.0, 97.0)])
        assert abdi_ranaldo(d)["horquilla_pct"][0] == 0.0

    def test_sin_datos_no_se_inventa_una_tabla(self):
        assert abdi_ranaldo(pl.DataFrame()).is_empty()


class TestPorMes:
    def test_promedia_dentro_del_mes_y_no_entre_meses(self):
        d = pl.DataFrame({
            "activo": ["A"] * 3,
            "fecha": [date(2024, 1, 5), date(2024, 1, 20), date(2024, 2, 3)],
            "horquilla_pct": [0.02, 0.04, 0.10],
        })
        r = por_mes(d).sort("mes")
        assert r["horquilla_pct"].to_list() == pytest.approx([0.03, 0.10])


class TestResumen:
    def test_agrega_meses_y_pasa_a_puntos_basicos(self):
        """Dos sesiones del mismo mes son UN mes: `n` cuenta meses, no dias,
        y el valor del mes es la media de sus dias."""
        e = pl.DataFrame({"activo": ["A", "A"],
                          "fecha": [date(2024, 1, 5), date(2024, 1, 20)],
                          "horquilla_pct": [0.01, 0.03]})
        r = resumen_por_activo(e)
        assert r["A"]["n"] == 1
        assert r["A"]["mediana_bps"] == 2.0   # media 0,02 % = 2 bps
        assert r["A"]["max_bps"] == 2.0

    def test_tiene_los_mismos_campos_que_la_medicion_que_sustituye(self):
        e = pl.DataFrame({"activo": ["A"], "fecha": [date(2024, 1, 5)],
                          "horquilla_pct": [0.05]})
        assert set("n mediana_bps p90_bps max_bps".split()) == set(
            resumen_por_activo(e)["A"])

    def test_sin_estimaciones_devuelve_vacio(self):
        assert resumen_por_activo(pl.DataFrame()) == {}
