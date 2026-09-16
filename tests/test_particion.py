"""La valla entre explorar y validar.

Discovery automatico y un contador de contrastes compartido son incompatibles:
con cinco mil pruebas el liston del Deflated Sharpe sube tanto que nada podria
validarse. La salida no es contar menos, es que discovery NO PUEDA mirar.
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from brc.datos.particion import (
    HOLDOUT_DESDE, PermisoDenegado, Puerta, Reparto, Ventana, sortear)

SECTORES = ["tecnologia", "salud", "energia", "finanzas", "consumo",
            "industria", "materiales", "servicios", "inmobiliario", "telecos"]


class TestSorteo:
    def test_reparte_todos_los_sectores(self):
        r = sortear(SECTORES)
        assert r.sectores == frozenset(SECTORES)
        assert not (r.exploracion & r.validacion)

    def test_es_reproducible(self):
        assert sortear(SECTORES) == sortear(SECTORES)

    def test_otra_semilla_reparte_distinto(self):
        a, b = sortear(SECTORES), sortear(SECTORES, semilla="otra")
        assert a.exploracion != b.exploracion

    def test_añadir_un_sector_no_reordena_a_los_demas(self):
        """Si el reparto cambiara al crecer el universo, un estudio de hace un
        mes habria visto una muestra distinta de la que dice haber visto."""
        antes = sortear(SECTORES)
        despues = sortear(SECTORES + ["nuevo"])
        for s in SECTORES:
            assert (s in antes.exploracion) == (s in despues.exploracion)

    def test_sin_sectores_no_revienta(self):
        assert sortear([]).sectores == frozenset()


class TestPuerta:
    @staticmethod
    def _datos():
        return pl.DataFrame({
            "sector": ["tecnologia", "salud", "energia", "finanzas"] * 2,
            "fecha": [date(2020, 6, 1)] * 4 + [date(2026, 6, 1)] * 4,
            "valor": list(range(8)),
        })

    def test_exploracion_solo_ve_sus_sectores(self):
        r = sortear(SECTORES)
        p = Puerta(Ventana.EXPLORACION, r)
        fuera = p.recortar(self._datos())
        assert set(fuera["sector"]) <= r.exploracion

    def test_validacion_ve_los_otros_y_ninguno_se_repite(self):
        r = sortear(SECTORES)
        exp = set(Puerta(Ventana.EXPLORACION, r).recortar(self._datos())["sector"])
        val = set(Puerta(Ventana.VALIDACION, r).recortar(self._datos())["sector"])
        assert not (exp & val)

    def test_fuera_del_holdout_no_se_ve_el_periodo_reservado(self):
        r = sortear(SECTORES)
        for v in (Ventana.EXPLORACION, Ventana.VALIDACION):
            fuera = Puerta(v, r).recortar(self._datos())
            if not fuera.is_empty():
                assert fuera["fecha"].max() < HOLDOUT_DESDE

    def test_el_holdout_solo_ve_el_periodo_reservado(self):
        """Abrirlo no da acceso al resto: da acceso a lo que faltaba."""
        fuera = Puerta(Ventana.HOLDOUT, sortear(SECTORES)).recortar(self._datos())
        assert not fuera.is_empty()
        assert fuera["fecha"].min() >= HOLDOUT_DESDE

    def test_el_holdout_ve_todos_los_sectores(self):
        r = sortear(SECTORES)
        assert Puerta(Ventana.HOLDOUT, r).sectores_visibles() == r.sectores

    def test_un_marco_vacio_no_revienta(self):
        p = Puerta(Ventana.EXPLORACION, sortear(SECTORES))
        assert p.recortar(pl.DataFrame()).is_empty()


class TestExigir:
    def test_pedir_otra_ventana_falla_ruidosamente(self):
        p = Puerta(Ventana.EXPLORACION, sortear(SECTORES))
        with pytest.raises(PermisoDenegado, match="exploracion"):
            p.exigir(Ventana.VALIDACION)

    def test_pedir_la_propia_no_falla(self):
        p = Puerta(Ventana.VALIDACION, sortear(SECTORES))
        p.exigir(Ventana.VALIDACION)
