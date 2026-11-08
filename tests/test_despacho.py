"""Paquete Despacho."""
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

from ontara import calidad, decisiones, despacho, ingesta, ontologia, paquetes, reglas
from ontara.db import resultados_despacho

CORTE = date(2026, 10, 31)
C2 = date(2026, 11, 14)
PRUEBA = Path(__file__).resolve().parent.parent / "datos_ejemplo" / "prueba"
M = 1_000_000


@pytest.fixture
def p():
    return reglas.parametros_base("despacho")


def pedido(nit, num, prometida, valor, despacho_=None, productos=None, dias_antes=2):
    return {"nit": nit, "pedido": num, "fecha_pedido": prometida - timedelta(days=dias_antes),
            "fecha_compromiso": prometida, "fecha_despacho": despacho_, "valor": valor,
            "valor_pendiente": 0.0 if despacho_ else valor, "productos": [] if despacho_ else (productos or []),
            "ciudad": "Cali"}


def evaluar(ped, p, **kw):
    res = despacho.evaluar_despacho(pd.DataFrame(ped, columns=despacho.COLUMNAS), CORTE, p, **kw)
    return {(r.nit, r.tipo): r for r in res}


# --- Pedidos en riesgo ------------------------------------------------------------------------
def test_despachar_pedidos_atrasados_y_por_vencer(p):
    res = evaluar([pedido("A", "1", CORTE - timedelta(days=3), 5 * M),
                   pedido("A", "2", CORTE + timedelta(days=1), 2 * M),        # vence mañana: entra
                   pedido("A", "3", CORTE + timedelta(days=4), 9 * M),        # todavía no: no entra
                   pedido("B", "4", CORTE - timedelta(days=1), 500_000)], p,  # menos del monto mínimo
                  info={"A": {"nombre": "Cliente A", "cupo": None}})
    assert list(res) == [("A", "despachar_pedidos")]
    d = res[("A", "despachar_pedidos")]
    assert d.saldo_vencido == 7 * M and [x["pedido"] for x in d.evidencia["pedidos"]] == ["1", "2"]
    assert d.evidencia["dias_atraso_max"] == 3 and d.evidencia["bloqueo"] is None
    assert d.recomendacion.startswith("Despachar hoy los pedidos 1, 2 de Cliente A")


def test_credito_frenado_retiene_el_pedido(p):
    otras = [{"id": 7, "nit": "A", "tipo": "congelar_credito", "evidencia": {}}]
    d = evaluar([pedido("A", "1", CORTE - timedelta(days=2), 5 * M)], p, otras=otras,
                compras={"A": 900 * M, "B": 10 * M})[("A", "despachar_pedidos")]
    assert d.evidencia["bloqueo"] == "credito" and "No despachar" in d.recomendacion and "#7" in d.recomendacion
    assert d.evidencia["cliente_clave"] and d.nivel_aprobacion == "gerente"   # retener a un cliente clave


def test_pedido_que_pasa_el_cupo(p):
    ped = [pedido("A", "1", CORTE, 15 * M)]
    info = {"A": {"nombre": "Cliente A", "cupo": 100 * M}}
    d = evaluar(ped, p, info=info, saldos={"A": 90 * M})[("A", "despachar_pedidos")]
    assert d.evidencia["bloqueo"] == "cupo" and "pasan el cupo por $5.000.000" in d.recomendacion
    otras = [{"id": 3, "nit": "A", "tipo": "ampliar_cupo", "evidencia": {}}]
    d = evaluar(ped, p, info=info, saldos={"A": 90 * M}, otras=otras)[("A", "despachar_pedidos")]
    assert d.recomendacion.startswith("Aprobar la ampliación de cupo de Cliente A (decisión #3)")


def test_falta_inventario_con_cruce_a_reposicion(p):
    ped = [pedido("A", "1", CORTE, 5 * M, productos=[{"producto": "Leche", "cantidad": 30, "valor": 5 * M}]),
           pedido("B", "2", CORTE - timedelta(days=1), 3 * M,
                  productos=[{"producto": "Leche", "cantidad": 10, "valor": 3 * M}])]
    otras = [{"id": 9, "nit": "P-x", "tipo": "reponer_producto", "evidencia": {"producto": "Leche"}}]
    res = evaluar(ped, p, stock={"Leche": 35}, otras=otras)
    d = res[("A", "despachar_pedidos")]
    assert d.evidencia["bloqueo"] == "inventario" and d.evidencia["faltantes"][0]["comprometido"] == 40
    assert "Inventario, decisión #9" in d.evidencia["cruces"][0]
    assert evaluar(ped, p, stock=None)[("A", "despachar_pedidos")].evidencia["bloqueo"] is None   # sin reporte


