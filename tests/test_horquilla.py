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

from brc.estudio.horquilla import (UMBRAL_NEGATIVOS, abdi_ranaldo,
                                   acuerdo_entre_predictores,
                                   cota_por_activo, cota_superior,
                                   corwin_schultz, diagnostico_sesgo,
                                   frecuencia_negativos, por_mes,
                                   resumen_por_activo)


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


class TestNegativos:
    """La informacion que se tiraba al poner las negativas a cero."""

    def test_cuenta_las_que_salen_negativas_antes_de_recortar(self):
        d = pl.concat([
            # Par que da negativo: rangos pegados y sin solapar.
            pl.DataFrame([_dia(1, 100.5, 99.5, 100.0), _dia(2, 105.0, 104.0, 104.5)]),
            # Par que da positivo: dos dias identicos, precio quieto.
            pl.DataFrame([_dia(3, 100.5, 99.5, 100.0), _dia(4, 100.5, 99.5, 100.0)]),
        ])
        # Tres pares (2-1, 3-2, 4-3); el 3-2 tambien sale negativo al bajar.
        f = frecuencia_negativos(d)
        assert 0 < f["A"] < 1

    def test_sin_recortar_se_conserva_el_signo(self):
        d = pl.DataFrame([_dia(1, 100.5, 99.5, 100.0), _dia(2, 105.0, 104.0, 104.5)])
        assert corwin_schultz(d)["horquilla_pct"][0] == 0.0
        assert corwin_schultz(d, recortar=False)["horquilla_pct"][0] < 0

    def test_un_activo_sin_negativas_da_cero(self):
        d = _sesiones("A", [(100.0, 0.05)] * 4)
        assert frecuencia_negativos(d)["A"] == 0.0

    def test_marca_fuera_de_zona_buena_por_encima_del_umbral(self):
        d = _sesiones("A", [(100.0, 0.05)] * 4)
        diag = diagnostico_sesgo(d)
        assert diag["A"]["negativos_pct"] == 0.0
        assert diag["A"]["fuera_de_zona_buena"] is False
        assert UMBRAL_NEGATIVOS == 0.40

    def test_tambien_funciona_con_abdi_ranaldo(self):
        d = _sesiones("A", [(100.0, 0.05)] * 4)
        assert frecuencia_negativos(d, metodo=abdi_ranaldo)["A"] == 0.0

    def test_sin_datos_no_inventa_nada(self):
        assert frecuencia_negativos(pl.DataFrame()) == {}
        assert diagnostico_sesgo(pl.DataFrame()) == {}


class TestCotaSuperior:
    """El sesgo de momento de Tremacoldi-Rossi e Irwin (2021)."""

    def test_sin_volatilidad_el_sesgo_de_momento_es_CERO(self):
        """Dos dias de rango identico dan kappa=1, luego phi=sqrt(2), luego
        sesgo de momento exactamente 0. La cota tiene que coincidir con la
        estimacion, y las dos con la horquilla de verdad. Sale del algebra."""
        d = _sesiones("A", [(100.0, 0.02), (100.0, 0.02)])
        c = cota_superior(d)
        assert c.height == 1
        assert c["cota_pct"][0] == pytest.approx(2.0, rel=1e-9)
        assert c["cota_pct"][0] == pytest.approx(c["estimacion_pct"][0], rel=1e-12)

    def test_con_rangos_distintos_la_cota_baja_de_la_estimacion(self):
        """phi > sqrt(2) en cuanto los dos dias no miden lo mismo, y el sesgo
        de momento es no negativo por construccion: la cota nunca sube."""
        d = pl.DataFrame([_dia(1, 101.0, 99.0, 100.0), _dia(2, 100.4, 99.6, 100.0)])
        c = cota_superior(d)
        if c.height:
            assert c["cota_pct"][0] < c["estimacion_pct"][0]

    def test_solo_acota_donde_el_articulo_dice_que_puede(self):
        """Sin estimacion positiva no hay nada que acotar: esas sesiones no
        salen, en vez de salir con un numero inventado."""
        d = pl.DataFrame([_dia(1, 100.5, 99.5, 100.0), _dia(2, 105.0, 104.0, 104.5)])
        assert corwin_schultz(d, recortar=False)["horquilla_pct"][0] < 0
        assert cota_superior(d).is_empty()

    def test_el_resumen_compara_las_MISMAS_sesiones(self):
        """Comparar la cota con la estimacion de todas las sesiones daria que
        acotar empeora, que es la lectura absurda que este resumen evita."""
        d = _sesiones("A", [(100.0, 0.02)] * 6)
        r = cota_por_activo(d)["A"]
        assert r["cota_bps"] == pytest.approx(r["estimacion_cruda_bps"])
        assert r["sesgo_minimo_pct"] == 0.0
        assert r["sesiones_con_cota"] == 5

    def test_sin_datos_no_inventa_nada(self):
        assert cota_superior(pl.DataFrame()).is_empty()
        assert cota_por_activo(pl.DataFrame()) == {}


class TestLosDosPredictores:
    """El articulo pide reportar los dos cuando no hay con que validar."""

    def test_sin_volatilidad_los_dos_dan_la_misma_cota(self):
        """Con el precio quieto no hay ni diferencia de rangos ni salto
        vertical, asi que los dos predictores se reducen a lo mismo y el sesgo
        de momento es cero: las dos cotas son la horquilla exacta."""
        d = _sesiones("A", [(100.0, 0.02), (100.0, 0.02)])
        doble = cota_superior(d, predictor="doble")["cota_pct"][0]
        simple = cota_superior(d, predictor="simple")["cota_pct"][0]
        assert doble == pytest.approx(2.0, rel=1e-9)
        assert simple == pytest.approx(doble, rel=1e-9)

    def test_el_de_la_ecuacion_15_marca_al_menos_tantas_sesiones(self):
        """Sustituir el rango verdadero por el observado SOBREestima el sesgo,
        asi que ese predictor da positivo mas a menudo: sus falsos positivos
        son una consecuencia del metodo, no ruido."""
        d = _sesiones("A", [(100.0, 0.02), (101.0, 0.03), (99.0, 0.01),
                            (100.5, 0.04), (98.0, 0.02)])
        assert (cota_superior(d, predictor="simple").height
                >= cota_superior(d, predictor="doble").height)

    def test_el_acuerdo_es_una_fraccion_por_activo(self):
        d = _sesiones("A", [(100.0, 0.02)] * 5)
        a = acuerdo_entre_predictores(d)
        assert a["A"] == 100.0

    def test_sin_datos_no_inventa_nada(self):
        assert acuerdo_entre_predictores(pl.DataFrame()) == {}
