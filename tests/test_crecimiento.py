"""Tanda 1: ampliar cupo, valor del cliente, delegación, grupo de control, caja liberada y acceso."""
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert, inspect, text

from ontara import acceso, db, decisiones, impacto, reglas
from ontara.db import reglas_version

from conftest import CORTE_1, CORTE_2

CORTE = date(2026, 10, 31)


def f(nit, numero, emision, vence, saldo, valor=None):
    return {"nit": nit, "numero": numero, "fecha_emision": emision, "fecha_vencimiento": vence,
            "saldo": saldo, "valor": valor}


@pytest.fixture
def p():
    return reglas.parametros_base()


@pytest.fixture
def datos():
    fact = pd.DataFrame([
        # Cliente G: al día, usa 90 % del cupo y compró hace 10 días -> ampliar cupo
        f("G", "G1", date(2026, 10, 21), date(2026, 11, 20), 45_000_000, 45_000_000),
        # Cliente S: al día pero pasado del cupo -> ampliar, no frenar
        f("S", "S1", date(2026, 10, 25), date(2026, 11, 24), 12_000_000, 12_000_000),
        # Cliente M: 152 días de mora y sigue comprando -> frenar y cobrar
        f("M", "M1", date(2026, 5, 1), date(2026, 6, 1), 30_000_000, 30_000_000),
        f("M", "M2", date(2026, 10, 20), date(2026, 11, 19), 2_000_000, 2_000_000),
        # Cliente P: pequeño, en mora, usa todo el cupo -> no se le amplía
        f("P", "P1", date(2026, 9, 1), date(2026, 10, 1), 3_000_000, 3_000_000),
    ])
    cli = {"G": {"nombre": "Grande", "cupo": 50_000_000}, "S": {"nombre": "Sobrecupo", "cupo": 10_000_000},
           "M": {"nombre": "Moroso", "cupo": None}, "P": {"nombre": "Pequeño", "cupo": 3_000_000}}
    return fact, cli


def por_tipo(res, tipo):
    return {r.nit: r for r in res if r.tipo == tipo}


# --- Ampliar cupo ----------------------------------------------------------------
def test_ampliar_cupo_a_buen_pagador_que_usa_su_cupo(datos, p):
    fact, cli = datos
    amp = por_tipo(reglas.evaluar_cartera(fact, CORTE, p, cli), "ampliar_cupo")
    assert "G" in amp and "P" not in amp
    g = amp["G"]
    assert g.evidencia["aumento_sugerido"] == 15_000_000          # 30 % de 50 millones
    assert g.evidencia["cupo_sugerido"] == 65_000_000
    assert "usa el 90% de su cupo" in g.motivo


def test_sobrecupo_al_dia_amplia_en_vez_de_frenar(datos, p):
    fact, cli = datos
    res = reglas.evaluar_cartera(fact, CORTE, p, cli)
    assert "S" in por_tipo(res, "ampliar_cupo")
    assert "S" not in por_tipo(res, "congelar_credito")
    p["ampliar_activo"] = False
    assert "S" in por_tipo(reglas.evaluar_cartera(fact, CORTE, p, cli), "congelar_credito")


def test_tope_del_aumento(datos, p):
    fact, cli = datos
    p["ampliar_maximo_cop"] = 5_000_000
    g = por_tipo(reglas.evaluar_cartera(fact, CORTE, p, cli), "ampliar_cupo")["G"]
    assert g.evidencia["aumento_sugerido"] == 5_000_000


# --- Valor del cliente -------------------------------------------------------------
def test_compras_cuentan_cada_factura_una_vez_y_usan_la_ventana():
    hist = pd.DataFrame([
        f("A", "1", date(2026, 10, 1), date(2026, 10, 31), 1_000_000, 4_000_000),
        f("A", "1", date(2026, 10, 1), date(2026, 10, 31), 500_000, 4_000_000),     # mismo número, otro corte
        f("A", "2", date(2025, 6, 1), date(2025, 7, 1), 9_000_000, 9_000_000),      # fuera de 12 meses
        f("B", "3", date(2026, 9, 1), date(2026, 10, 1), 2_000_000, None),          # sin valor: usa saldo
    ])
    assert reglas.compras_por_cliente(hist, CORTE, 12) == {"A": 4_000_000.0, "B": 2_000_000.0}


def test_clientes_clave_curva_abc():
    compras = {"A": 60.0, "B": 25.0, "C": 10.0, "D": 5.0}
    assert reglas.clientes_clave(compras, 80) == {"A", "B"}
    assert reglas.clientes_clave(compras, 50) == {"A"}
    assert reglas.clientes_clave({}, 80) == set()