# --- Cumplimiento ------------------------------------------------------------------------------
def test_recuperar_cumplimiento(p):
    def hechos(nit, atrasos):
        return [pedido(nit, f"{nit}{k}", CORTE - timedelta(days=10 * k + 5), 2 * M,
                       CORTE - timedelta(days=10 * k + 5 - a)) for k, a in enumerate(atrasos)]
    ped = hechos("A", [0, 3, 0, 4, 2]) + hechos("B", [0, 0, 0, 0, 0, 0, 1]) + hechos("C", [5, 5, 5])
    otras = [{"id": 4, "nit": "A", "tipo": "recuperar_cliente", "evidencia": {}}]
    res = evaluar(ped, p, otras=otras, compras={"A": 120 * M, "B": 80 * M})
    d = res[("A", "recuperar_cumplimiento")]
    assert d.evidencia["cumplimiento_pct"] == 40 and d.evidencia["dias_atraso_prom"] == 3
    assert d.saldo_vencido == 120 * M and len(d.evidencia["tarde"]) == 3
    assert "Foco comercial" in d.evidencia["cruces"][0]
    assert ("B", "recuperar_cumplimiento") not in res      # 6 de 7 a tiempo (86 %): sobre el mínimo
    assert ("C", "recuperar_cumplimiento") not in res      # solo 3 pedidos: muy pocos para medir


# --- Calidad y flujo ----------------------------------------------------------------------------
def test_semaforo_de_pedidos():
    pl = ontologia.cargar_plantilla(objeto="Pedido")
    crudo = pd.DataFrame({"NIT": ["900111222", "900111222", "", "900333444"], "Cliente": ["Uno", "Uno", "Dos", "Tres"],
                          "Pedido": ["P1", "P1", "P2", "P3"], "Fecha pedido": ["01/10/2026"] * 4,
                          "Fecha entrega prometida": ["03/10/2026"] * 4,
                          "Fecha despacho": ["03/10/2026", "", "", "28/09/2026"],
                          "Producto": ["Arroz", "Leche", "Arroz", "Arroz"], "Valor": ["$ 1.000.000"] * 4})
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns), pl)
    assert {mapeo["pedido"], mapeo["fecha_compromiso"], mapeo["fecha_despacho"]} == \
        {"Pedido", "Fecha entrega prometida", "Fecha despacho"}
    informe, validas = calidad.evaluar_pedidos(ingesta.convertir(crudo, mapeo, pl), mapeo, pl)
    assert informe.filas_validas == 2 and list(validas["producto"]) == ["Arroz", "Leche"]   # sin NIT y al revés


EVALUAR = {"cartera": calidad.evaluar, "ventas": calidad.evaluar_ventas, "lineas": calidad.evaluar_lineas,
           "inventario": calidad.evaluar_inventario, "pedidos": calidad.evaluar_pedidos}


def cargar(motor, nombre, corte, tipo):
    pl = ontologia.cargar_plantilla(objeto=paquetes.REPORTES[tipo]["objeto"])
    crudo = ingesta.leer_archivo((PRUEBA / nombre).read_bytes(), nombre)
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns), pl)
    informe, validas = EVALUAR[tipo](ingesta.convertir(crudo, mapeo, pl), mapeo, pl)
    assert informe.puede_cargar and informe.filas_validas == informe.filas_total
    return decisiones.registrar_carga(motor, validas, informe, mapeo, corte, nombre, "prueba", tipo)


def test_flujo_completo_de_despacho(motor):
    cargar(motor, "credito_y_cobro_cartera_2026-10-31.csv", CORTE, "cartera")
    cargar(motor, "foco_comercial_ventas_2026-10-31.csv", CORTE, "ventas")
    cargar(motor, "margen_ventas_por_producto_2026-10-31.csv", CORTE, "lineas")
    cargar(motor, "inventario_existencias_2026-10-31.csv", CORTE, "inventario")
    r = cargar(motor, "despacho_pedidos_2026-10-31.csv", CORTE, "pedidos")
    assert r["paquetes"] == ["despacho"]
    lista = {d["cliente"]: d for d in decisiones.listar(motor, None, paquete="despacho", tipo="despachar_pedidos")}
    assert lista["Constructora Río Cauca"]["evidencia"]["bloqueo"] == "credito"
    assert lista["Distribuidora Cañaveral"]["evidencia"]["bloqueo"] == "cupo"
    assert lista["Hotel Plaza Cali"]["evidencia"]["bloqueo"] == "inventario"
    assert lista["Empaques Candelaria"]["evidencia"]["bloqueo"] is None
    assert "Mercados La 14 Sur" not in lista                      # su pedido todavía no vence
    cumplir = {d["cliente"] for d in decisiones.listar(motor, None, paquete="despacho", tipo="recuperar_cumplimiento")}
    assert cumplir == {"Licorera del Sur", "Hotel Plaza Cali", "Textiles Ginebra"}

    canaveral = lista["Distribuidora Cañaveral"]
    decisiones.registrar_accion(motor, canaveral["id"], "aprobar", "gerente")
    r = cargar(motor, "despacho_pedidos_2026-11-14.csv", C2, "pedidos")
    assert r["resultados_medidos"] == 10
    with motor.connect() as cx:
        fila = cx.execute(select(resultados_despacho).where(resultados_despacho.c.decision_id == canaveral["id"])
                          ).mappings().one()
    assert fila["atendida"] and fila["despachados"] == 1 and fila["dias_atraso"] == 4
    res = despacho.resumen(motor)
    assert res["hoy"]["atrasados"] < res["base"]["atrasados"] and res["valor_despachado_atendidas"] == 9 * M
    assert res["clientes_mejoraron"] == 3
    abiertas = {d["cliente"] for d in decisiones.listar(motor, paquete="despacho", tipo="despachar_pedidos")}
    assert abiertas == {"Constructora Río Cauca"}                 # el único pedido que sigue retenido
