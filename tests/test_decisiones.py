import pytest
from sqlalchemy import func, select

from ontara import decisiones, impacto, reglas
from ontara.db import linea_base, resultados

from conftest import CORTE_1, CORTE_2


def cargar(motor, corte, datos, nombre="x.csv"):
    informe, validas, mapeo = datos
    return decisiones.registrar_carga(motor, validas, informe, mapeo, corte, nombre, "prueba")


def test_primera_carga_crea_linea_base_y_bandeja(motor, corte1):
    r = cargar(motor, CORTE_1, corte1)
    assert r["linea_base_creada"] and r["decisiones_nuevas"] > 0
    lista = decisiones.listar(motor)
    assert all(d["estado"] == "pendiente" for d in lista)
    assert lista == sorted(lista, key=lambda d: d["prioridad"], reverse=True)
    assert lista[0]["evidencia"]["facturas"]


def test_segundo_corte_actualiza_sin_duplicar_y_cierra_las_pagadas(motor, corte1, corte2):
    cargar(motor, CORTE_1, corte1)
    r2 = cargar(motor, CORTE_2, corte2)
    assert not r2["linea_base_creada"]
    with motor.connect() as cx:
        assert cx.execute(select(func.count()).select_from(linea_base)).scalar() == 1
    abiertas = decisiones.listar(motor)
    claves = [(d["nit"], d["tipo"]) for d in abiertas]
    assert len(claves) == len(set(claves))
    assert r2["decisiones_cerradas"] >= 1
    cerrada = decisiones.listar(motor, ("cerrada",))[0]
    assert decisiones.detalle(motor, cerrada["id"])["acciones"][-1]["tipo"] == "sistema"


def test_resultados_en_pesos_distinguen_atendidas(motor, corte1, corte2):
    reglas.guardar_parametros(motor, {"pct_grupo_control": 0}, "gerente", "sin grupo de control")
    cargar(motor, CORTE_1, corte1)
    cobros = decisiones.listar(motor, tipo="cobrar")
    for d in cobros:
        decisiones.registrar_accion(motor, d["id"], "aprobar", "lider")
    cargar(motor, CORTE_2, corte2)
    with motor.connect() as cx:
        filas = cx.execute(select(resultados)).mappings().all()
    assert filas and all(f["atendida"] for f in filas)
    assert all(f["pesos_recuperados"] == max(0, f["saldo_vencido_antes"] - f["saldo_vencido_despues"])
               for f in filas)
    res = impacto.resumen(motor)
    assert res["pesos_recuperados_atendidas"] > 0 and res["pesos_recuperados_no_atendidas"] == 0


def test_ciclo_de_vida_y_validaciones(motor, corte1):
    cargar(motor, CORTE_1, corte1)
    d = decisiones.listar(motor)[0]
    with pytest.raises(ValueError):
        decisiones.registrar_accion(motor, d["id"], "ejecutar", "lider", "llamé")   # sin aprobar
    with pytest.raises(ValueError):
        decisiones.registrar_accion(motor, d["id"], "descartar", "lider", "  ")    # sin motivo
    assert decisiones.registrar_accion(motor, d["id"], "aprobar", "lider") == "aprobada"
    assert decisiones.registrar_accion(motor, d["id"], "ejecutar", "lider", "Acuerdo a 15 días") == "ejecutada"
    det = decisiones.detalle(motor, d["id"])
    assert [a["tipo"] for a in det["acciones"]] == ["aprobar", "ejecutar"]


def test_descartada_no_reaparece_al_recalcular_el_mismo_corte(motor, corte1):
    r = cargar(motor, CORTE_1, corte1)
    d = decisiones.listar(motor)[0]
    decisiones.registrar_accion(motor, d["id"], "descartar", "lider", "Cliente ya pagó por transferencia")
    decisiones.recalcular(motor, r["carga_id"])
    assert all((x["nit"], x["tipo"]) != (d["nit"], d["tipo"]) for x in decisiones.listar(motor))


def test_cambiar_reglas_y_recalcular(motor, corte1):
    r = cargar(motor, CORTE_1, corte1)
    antes = len(decisiones.listar(motor, tipo="cobrar"))
    reglas.guardar_parametros(motor, {"umbral_cobro_minimo": 20_000_000}, "gerente", "solo grandes")
    decisiones.recalcular(motor, r["carga_id"])
    despues = decisiones.listar(motor, tipo="cobrar")
    assert 0 < len(despues) < antes
    assert all(d["evidencia"]["saldo_vencido"] >= 20_000_000 for d in despues)
    assert all(d["evidencia"]["regla_version"] == 2 for d in despues)
