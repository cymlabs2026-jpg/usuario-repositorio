"""Paquete Caja a 13 semanas."""
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func, select

from ontara import caja, calidad, decisiones, ingesta, ontologia, paquetes, reglas
from ontara.db import proyecciones_caja, resultados_caja

CORTE = date(2026, 10, 31)
C2 = date(2026, 11, 14)
PRUEBA = Path(__file__).resolve().parent.parent / "datos_ejemplo" / "prueba"
M = 1_000_000


@pytest.fixture
def p():
    return reglas.parametros_base("caja")


def factura(nit, numero, vence, saldo):
    return {"nit": nit, "numero": numero, "fecha_emision": vence - timedelta(days=30), "fecha_vencimiento": vence,
            "valor": saldo, "saldo": saldo, "vendedor": None}


def cuenta(nit, proveedor, numero, vence, saldo, categoria="Compras"):
    return {"nit": nit, "proveedor": proveedor, "numero": numero, "fecha_emision": None,
            "fecha_vencimiento": vence, "saldo": saldo, "categoria": categoria}


def proyectar(fact, cxp, p, saldo=200 * M, atrasos=None, venta=0.0):
    cobros = caja.cobros_esperados(pd.DataFrame(fact), CORTE, atrasos or {}, p)
    cxp = pd.DataFrame(cxp, columns=["nit", "proveedor", "numero", "fecha_emision", "fecha_vencimiento", "saldo",
                                     "categoria"])
    return caja.proyectar(CORTE, saldo, cobros, cxp, venta, 30, 0, 30, 80, p), cobros, cxp


# --- Proyección ---------------------------------------------------------------------------
def test_cobros_esperados_segun_como_paga_cada_cliente(p):
    fact = [factura("A", "1", CORTE + timedelta(days=5), 10 * M),        # paga 14 días tarde -> semana 3
            factura("B", "2", CORTE + timedelta(days=5), 10 * M),        # sin historia: 5 días -> semana 2
            factura("C", "3", CORTE - timedelta(days=20), 10 * M),       # vencida 20 días -> en 10 días
            factura("D", "4", CORTE - timedelta(days=120), 10 * M),      # mora alta: 50 % en la semana 6
            factura("E", "5", CORTE - timedelta(days=400), 10 * M)]      # incobrable: no se proyecta
    c = caja.cobros_esperados(pd.DataFrame(fact), CORTE, {"A": 14}, p).set_index("nit")
    assert c.loc["A", "semana"] == 3 and c.loc["B", "semana"] == 2 and c.loc["C", "semana"] == 2
    assert c.loc["D", "semana"] == 6 and c.loc["D", "monto"] == 5 * M
    assert c.loc["E", "estado"] == "incobrable" and c.loc["E", "monto"] == 0


def test_saldo_semana_a_semana(p):
    fact = [factura("A", "1", CORTE + timedelta(days=3), 50 * M)]
    cxp = [cuenta("P", "Prov", "F1", CORTE + timedelta(days=10), 80 * M)]
    proy, _, _ = proyectar(fact, cxp, {**p, "gastos_fijos_mensuales": 52 * M}, atrasos={"A": 0})
    assert len(proy) == 13
    gasto = 12 * M
    assert proy.loc[0, "saldo_final"] == pytest.approx(200 * M + 50 * M - gasto)
    assert proy.loc[1, "saldo_final"] == pytest.approx(200 * M + 50 * M - 80 * M - 2 * gasto)


def test_ventas_y_compras_nuevas_llegan_con_su_plazo():
    assert caja._fraccion_llegada(5, 35) == 0 and caja._fraccion_llegada(6, 35) == 1
    assert caja._fraccion_llegada(5, 31.5) == pytest.approx(0.5)


