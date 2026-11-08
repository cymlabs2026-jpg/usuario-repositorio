"""Etapa 0 del aprendizaje: foto de variables, motivos estructurados, recaudos y dataset de entrenamiento."""
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select, text

from ontara import aprendizaje, calidad, db, decisiones, ingesta, ontologia, reglas
from ontara.db import acciones

PRUEBA = Path(__file__).resolve().parent.parent / "datos_ejemplo" / "prueba"
C1, C2 = date(2026, 10, 31), date(2026, 11, 14)
EVALUAR = {"cartera": (calidad.evaluar, "Factura"), "recaudos": (calidad.evaluar_recaudos, "Recaudo"),
           "ventas": (calidad.evaluar_ventas, "Venta")}


def preparar(nombre: str, tipo: str):
    evaluar, objeto = EVALUAR[tipo]
    plantilla = ontologia.cargar_plantilla(objeto=objeto)
    crudo = ingesta.leer_archivo((PRUEBA / nombre).read_bytes(), nombre)
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns), plantilla)
    datos = ingesta.convertir(crudo, mapeo, plantilla)
    informe, validas = evaluar(datos, mapeo, plantilla)
    return informe, validas, mapeo


def cargar(motor, nombre, corte, tipo="cartera"):
    informe, validas, mapeo = preparar(nombre, tipo)
    return decisiones.registrar_carga(motor, validas, informe, mapeo, corte, nombre, "prueba", tipo)


@pytest.fixture
def motor():
    return db.crear_motor("sqlite://")


def test_reporte_de_recaudos_se_reconoce_y_se_carga(motor):
    informe, validas, mapeo = preparar("prueba_recaudos_2026-11-14.csv", "recaudos")
    assert {"nit", "factura", "fecha_pago", "valor", "recibo"} <= set(mapeo)
    assert informe.semaforo == "verde" and informe.filas_validas == informe.filas_total
    cargar(motor, "prueba_cartera_corte_2026-10-31.csv", C1)
    r = cargar(motor, "prueba_recaudos_2026-11-14.csv", C2, "recaudos")
    assert r["decisiones_nuevas"] == 0
    rec = aprendizaje.recaudos_por_factura(motor)
    assert len(rec) == informe.filas_validas
    assert rec["fecha_pago"].between(C1, C2).all()
    # Cargar el mismo reporte dos veces no duplica los pagos.
    cargar(motor, "prueba_recaudos_2026-11-14.csv", C2, "recaudos")
    assert len(aprendizaje.recaudos_por_factura(motor)) == len(rec)


def test_semaforo_de_recaudos_excluye_pagos_sin_cruzar_y_reversiones():
    plantilla = ontologia.cargar_plantilla(objeto="Recaudo")
    crudo = pd.DataFrame({"NIT": ["900123456", "900123456", "800111222", "800111222"],
                          "Factura": ["F1", "", "F2", "F2"], "Fecha pago": ["01/11/2026"] * 4,
                          "Valor": ["$ 100.000", "$ 50.000", "-20.000", "$ 70.000"]})
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns), plantilla)
    informe, validas = calidad.evaluar_recaudos(ingesta.convertir(crudo, mapeo, plantilla), mapeo, plantilla)
    assert list(validas["factura"]) == ["F1", "F2"]
    assert informe.puede_cargar


def test_cada_decision_nueva_guarda_su_foto_de_variables(motor):
    cargar(motor, "prueba_cartera_corte_2026-10-31.csv", C1)
    lista = decisiones.listar(motor, incluir_control=True)
    assert lista and all(d["variables"] for d in lista)
    v = next(d for d in lista if d["tipo"] == "cobrar")["variables"]
    assert {"prioridad", "monto_en_juego", "saldo_vencido", "dias_mora_max", "componentes_prioridad_mora"} <= set(v)
    assert all(isinstance(x, (int, float, bool)) for x in v.values())
    # La foto es la del momento de la detección: el segundo corte no la sobrescribe.
    antes = {d["id"]: d["variables"] for d in lista}
    cargar(motor, "prueba_cartera_corte_2026-11-14.csv", C2)
    despues = {d["id"]: d["variables"] for d in decisiones.listar(motor, None, incluir_control=True)}
    assert all(despues[i] == v for i, v in antes.items())


def test_motivo_y_canal_estructurados(motor):
    cargar(motor, "prueba_cartera_corte_2026-10-31.csv", C1)
    a, b = decisiones.listar(motor)[:2]
    # Con un motivo de la lista el comentario es opcional; con "otro" no.
    assert decisiones.registrar_accion(motor, a["id"], "descartar", "lider", motivo="ya_pago") == "descartada"
    with pytest.raises(ValueError):
        decisiones.registrar_accion(motor, b["id"], "descartar", "lider", motivo="otro")
    with pytest.raises(ValueError):
        decisiones.registrar_accion(motor, b["id"], "descartar", "lider", "x", motivo="inventado")
    decisiones.registrar_accion(motor, b["id"], "aprobar", "lider")
    with pytest.raises(ValueError):
        decisiones.registrar_accion(motor, b["id"], "ejecutar", "lider", "x", canal="paloma")
    decisiones.registrar_accion(motor, b["id"], "ejecutar", "lider", canal="llamada",
                                fecha_compromiso=date(2026, 11, 10), monto_compromiso=1_500_000)
    with motor.connect() as cx:
        fila = cx.execute(select(acciones).where(acciones.c.decision_id == b["id"], acciones.c.tipo == "ejecutar")
                          ).mappings().one()
    assert fila["canal"] == "llamada" and fila["fecha_compromiso"] == date(2026, 11, 10)
    assert fila["monto_compromiso"] == 1_500_000


