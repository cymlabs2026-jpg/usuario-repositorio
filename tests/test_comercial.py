"""Base común de paquetes y Foco comercial."""
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func, select

from ontara import calidad, comercial, decisiones, impacto, ingesta, ontologia, paquetes, reglas
from ontara.db import clientes, linea_base, resultados_comercial

CORTE = date(2026, 10, 31)
PRUEBA = Path(__file__).resolve().parent.parent / "datos_ejemplo" / "prueba"


def compras(nit, fechas, valor=1_000_000):
    return [{"nit": nit, "numero": f"{nit}-{i}", "fecha": f, "valor": valor, "vendedor": "Ana"}
            for i, f in enumerate(fechas)]


def cada(dias, desde, hasta):
    f, out = desde, []
    while f <= hasta:
        out.append(f)
        f += timedelta(days=dias)
    return out


@pytest.fixture
def p():
    return reglas.parametros_base("comercial")


def evaluar(filas, p, cartera=None):
    hist = pd.DataFrame(filas)
    return {r.nit: r for r in comercial.evaluar_comercial(hist, CORTE, p, {}, cartera)}


INICIO = CORTE - timedelta(days=360)


# --- Reglas -----------------------------------------------------------------------------
def test_cliente_que_dejo_de_comprar(p):
    filas = compras("A", cada(10, INICIO, CORTE - timedelta(days=60)))     # compraba cada 10 días
    filas += compras("B", cada(10, INICIO, CORTE))                          # sigue igual
    res = evaluar(filas, p)
    assert res["A"].tipo == "recuperar_cliente" and "sin comprar" in res["A"].motivo
    assert res["A"].evidencia["frecuencia_dias"] == 10
    assert "B" not in res


def test_caida_de_volumen_a_su_ritmo(p):
    filas = [dict(x, valor=400_000 if x["fecha"] > CORTE - timedelta(days=90) else 1_000_000)
             for x in compras("C", cada(7, INICIO, CORTE))]
    r = evaluar(filas, p)["C"]
    assert r.tipo == "recuperar_cliente" and "cayeron 60%" in r.motivo


def test_cliente_que_volvio_tras_un_hueco_no_alerta(p):
    fechas = cada(7, INICIO, CORTE - timedelta(days=60)) + cada(7, CORTE - timedelta(days=10), CORTE)
    assert "D" not in evaluar(compras("D", fechas), p)


def test_cliente_que_crece(p):
    filas = [dict(x, valor=1_600_000 if x["fecha"] > CORTE - timedelta(days=90) else 1_000_000)
             for x in compras("E", cada(7, INICIO, CORTE))]
    r = evaluar(filas, p)["E"]
    assert r.tipo == "crecer_cliente" and r.saldo_vencido > 0


def test_filtros_de_tamano_y_de_historia(p):
    pocas = compras("F", [INICIO, INICIO + timedelta(days=30), INICIO + timedelta(days=60)], 9_000_000)
    pequeno = compras("G", cada(10, INICIO, CORTE - timedelta(days=80)), 100_000)
    assert evaluar(pocas + pequeno, p) == {}


def test_cruce_con_cartera(p):
    filas = compras("A", cada(10, INICIO, CORTE - timedelta(days=60)))
    cartera = {"A": [{"id": 7, "tipo": "congelar_credito", "evidencia": {}, "recomendacion": ""}]}
    r = evaluar(filas, p, cartera)["A"]
    assert "frenarle el crédito (decisión #7)" in r.evidencia["cruces_cartera"][0]


def test_mismos_datos_mismas_decisiones(p):
    filas = compras("A", cada(10, INICIO, CORTE - timedelta(days=60))) + compras("C", cada(7, INICIO, CORTE))
    a = [(r.nit, r.tipo, r.prioridad) for r in evaluar(filas, p).values()]
    assert a == [(r.nit, r.tipo, r.prioridad) for r in evaluar(filas, p).values()]


# --- Calidad del reporte de ventas ----------------------------------------------------------
def test_semaforo_de_ventas():
    pl = ontologia.cargar_plantilla(objeto="Venta")
    crudo = pd.DataFrame({"NIT": ["900123456-1", "x", "800111222"], "Cliente": ["Uno", "Dos", "Tres"],
                          "Factura": ["1", "2", "3"], "Fecha": ["01/10/2026", "02/10/2026", "03/10/2026"],
                          "Total": ["$ 1.000.000", "$ 500.000", "-$ 200.000"]})
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns), pl)
    assert mapeo == {"nit": "NIT", "cliente": "Cliente", "numero": "Factura", "fecha": "Fecha", "valor": "Total"}
    informe, validas = calidad.evaluar_ventas(ingesta.convertir(crudo, mapeo, pl), mapeo, pl)
    assert informe.filas_validas == 2 and informe.semaforo == "rojo"           # 1 de 3 sin NIT = 33 %
    assert next(v for v in informe.validaciones if v.nombre == "Notas crédito").filas_afectadas == 1
    informe, _ = calidad.evaluar_ventas(ingesta.convertir(crudo, {"nit": "NIT"}, pl), {"nit": "NIT"}, pl)
    assert not informe.puede_cargar and "valor" in informe.faltan_campos


# --- Flujo completo con los datos de prueba ---------------------------------------------------
def cargar(motor, nombre, corte, tipo):
    pl = ontologia.cargar_plantilla(objeto=paquetes.REPORTES[tipo]["objeto"])
    crudo = ingesta.leer_archivo((PRUEBA / nombre).read_bytes(), nombre)
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns), pl)
    datos = ingesta.convertir(crudo, mapeo, pl)
    informe, validas = (calidad.evaluar if tipo == "cartera" else calidad.evaluar_ventas)(datos, mapeo, pl)
    assert informe.semaforo == "verde"
    return decisiones.registrar_carga(motor, validas, informe, mapeo, corte, nombre, "prueba", tipo)


