"""Modo demostración: precarga los datos de prueba (inventados) en una base vacía.

Se activa con ONTARA_DEMO=1. Sirve para publicar una versión de prueba en la web sin que nadie tenga que
subir archivos: al arrancar con la base vacía, carga los 13 reportes de `datos_ejemplo/prueba/` en el orden
del README. Nunca se activa en el despliegue de un cliente porque esa variable no se configura allí.
"""
from __future__ import annotations

import os
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from . import calidad, config, decisiones, ingesta, ontologia, paquetes
from .db import cargas

DIR_PRUEBA = config.RAIZ / "datos_ejemplo" / "prueba"
CORTE_1, CORTE_2 = date(2026, 10, 31), date(2026, 11, 14)

# (archivo, tipo de reporte, fecha de corte, saldo en bancos)
ORDEN = [
    ("credito_y_cobro_cartera_2026-10-31.csv", "cartera", CORTE_1, None),
    ("foco_comercial_ventas_2026-10-31.csv", "ventas", CORTE_1, None),
    ("margen_ventas_por_producto_2026-10-31.csv", "lineas", CORTE_1, None),
    ("caja_13_semanas_cuentas_por_pagar_2026-10-31.csv", "pagos", CORTE_1, 250_000_000.0),
    ("inventario_existencias_2026-10-31.csv", "inventario", CORTE_1, None),
    ("despacho_pedidos_2026-10-31.csv", "pedidos", CORTE_1, None),
    ("credito_y_cobro_cartera_2026-11-14.csv", "cartera", CORTE_2, None),
    ("foco_comercial_ventas_2026-11-14.csv", "ventas", CORTE_2, None),
    ("margen_ventas_por_producto_2026-11-14.csv", "lineas", CORTE_2, None),
    ("aprendizaje_recaudos_2026-11-14.csv", "recaudos", CORTE_2, None),
    ("caja_13_semanas_cuentas_por_pagar_2026-11-14.csv", "pagos", CORTE_2, 390_000_000.0),
    ("inventario_existencias_2026-11-14.csv", "inventario", CORTE_2, None),
    ("despacho_pedidos_2026-11-14.csv", "pedidos", CORTE_2, None),
]

EVALUAR = {"cartera": calidad.evaluar, "ventas": calidad.evaluar_ventas, "lineas": calidad.evaluar_lineas,
           "pagos": calidad.evaluar_pagos, "inventario": calidad.evaluar_inventario,
           "pedidos": calidad.evaluar_pedidos, "recaudos": calidad.evaluar_recaudos}


def activo() -> bool:
    return os.environ.get("ONTARA_DEMO", "").lower() in ("1", "true", "si", "sí", "yes")


def cargar_archivo(motor: Engine, nombre: str, tipo: str, corte: date, saldo_bancos: float | None = None) -> dict:
    """Carga un archivo de prueba igual que la pantalla Cargar datos (mapeo sugerido y semáforo de calidad)."""
    plantilla = ontologia.cargar_plantilla(objeto=paquetes.REPORTES[tipo]["objeto"])
    crudo = ingesta.leer_archivo((DIR_PRUEBA / nombre).read_bytes(), nombre)
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns), plantilla)
    datos = ingesta.convertir(crudo, mapeo, plantilla)
    informe, validas = EVALUAR[tipo](datos, mapeo, plantilla)
    return decisiones.registrar_carga(motor, validas, informe, mapeo, corte, nombre, "demo", tipo, saldo_bancos)


def precargar(motor: Engine) -> int:
    """Carga los datos de prueba si la base no tiene ninguna carga. Devuelve cuántos archivos cargó."""
    with motor.connect() as cx:
        if cx.execute(select(func.count()).select_from(cargas)).scalar():
            return 0
    for nombre, tipo, corte, saldo in ORDEN:
        cargar_archivo(motor, nombre, tipo, corte, saldo)
    return len(ORDEN)
