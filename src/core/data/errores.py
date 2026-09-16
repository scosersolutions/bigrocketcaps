"""Errores comunes de las descargas.

`DescargaError` vivia en `binance_dumps`, el modulo de volcados de Binance, y
lo importaban ocho modulos que no tienen nada que ver con cripto: EDGAR, Form
4, acciones, calendario, eventos, hechos, insiders y objetivos.

Funcionaba mientras estuvieran todos en el mismo paquete. Al podar lo de cripto
para acciones US, ocho modulos se quedaron importando de un fichero que ya no
existia. Una excepcion generica no puede vivir dentro de un modulo especifico:
se queda aqui, que es de donde nunca debio salir.
"""
from __future__ import annotations


class DescargaError(RuntimeError):
    """No se pudo traer el dato, y no se devuelve uno a medias."""