def test_ventas_primero_no_crea_linea_base(motor):
    r = cargar(motor, "foco_comercial_ventas_2026-10-31.csv", CORTE, "ventas")
    assert r["paquetes"] == ["comercial"] and r["decisiones_nuevas"] > 0 and not r["linea_base_creada"]
    with motor.connect() as cx:
        assert cx.execute(select(func.count()).select_from(linea_base)).scalar() == 0
    assert impacto.resumen(motor)["linea_base"] is None


def test_flujo_cartera_y_ventas(motor):
    cargar(motor, "credito_y_cobro_cartera_2026-10-31.csv", CORTE, "cartera")
    with motor.connect() as cx:
        cupos = dict(cx.execute(select(clientes.c.nit, clientes.c.cupo)).all())
    clave_antes = {d["nit"] for d in decisiones.listar(motor, None, paquete="cartera")
                   if d["evidencia"]["cliente_clave"]}
    cargar(motor, "foco_comercial_ventas_2026-10-31.csv", CORTE, "ventas")

    # El reporte de ventas no borra los cupos y recalcula los clientes clave de Cartera con todas las compras.
    with motor.connect() as cx:
        assert dict(cx.execute(select(clientes.c.nit, clientes.c.cupo)).all()) == cupos
    clave_despues = {d["nit"] for d in decisiones.listar(motor, None, paquete="cartera")
                     if d["evidencia"]["cliente_clave"]}
    assert clave_despues != clave_antes

    com = decisiones.listar(motor, None, paquete="comercial", incluir_control=True)
    assert {d["tipo"] for d in com} == {"recuperar_cliente", "crecer_cliente"}
    assert any(d["evidencia"]["cruces_cartera"] for d in com)
    assert all(d["grupo"] != "control" for d in com if d["evidencia"]["cliente_clave"])
    assert all(d["paquete"] == "cartera" for d in decisiones.listar(motor, None, paquete="cartera"))

    # Segundo corte de cada reporte: se miden resultados y se cierran los clientes que volvieron.
    cargar(motor, "credito_y_cobro_cartera_2026-11-14.csv", date(2026, 11, 14), "cartera")
    r = cargar(motor, "foco_comercial_ventas_2026-11-14.csv", date(2026, 11, 14), "ventas")
    assert r["resultados_medidos"] > 0 and r["decisiones_cerradas"] > 0
    with motor.connect() as cx:
        volvieron = cx.execute(select(func.count()).select_from(resultados_comercial)
                               .where(resultados_comercial.c.volvio_a_comprar)).scalar()
    assert volvieron > 0
    res = comercial.resumen(motor)
    assert res["alertas_medidas"] > 0 and res["n_tratamiento"] > 0
    # Las métricas de cartera no se mezclan con los reportes de ventas.
    assert len(impacto.historial(motor)) == 2


def test_carga_anterior_es_del_mismo_reporte(motor):
    cargar(motor, "credito_y_cobro_cartera_2026-10-31.csv", CORTE, "cartera")
    cargar(motor, "foco_comercial_ventas_2026-10-31.csv", CORTE, "ventas")
    c2 = cargar(motor, "credito_y_cobro_cartera_2026-11-14.csv", date(2026, 11, 14), "cartera")["carga_id"]
    assert decisiones._carga_anterior(motor, c2) == 1


def test_tipo_de_reporte_invalido(motor):
    informe = calidad.Informe("verde", 0, 0, True, [], [])
    with pytest.raises(ValueError):
        decisiones.registrar_carga(motor, pd.DataFrame(), informe, {}, CORTE, "x", "u", "despachos")


# --- Registro de paquetes y reglas ------------------------------------------------------------
def test_paquetes_activos(monkeypatch):
    assert [p.clave for p in paquetes.activos()] == ["cartera", "comercial", "margen", "caja", "inventario"]
    monkeypatch.setenv("ONTARA_PAQUETES", "cartera")
    assert [p.clave for p in paquetes.activos()] == ["cartera"]
    assert paquetes.de_reporte("ventas") == []


def test_solo_cartera_activo_ignora_ventas(motor, monkeypatch):
    monkeypatch.setenv("ONTARA_PAQUETES", "cartera")
    r = cargar(motor, "foco_comercial_ventas_2026-10-31.csv", CORTE, "ventas")
    assert r["paquetes"] == [] and decisiones.listar(motor, None) == []


def test_reglas_comerciales_versionadas(motor):
    v = reglas.guardar_parametros(motor, {"factor_atraso": 2.0}, "gerente", "más tolerancia", paquete="comercial")
    assert v["version"] == 2 and reglas.regla_activa(motor, "comercial")["parametros"]["factor_atraso"] == 2.0
    assert reglas.regla_activa(motor)["version"] == 1                       # cartera no cambia
    with pytest.raises(ValueError):
        reglas.guardar_parametros(motor, {"pct_grupo_control": 70}, "gerente", paquete="comercial")
    assert "factor_atraso" in reglas.descripciones("comercial")


def test_grupo_de_control_excluye_clientes_clave(p):
    r = reglas.ResultadoRegla("1", "", "recuperar_cliente", 0, "", "", 1.0, {"cliente_clave": True})
    assert decisiones.asignar_grupo(r, 1, {**p, "pct_grupo_control": 50}, {"recuperar_cliente"}) == "fuera"
    r.evidencia["cliente_clave"] = False
    grupos = {decisiones.asignar_grupo(r, c, {**p, "pct_grupo_control": 50}, {"recuperar_cliente"})
              for c in range(20)}
    assert grupos == {"control", "tratamiento"}
