"""Paquete Inventario."""
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

from ontara import calidad, decisiones, ingesta, inventario, margen, ontologia, paquetes, reglas
from ontara.db import resultados_inventario

CORTE = date(2026, 10, 31)
C2 = date(2026, 11, 14)
PRUEBA = Path(__file__).resolve().parent.parent / "datos_ejemplo" / "prueba"
M = 1_000_000


@pytest.fixture
def p():
    return reglas.parametros_base("inventario")


def existencia(producto, cantidad, costo_u):
    return {"producto": producto, "cantidad": cantidad, "costo_total": cantidad * costo_u, "costo_unitario": costo_u,
            "bodegas": ""}


def demanda(producto, uds_dia, costo_u, margen_pct=30.0, dias_sin_venta=1):
    venta_dia = uds_dia * costo_u / (1 - margen_pct / 100)
    return {"producto": producto, "unidades_dia": uds_dia, "costo_dia": uds_dia * costo_u, "ventas_dia": venta_dia,
            "margen_pct": margen_pct, "ultima_venta": CORTE - timedelta(days=dias_sin_venta)}


def evaluar(exist, dem, p, otras=None):
    res = inventario.evaluar_inventario(pd.DataFrame(exist), pd.DataFrame(dem), CORTE, p, otras)
    return {r.nombre: r for r in res}


def test_reponer_antes_del_quiebre(p):
    res = evaluar([existencia("Arroz", 50, 100_000)], [demanda("Arroz", 10, 100_000)], p)
    r = res["Arroz"]                                     # 5 días de cobertura < 15 + 7
    assert r.tipo == "reponer_producto" and r.nit == margen.id_producto("Arroz")
    assert r.evidencia["unidades_sugeridas"] == 400     # 10/día × (15 + 30) - 50
    assert r.saldo_vencido == 40 * M and r.evidencia["dias_sin_stock"] == 10
    assert r.nivel_aprobacion == "gerente"              # 40 M > 20 M del líder


def test_no_reponer_lo_que_se_vende_bajo_el_costo(p):
    otras = [{"id": 9, "tipo": "revisar_margen_producto", "evidencia": {"producto": "Aceite"}}]
    r = evaluar([existencia("Aceite", 30, 100_000)], [demanda("Aceite", 10, 100_000, -5.0)], p, otras)["Aceite"]
    assert r.recomendacion.startswith("Corregir el precio") and "decisión de Margen #9" in r.evidencia["cruces"][0]


def test_inventario_lento_y_sin_movimiento(p):
    exist = [existencia("Café", 1500, 50_000), existencia("Vinagre", 200, 20_000), existencia("Sal", 400, 10_000)]
    dem = [demanda("Café", 10, 50_000), demanda("Sal", 10, 10_000),
           demanda("Vinagre", 0, 20_000, dias_sin_venta=200)]
    res = evaluar(exist, dem, p)
    assert res["Café"].tipo == "liberar_inventario"     # 150 días: sobran 1500 - 300 = 1200 unidades
    assert res["Café"].saldo_vencido == 60 * M
    assert res["Vinagre"].evidencia["sin_movimiento"] and res["Vinagre"].saldo_vencido == 4 * M
    assert "Sal" not in res                              # 40 días: sano


def test_umbral_y_excepciones(p):
    exist = [existencia("Chicle", 100, 10_000)]          # 1 M sin movimiento: bajo el mínimo de 2 M
    assert evaluar(exist, [], p) == {}
    p["productos_excepcion"] = ["café"]
    assert evaluar([existencia("Café", 1500, 50_000)], [demanda("Café", 10, 50_000)], p) == {}


def test_cruce_con_caja(p):
    otras = [{"id": 4, "tipo": "caja_bajo_minimo", "evidencia": {"inicio": "2026-12-20"}}]
    res = evaluar([existencia("Café", 1500, 50_000)], [demanda("Café", 10, 50_000)], p, otras)
    assert "decisión #4" in res["Café"].evidencia["cruces"][0]