# --- Reglas ------------------------------------------------------------------------------------
def test_semana_en_rojo_con_palancas(p):
    fact = [factura("A", "1", CORTE - timedelta(days=10), 20 * M)]       # vencida, se espera en la semana 2
    cxp = [cuenta("P1", "Molinos", "F1", CORTE + timedelta(days=25), 150 * M),
           cuenta("P2", "Empaques", "F2", CORTE + timedelta(days=24), 30 * M),
           cuenta("N", "Nómina", "NOM", CORTE + timedelta(days=24), 60 * M, "Nómina")]
    proy, cobros, cxp_df = proyectar(fact, cxp, p, saldo=120 * M)
    res = caja.evaluar_caja(proy, cobros, cxp_df, CORTE, p, {"A": "Cliente A"})
    rojo = next(r for r in res if r.tipo == "caja_bajo_minimo")
    ev = rojo.evidencia
    assert ev["semana_desde"] == 4 and rojo.saldo_vencido == pytest.approx(200 * M)
    assert ev["cobrar"] == []                       # la única factura ya se espera antes de la semana en rojo
    assert {a["proveedor"] for a in ev["aplazar"]} == {"Molinos", "Empaques"}   # la nómina nunca se aplaza
    assert ev["financiar"] == pytest.approx(20 * M)
    assert "crédito de tesorería" in rojo.recomendacion and rojo.nivel_aprobacion == "gerente"
    prov = next(r for r in res if r.tipo == "aplazar_pago_proveedor")
    assert prov.nit == "P1" and "correr 30 días" in prov.recomendacion


def test_aplazamiento_parcial_si_todos_son_grandes(p):
    cxp = [cuenta("P1", "Molinos", "F1", CORTE + timedelta(days=25), 150 * M)]
    proy, cobros, cxp_df = proyectar([], cxp, p, saldo=230 * M)
    rojo = next(r for r in caja.evaluar_caja(proy, cobros, cxp_df, CORTE, p, {})
                if r.tipo == "caja_bajo_minimo")
    assert rojo.evidencia["aplazar"][0]["parcial"] and rojo.evidencia["aplazar"][0]["saldo"] == 20 * M


def test_cobrar_lo_que_llegaria_tarde(p):
    fact = [factura("A", "1", CORTE + timedelta(days=5), 40 * M)]        # paga 40 días tarde: semana 7
    cxp = [cuenta("P1", "Molinos", "F1", CORTE + timedelta(days=25), 100 * M)]
    proy, cobros, cxp_df = proyectar(fact, cxp, p, saldo=150 * M, atrasos={"A": 40})
    otras = {"A": [{"id": 5, "tipo": "cobrar", "evidencia": {}},
                   {"id": 8, "tipo": "ampliar_cupo", "evidencia": {"aumento_sugerido": 15 * M}}]}
    rojo = next(r for r in caja.evaluar_caja(proy, cobros, cxp_df, CORTE, p, {"A": "Cliente A"}, otras)
                if r.tipo == "caja_bajo_minimo")
    assert rojo.evidencia["cobrar"] == [{"nit": "A", "cliente": "Cliente A", "monto": 40 * M, "decision_cartera": 5}]
    assert "ampliaciones de cupo" in rojo.evidencia["cruces"][0]


def test_excedente_de_caja(p):
    res = caja.evaluar_caja(*proyectar([], [], p, saldo=500 * M), CORTE, p, {})
    assert [r.tipo for r in res] == ["excedente_de_caja"] and res[0].saldo_vencido == 400 * M
    assert caja.evaluar_caja(*proyectar([], [], p, saldo=300 * M), CORTE, p, {}) == []


# --- Calidad y carga ------------------------------------------------------------------------------
def test_semaforo_de_cuentas_por_pagar():
    pl = ontologia.cargar_plantilla(objeto="CuentaPorPagar")
    crudo = pd.DataFrame({"NIT proveedor": ["800111222", "", "800333444"], "Proveedor": ["Uno", "Dos", "Tres"],
                          "Documento": ["1", "2", "3"], "Vence": ["15/11/2026", "20/11/2026", "30/11/2026"],
                          "Saldo": ["$ 1.000.000", "$ 2.000.000", "0"], "Concepto": ["Compras"] * 3})
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns), pl)
    assert {mapeo["nit"], mapeo["proveedor"], mapeo["fecha_vencimiento"]} == {"NIT proveedor", "Proveedor", "Vence"}
    informe, validas = calidad.evaluar_pagos(ingesta.convertir(crudo, mapeo, pl), mapeo, pl)
    assert informe.filas_validas == 1 and informe.semaforo == "rojo"