def test_dataset_une_variables_acciones_y_resultados(motor):
    reglas.guardar_parametros(motor, {"pct_grupo_control": 0}, "gerente", "sin control para la prueba")
    cargar(motor, "prueba_cartera_corte_2026-10-31.csv", C1)
    cobros = decisiones.listar(motor, tipo="cobrar")
    decisiones.registrar_accion(motor, cobros[0]["id"], "aprobar", "lider")
    decisiones.registrar_accion(motor, cobros[0]["id"], "ejecutar", "lider", canal="whatsapp")
    decisiones.registrar_accion(motor, cobros[1]["id"], "descartar", "lider", motivo="acuerdo_vigente")
    cargar(motor, "prueba_cartera_corte_2026-11-14.csv", C2)
    cargar(motor, "prueba_recaudos_2026-11-14.csv", C2, "recaudos")

    ds = aprendizaje.dataset(motor, horizontes=(14, 30))
    assert len(ds) == len(decisiones.listar(motor, None, incluir_control=True))
    assert "nit" not in ds.columns and ds["cliente"].str.startswith("C-").all()
    assert {"v_prioridad", "v_saldo_vencido", "saldo_vencido_14d", "recuperado_14d", "pagado_14d"} <= set(ds.columns)
    assert "saldo_vencido_30d" not in ds.columns or ds["saldo_vencido_30d"].isna().all()   # aún no pasan 30 días

    fila = ds.set_index("decision_id").loc[cobros[0]["id"]]
    assert fila["canal"] == "whatsapp" and fila["atendida"] and fila["primera_accion"] == "aprobar"
    assert fila["dias_medidos_14d"] == 14
    assert fila["recuperado_14d"] == max(0.0, fila["monto_inicial"] - fila["saldo_vencido_14d"])
    assert ds.set_index("decision_id").loc[cobros[1]["id"], "motivo"] == "acuerdo_vigente"
    # Los pagos de los recaudos cuadran con lo que bajó la cartera de quienes pagaron.
    assert ds["pagado_14d"].sum() > 0
    assert (ds.loc[ds["pagado_14d"] > 0, "dias_hasta_primer_pago"] <= 14).all()


def test_preparacion_y_descartes(motor):
    cargar(motor, "prueba_cartera_corte_2026-10-31.csv", C1)
    d = decisiones.listar(motor, tipo="congelar_credito")[0]
    decisiones.registrar_accion(motor, d["id"], "descartar", "gerente", motivo="cliente_estrategico")
    prep = {p["indicador"]: p for p in aprendizaje.preparacion(motor)}
    assert prep["Cortes de cartera cargados"]["valor"] == "1" and not prep["Cortes de cartera cargados"]["listo"]
    assert prep["Descartes y ajustes con motivo"]["valor"] == "100 %"
    desc = aprendizaje.descartes_por_tipo(motor).set_index("tipo")
    assert desc.loc["congelar_credito", "descartadas"] == 1
    assert desc.loc["congelar_credito", "motivo_principal"] == decisiones.MOTIVOS["descartar"]["cliente_estrategico"]


def test_textos_aprobados(motor):
    cargar(motor, "prueba_cartera_corte_2026-10-31.csv", C1)
    d = decisiones.listar(motor, tipo="cobrar")[0]
    aprendizaje.guardar_texto(motor, d["id"], "mensaje_cobro", "Borrador", "Borrador corregido", "lider")
    aprendizaje.guardar_texto(motor, d["id"], "mensaje_cobro", "Igual", "Igual", "lider")
    t = aprendizaje.textos(motor, "mensaje_cobro")
    assert [x["editado"] for x in t] == [False, True]
    with pytest.raises(ValueError):
        aprendizaje.guardar_texto(motor, d["id"], "mensaje_cobro", "x", "  ", "lider")


def test_migracion_agrega_columnas_nuevas_a_una_base_vieja(tmp_path):
    url = f"sqlite:///{tmp_path / 'vieja.db'}"
    m = db.crear_motor(url)
    with m.begin() as cx:                      # simula una base creada antes de la etapa 0
        cx.execute(text("ALTER TABLE acciones DROP COLUMN canal"))
        cx.execute(text("ALTER TABLE decisiones DROP COLUMN variables"))
    m2 = db.crear_motor(url)
    with m2.connect() as cx:
        cols_acc = {r[1] for r in cx.execute(text("PRAGMA table_info(acciones)"))}
        cols_dec = {r[1] for r in cx.execute(text("PRAGMA table_info(decisiones)"))}
    assert "canal" in cols_acc and "variables" in cols_dec