def test_semaforo_de_existencias():
    pl = ontologia.cargar_plantilla(objeto="Existencia")
    crudo = pd.DataFrame({"Referencia": ["Arroz", "Arroz", "Sal", ""], "Bodega": ["Cali", "Cali", "Cali", "Cali"],
                          "Existencia": ["10", "5", "-3", "4"], "Costo promedio": ["$ 1.000", "$ 1.000", "$ 500", "1"]})
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns), pl)
    assert {mapeo["producto"], mapeo["cantidad"], mapeo["costo_unitario"]} == {"Referencia", "Existencia",
                                                                                "Costo promedio"}
    informe, validas = calidad.evaluar_inventario(ingesta.convertir(crudo, mapeo, pl), mapeo, pl)
    assert informe.filas_validas == 1 and validas["costo_total"].iloc[0] == 10_000
    sin_costo = {k: v for k, v in mapeo.items() if k != "costo_unitario"}
    informe, _ = calidad.evaluar_inventario(ingesta.convertir(crudo, sin_costo, pl), sin_costo, pl)
    assert not informe.puede_cargar


EVALUAR = {"cartera": calidad.evaluar, "ventas": calidad.evaluar_ventas, "lineas": calidad.evaluar_lineas,
           "pagos": calidad.evaluar_pagos, "inventario": calidad.evaluar_inventario}


def cargar(motor, nombre, corte, tipo, saldo=None):
    pl = ontologia.cargar_plantilla(objeto=paquetes.REPORTES[tipo]["objeto"])
    crudo = ingesta.leer_archivo((PRUEBA / nombre).read_bytes(), nombre)
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns), pl)
    informe, validas = EVALUAR[tipo](ingesta.convertir(crudo, mapeo, pl), mapeo, pl)
    assert informe.puede_cargar and informe.filas_validas == informe.filas_total
    return decisiones.registrar_carga(motor, validas, informe, mapeo, corte, nombre, "prueba", tipo, saldo)


def test_sin_ventas_por_producto_no_hay_decisiones(motor):
    r = cargar(motor, "inventario_existencias_2026-10-31.csv", CORTE, "inventario")
    assert r["paquetes"] == ["inventario"] and r["decisiones_nuevas"] == 0
    # Al llegar las ventas por producto, Inventario se recalcula solo.
    cargar(motor, "margen_ventas_por_producto_2026-10-31.csv", CORTE, "lineas")
    assert decisiones.listar(motor, None, paquete="inventario")


def test_flujo_completo_de_inventario(motor):
    cargar(motor, "margen_ventas_por_producto_2026-10-31.csv", CORTE, "lineas")
    cargar(motor, "inventario_existencias_2026-10-31.csv", CORTE, "inventario")
    lista = {d["evidencia"]["producto"]: d for d in decisiones.listar(motor, None, paquete="inventario")}
    assert {lista[x]["tipo"] for x in ("Arroz blanco 25 kg", "Leche en polvo 25 kg", "Panela 24 und")} == \
        {"reponer_producto"}
    assert lista["Detergente 10 kg"]["tipo"] == "liberar_inventario"
    assert lista["Harina de maíz 25 kg"]["evidencia"]["sin_movimiento"]
    assert lista["Aceite de girasol 3 L"]["recomendacion"].startswith("Corregir el precio")
    for d in lista.values():
        decisiones.registrar_accion(motor, d["id"], "aprobar", "gerente")

    cargar(motor, "margen_ventas_por_producto_2026-11-14.csv", C2, "lineas")
    r = cargar(motor, "inventario_existencias_2026-11-14.csv", C2, "inventario")
    assert r["resultados_medidos"] == len(lista)
    with motor.connect() as cx:
        res = {f["decision_id"]: f for f in cx.execute(select(resultados_inventario)).mappings()}
    assert res[lista["Panela 24 und"]["id"]]["quiebre"]                     # se agotó
    assert not res[lista["Arroz blanco 25 kg"]["id"]]["quiebre"]            # se repuso a tiempo
    assert res[lista["Harina de maíz 25 kg"]["id"]]["liberado"] == lista["Harina de maíz 25 kg"]["saldo_vencido_inicial"]
    s = inventario.resumen(motor)
    assert s["capital_liberado_atendidas"] > 0 and s["quiebres"] == 1
    assert s["hoy"]["capital"] < s["base"]["capital"] and s["hoy"]["agotados"] == 1
