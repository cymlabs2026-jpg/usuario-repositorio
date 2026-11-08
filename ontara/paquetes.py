"""Registro de paquetes de decisión.

Un paquete = un reporte que lo alimenta + reglas versionadas (reglas/<clave>.yaml) + una función
que detecta decisiones + una función que mide su resultado. La bandeja, la memoria de decisiones,
la matriz de delegación, el grupo de control y el asistente son comunes a todos.

Para agregar un paquete nuevo: escribir su YAML de reglas, su módulo con `detectar` y `medir`,
y registrarlo aquí. Se activan por cliente con ONTARA_PAQUETES (por ejemplo "cartera,comercial,margen,caja,inventario").
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.engine import Engine

from . import caja, comercial, inventario, margen, reglas

# Reportes que el cliente puede cargar y el objeto de la ontología que describe sus columnas.
REPORTES = {
    "cartera": {"nombre": "Cartera: facturas con saldo pendiente", "objeto": "Factura"},
    "ventas": {"nombre": "Ventas: todas las facturas emitidas, incluidas las pagadas", "objeto": "Venta"},
    "lineas": {"nombre": "Ventas por producto: líneas de factura con costo", "objeto": "LineaVenta"},
    "pagos": {"nombre": "Cuentas por pagar: proveedores y obligaciones, con el saldo en bancos",
              "objeto": "CuentaPorPagar"},
    "inventario": {"nombre": "Inventario: existencias por producto con su costo", "objeto": "Existencia"},
    # No alimenta un paquete: da la fecha real de pago para que Ontara aprenda (ver aprendizaje.py).
    "recaudos": {"nombre": "Recaudos: pagos recibidos de clientes (recibos de caja)", "objeto": "Recaudo"},
}


@dataclass(frozen=True)
class Paquete:
    clave: str
    nombre: str
    reporte: str                                   # tipo de carga que lo alimenta
    tipos: dict[str, str]                          # tipo de decisión -> etiqueta
    tipos_control: frozenset[str]                  # tipos que pueden ir al grupo de control
    detectar: Callable[[Engine, int, dict], list[reglas.ResultadoRegla]]
    medir: Callable[[Engine, int], int]


def _detectar_cartera(motor, carga_id, regla):
    from . import decisiones
    return decisiones.detectar_cartera(motor, carga_id, regla)


def _medir_cartera(motor, carga_id):
    from . import decisiones
    return decisiones.medir_cartera(motor, carga_id)


REGISTRO: dict[str, Paquete] = {
    "cartera": Paquete("cartera", "Crédito y cobro", "cartera", reglas.TIPOS, frozenset({"cobrar"}),
                       _detectar_cartera, _medir_cartera),
    "comercial": Paquete("comercial", "Foco comercial", "ventas", comercial.TIPOS,
                         frozenset({"recuperar_cliente"}), comercial.detectar, comercial.medir),
    # Margen no usa grupo de control: renegociar precios con unos clientes y no con otros sería injusto.
    "margen": Paquete("margen", "Margen", "lineas", margen.TIPOS, frozenset(), margen.detectar, margen.medir),
    # Caja tampoco: es una sola caja; no hay a quién dejar sin gestión.
    "caja": Paquete("caja", "Caja a 13 semanas", "pagos", caja.TIPOS, frozenset(), caja.detectar, caja.medir),
    "inventario": Paquete("inventario", "Inventario", "inventario", inventario.TIPOS, frozenset(),
                          inventario.detectar, inventario.medir),
}


def activos() -> list[Paquete]:
    claves = os.environ.get("ONTARA_PAQUETES", "cartera,comercial,margen,caja,inventario")
    return [REGISTRO[c.strip()] for c in claves.split(",") if c.strip() in REGISTRO]


def obtener(clave: str) -> Paquete:
    return REGISTRO[clave]


def de_reporte(reporte: str) -> list[Paquete]:
    return [p for p in activos() if p.reporte == reporte]


def todos_los_tipos() -> dict[str, str]:
    return {t: e for p in REGISTRO.values() for t, e in p.tipos.items()}