def test_frenar_a_cliente_clave_lo_aprueba_el_gerente(datos, p):
    fact, cli = datos
    res = reglas.evaluar_cartera(fact, CORTE, p, cli, compras={"M": 900.0, "G": 50.0, "P": 50.0})
    m = por_tipo(res, "congelar_credito")["M"]
    assert m.evidencia["cliente_clave"] and m.nivel_aprobacion == "gerente"
    assert m.recomendacion.startswith("Revisar con el gerente")
    assert "Cliente clave" in por_tipo(res, "cobrar")["M"].recomendacion


# --- Matriz de delegación ------------------------------------------------------------
def test_nivel_de_aprobacion_por_monto(p):
    assert reglas.nivel_aprobacion("cobrar", 5_000_000, False, p) == "lider"
    assert reglas.nivel_aprobacion("cobrar", 25_000_000, False, p) == "gerente"
    assert reglas.nivel_aprobacion("congelar_credito", 1_000, True, p) == "gerente"
    assert reglas.puede_aprobar("gerente", "gerente") and not reglas.puede_aprobar("lider", "gerente")


def cargar(motor, corte, datos):
    informe, validas, mapeo = datos
    return decisiones.registrar_carga(motor, validas, informe, mapeo, corte, "x.csv", "prueba")


def test_lider_no_aprueba_lo_que_le_toca_al_gerente(motor, corte1):
    cargar(motor, CORTE_1, corte1)
    d = decisiones.listar(motor, nivel="gerente")[0]
    with pytest.raises(ValueError, match="la aprueba el gerente"):
        decisiones.registrar_accion(motor, d["id"], "aprobar", "maria", rol="lider")
    assert decisiones.registrar_accion(motor, d["id"], "nota", "maria", "Escalo al gerente", rol="lider") == "pendiente"
    assert decisiones.registrar_accion(motor, d["id"], "aprobar", "jefe", rol="gerente") == "aprobada"


# --- Grupo de control -----------------------------------------------------------------
def test_grupo_de_control_oculto_reproducible_y_sin_acciones(motor, corte1, corte2):
    reglas.guardar_parametros(motor, {"pct_grupo_control": 50}, "gerente", "más control para la prueba")
    cargar(motor, CORTE_1, corte1)
    todas = decisiones.listar(motor, None, incluir_control=True)
    control = [d for d in todas if d["grupo"] == "control"]
    assert control and all(d["tipo"] == "cobrar" for d in control)
    assert all(d["evidencia"]["saldo_vencido"] <= 20_000_000 for d in control)
    visibles = {d["id"] for d in decisiones.listar(motor, None)}
    assert not visibles & {d["id"] for d in control}
    with pytest.raises(ValueError, match="grupo de control"):
        decisiones.registrar_accion(motor, control[0]["id"], "aprobar", "gerente")
    # Misma asignación con los mismos datos
    r = reglas.ResultadoRegla(control[0]["nit"], "", "cobrar", 0, "", "", 1.0)
    params = reglas.regla_activa(motor)["parametros"]
    assert decisiones.asignar_grupo(r, control[0]["carga_origen_id"], params) == "control"
    # Fija por cliente: en cualquier corte posterior el mismo cliente sigue en control.
    assert all(decisiones.asignar_grupo(r, c, params) == "control" for c in range(2, 30))
    # Un experimento nuevo vuelve a sortear.
    nuevos = {decisiones.asignar_grupo(reglas.ResultadoRegla(d["nit"], "", "cobrar", 0, "", "", 1.0), 1,
                                       {**params, "experimento_control": 2}) for d in todas
              if d["tipo"] == "cobrar" and d["grupo"] != "fuera"}
    assert nuevos <= {"control", "tratamiento"}
    cargar(motor, CORTE_2, corte2)
    ctl = impacto.resumen(motor)["control"]
    assert ctl["n_control"] > 0 and ctl["n_tratamiento"] > 0
    assert ctl["efecto_estimado"] is not None


def test_comparar_control_calcula_el_efecto(motor):
    assert impacto.comparar_control(motor)["efecto_estimado"] is None


