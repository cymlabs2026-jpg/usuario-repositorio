from datetime import date

import pandas as pd
import pytest

from ontara import reglas

CORTE = date(2026, 10, 31)


def factura(nit, numero, emision, vence, saldo):
    return {"nit": nit, "numero": numero, "fecha_emision": emision, "fecha_vencimiento": vence, "saldo": saldo}


@pytest.fixture
def p():
    return reglas.parametros_base()


@pytest.fixture
def datos():
    f = pd.DataFrame([
        factura("1", "A1", date(2026, 5, 1), date(2026, 6, 1), 10_000_000),     # 152 días
        factura("1", "A2", date(2026, 10, 20), date(2026, 11, 19), 2_000_000),   # al día, compra reciente
        factura("2", "B1", date(2026, 9, 1), date(2026, 10, 1), 3_000_000),      # 30 días
        factura("3", "C1", date(2026, 10, 1), date(2026, 10, 29), 900_000),     # dentro de la gracia
        factura("4", "D1", date(2026, 8, 1), date(2026, 9, 1), 200_000),        # bajo el umbral
    ])
    cli = {"1": {"nombre": "Uno", "cupo": None}, "2": {"nombre": "Dos", "cupo": 2_000_000},
           "3": {"nombre": "Tres", "cupo": None}, "4": {"nombre": "Cuatro", "cupo": None}}
    return f, cli


def test_prioriza_mas_monto_y_mas_mora(datos, p):
    f, cli = datos
    res = reglas.evaluar_cartera(f, CORTE, p, cli)
    cobros = [r for r in res if r.tipo == "cobrar"]
    assert [r.nit for r in cobros] == ["1", "2"]
    assert cobros[0].prioridad > cobros[1].prioridad
    assert cobros[0].saldo_vencido == 10_000_000


def test_gracia_y_umbral_minimo(datos, p):
    f, cli = datos
    nits = {r.nit for r in reglas.evaluar_cartera(f, CORTE, p, cli)}
    assert "3" not in nits and "4" not in nits


def test_frenar_credito_por_mora_y_compra_reciente_o_sobrecupo(datos, p):
    f, cli = datos
    congelar = {r.nit: r for r in reglas.evaluar_cartera(f, CORTE, p, cli) if r.tipo == "congelar_credito"}
    assert "sigue comprando" in congelar["1"].motivo
    assert "supera el cupo" in congelar["2"].motivo


def test_cliente_estrategico_no_genera_decisiones(datos, p):
    f, cli = datos
    p["clientes_excepcion"] = ["1"]
    assert all(r.nit != "1" for r in reglas.evaluar_cartera(f, CORTE, p, cli))


def test_tendencia_sube_la_prioridad(datos, p):
    f, cli = datos
    sin = {r.nit: r.prioridad for r in reglas.evaluar_cartera(f, CORTE, p, cli) if r.tipo == "cobrar"}
    con = {r.nit: r.prioridad for r in reglas.evaluar_cartera(f, CORTE, p, cli, {"2": 1_000_000})
           if r.tipo == "cobrar"}
    assert con["2"] > sin["2"]


def test_evidencia_trae_facturas_y_version(datos, p):
    f, cli = datos
    r = reglas.evaluar_cartera(f, CORTE, p, cli, regla_version=3)[0]
    assert r.evidencia["regla_version"] == 3
    assert r.evidencia["facturas"][0] == {"numero": "A1", "vencimiento": "2026-06-01",
                                          "dias_mora": 152, "saldo": 10_000_000.0}


def test_mismos_datos_mismas_decisiones(datos, p):
    f, cli = datos
    a = [(r.nit, r.tipo, r.prioridad) for r in reglas.evaluar_cartera(f, CORTE, p, cli)]
    b = [(r.nit, r.tipo, r.prioridad) for r in reglas.evaluar_cartera(f, CORTE, p, cli)]
    assert a == b


def test_versiones_de_parametros(motor):
    v1 = reglas.regla_activa(motor)
    v2 = reglas.guardar_parametros(motor, {"dias_gracia": 10}, "gerente", "más gracia")
    assert v2["version"] == v1["version"] + 1
    assert reglas.regla_activa(motor)["parametros"]["dias_gracia"] == 10
    with pytest.raises(ValueError):
        reglas.guardar_parametros(motor, {"peso_monto": 0.9}, "gerente")
    with pytest.raises(ValueError):
        reglas.guardar_parametros(motor, {"inventado": 1}, "gerente")