def test_reporte_de_pagos_exige_saldo_en_bancos(motor):
    informe = calidad.Informe("verde", 0, 0, True, [], [])
    with pytest.raises(ValueError, match="saldo en bancos"):
        decisiones.registrar_carga(motor, pd.DataFrame(), informe, {}, CORTE, "x", "u", "pagos")


# --- Flujo completo con los datos de prueba ----------------------------------------------------------
EVALUAR = {"cartera": calidad.evaluar, "ventas": calidad.evaluar_ventas, "lineas": calidad.evaluar_lineas,
           "pagos": calidad.evaluar_pagos}


def cargar(motor, nombre, corte, tipo, saldo=None):
    pl = ontologia.cargar_plantilla(objeto=paquetes.REPORTES[tipo]["objeto"])
    crudo = ingesta.leer_archivo((PRUEBA / nombre).read_bytes(), nombre)
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns), pl)
    informe, validas = EVALUAR[tipo](ingesta.convertir(crudo, mapeo, pl), mapeo, pl)
    assert informe.puede_cargar and informe.filas_validas == informe.filas_total
    return decisiones.registrar_carga(motor, validas, informe, mapeo, corte, nombre, "prueba", tipo, saldo)


def test_flujo_completo_de_caja(motor):
    cargar(motor, "credito_y_cobro_cartera_2026-10-31.csv", CORTE, "cartera")
    cargar(motor, "foco_comercial_ventas_2026-10-31.csv", CORTE, "ventas")
    cargar(motor, "margen_ventas_por_producto_2026-10-31.csv", CORTE, "lineas")
    r = cargar(motor, "caja_13_semanas_cuentas_por_pagar_2026-10-31.csv", CORTE, "pagos", 250 * M)
    assert r["paquetes"] == ["caja"]
    lista = decisiones.listar(motor, None, paquete="caja")
    rojo = next(d for d in lista if d["tipo"] == "caja_bajo_minimo")
    assert rojo["evidencia"]["inicio"].startswith("2026-12") and rojo["evidencia"]["cobrar"]
    ingenio = next(d for d in lista if d["evidencia"].get("proveedor") == "Ingenio Providencia Comercial")
    assert ingenio["tipo"] == "aplazar_pago_proveedor" and ingenio["cliente"] is None
    assert all(d["grupo"] == "fuera" for d in lista)
    with motor.connect() as cx:
        assert cx.execute(select(func.count()).select_from(proyecciones_caja)).scalar() == 13

    # La caja se recalcula sola cuando llega otro reporte, sin duplicar decisiones.
    r = cargar(motor, "foco_comercial_ventas_2026-10-31.csv", CORTE, "ventas")
    assert len(decisiones.listar(motor, None, paquete="caja")) == len(lista)

    decisiones.registrar_accion(motor, ingenio["id"], "aprobar", "gerente")
    cargar(motor, "credito_y_cobro_cartera_2026-11-14.csv", C2, "cartera")
    r = cargar(motor, "caja_13_semanas_cuentas_por_pagar_2026-11-14.csv", C2, "pagos", 390 * M)
    assert r["resultados_medidos"] >= 2
    with motor.connect() as cx:
        fila = cx.execute(select(resultados_caja).where(resultados_caja.c.decision_id == ingenio["id"])
                          ).mappings().one()
    assert fila["atendida"] and fila["cubierto"] == ingenio["saldo_vencido_inicial"]   # el proveedor aceptó
    res = caja.resumen(motor)
    assert res["pagos_aplazados"] > 0 and res["precision"]["real"] == 390 * M
    assert res["semanas_rojo_hoy"] > 0 and len(res["proyeccion"]) == 13
    assert caja.atrasos_por_cliente(motor, C2)                       # ya hay historia de pagos entre cortes