# --- Caja liberada ---------------------------------------------------------------------
def test_dias_de_cartera_y_caja_liberada():
    fact = pd.DataFrame([f("A", "1", date(2026, 10, 15), date(2026, 11, 14), 3_000_000, 3_000_000),
                         f("A", "2", date(2026, 8, 1), date(2026, 9, 1), 3_000_000, 3_000_000)])
    m = impacto.metricas(fact, CORTE)
    assert m["ventas_diarias_estimadas"] == 100_000          # 3 millones en 30 días
    assert m["dias_cartera"] == 60.0
    base, hoy = {"cartera_total": 6_000_000, "dias_cartera": 60.0}, {"cartera_total": 4_000_000,
                                                                      "dias_cartera": 40.0,
                                                                      "ventas_diarias_estimadas": 100_000}
    assert impacto.caja_liberada(base, hoy)["pesos"] == 2_000_000
    con_contabilidad = impacto.caja_liberada(base, hoy, ventas_dia=200_000)
    assert con_contabilidad["dias_ganados"] == 10.0 and con_contabilidad["pesos"] == 2_000_000
    assert con_contabilidad["fuente_ventas"] == "contabilidad"


def test_resumen_incluye_caja(motor, corte1, corte2):
    cargar(motor, CORTE_1, corte1)
    cargar(motor, CORTE_2, corte2)
    caja = impacto.resumen(motor)["caja"]
    assert caja["dias_base"] and caja["dias_hoy"] and caja["fuente_ventas"] == "estimada"


# --- Reglas y migración ------------------------------------------------------------------
def test_version_antigua_hereda_parametros_nuevos(motor):
    viejos = {k: v for k, v in reglas.parametros_base().items() if not k.startswith("ampliar")}
    with motor.begin() as cx:
        cx.execute(insert(reglas_version).values(paquete="cartera", version=1, parametros=viejos,
                                                 creada_por="sistema"))
    assert reglas.regla_activa(motor)["parametros"]["ampliar_activo"] is True


def test_validacion_de_parametros_nuevos(motor):
    with pytest.raises(ValueError):
        reglas.guardar_parametros(motor, {"pct_grupo_control": 80}, "gerente")
    with pytest.raises(ValueError):
        reglas.guardar_parametros(motor, {"aprobacion_lider_max_cop": -1}, "gerente")


def test_migra_una_base_existente(tmp_path):
    url = f"sqlite:///{tmp_path / 'vieja.db'}"
    viejo = create_engine(url)
    with viejo.begin() as cx:
        cx.execute(text("CREATE TABLE decisiones (id INTEGER PRIMARY KEY, paquete VARCHAR(40))"))
        cx.execute(text("INSERT INTO decisiones (id, paquete) VALUES (1, 'cartera')"))
    motor = db.crear_motor(url)
    columnas = {c["name"] for c in inspect(motor).get_columns("decisiones")}
    assert {"nivel_aprobacion", "grupo"} <= columnas
    with motor.connect() as cx:
        assert cx.execute(text("SELECT grupo FROM decisiones")).scalar() == "tratamiento"


# --- Acceso ---------------------------------------------------------------------------------
def test_claves_con_sal_y_verificacion():
    h1, h2 = acceso.hash_clave("Secreta1", iteraciones=1000), acceso.hash_clave("Secreta1", iteraciones=1000)
    assert h1 != h2 and h1.startswith("pbkdf2_sha256$1000$")
    assert acceso.verificar_clave("Secreta1", h1) and not acceso.verificar_clave("otra", h1)
    import hashlib
    assert not acceso.verificar_clave("Secreta1", hashlib.sha256(b"Secreta1").hexdigest())   # formato viejo
    assert not acceso.verificar_clave("x", None)


def test_roles_y_autenticacion():
    lista = {"maria@empresa.com": {"rol": "lider"},
             "jefe": {"rol": "gerente", "clave": acceso.hash_clave("Clave9", iteraciones=1000)}}
    assert acceso.rol_de("Maria@Empresa.com ", lista) == "lider"
    assert acceso.rol_de("otro@empresa.com", lista) is None
    assert acceso.autenticar_con_clave("jefe", "Clave9", lista) == "gerente"
    assert acceso.autenticar_con_clave("jefe", "mal", lista) is None
    assert acceso.autenticar_con_clave("maria@empresa.com", "", lista) is None    # sin clave: solo OIDC


def test_usuarios_desde_variable(monkeypatch):
    monkeypatch.setenv("ONTARA_USUARIOS", '{"Ana@X.co": {"rol": "gerente"}}')
    assert acceso.rol_de("ana@x.co") == "gerente"


def test_bitacora(motor):
    acceso.registrar(motor, "maria@empresa.com", "ingreso", "oidc")
    acceso.registrar(motor, "intruso", "fallido", "clave")
    b = acceso.bitacora(motor)
    assert [x["evento"] for x in b] == ["fallido", "ingreso"]
