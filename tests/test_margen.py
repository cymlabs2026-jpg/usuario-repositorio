"""Paquete Margen."""
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func, select

from ontara import calidad, decisiones, ingesta, margen, ontologia, paquetes, reglas
from ontara.db import resultados_margen

CORTE = date(2026, 10, 31)
PRUEBA = Path(__file__).resolve().parent.parent / "datos_ejemplo" / "prueba"


def linea(nit, numero, producto, valor, costo, descuento=0.0, dias=10):
    return {"nit": nit, "numero": numero, "fecha": CORTE - timedelta(days=dias), "producto": producto,
            "valor": valor, "costo": costo, "descuento": descuento, "cantidad": 1.0, "vendedor": "Ana"}


@pytest.fixture
def p():
    return reglas.parametros_base("margen")


def evaluar(filas, p, otras=None):
    res = margen.evaluar_margen(pd.DataFrame(filas), CORTE, p, {"A": "Uno", "B": "Dos", "C": "Tres"}, otras)
    return {(r.tipo, r.nit): r for r in res}


M = 1_000_000


def test_cliente_con_margen_bajo(p):
    filas = [linea("A", f"A{i}", "Arroz", 10 * M, 9 * M, 1.5 * M, i) for i in range(3)]     # 10 %
    filas += [linea("B", f"B{i}", "Café", 10 * M, 7 * M, 0, i) for i in range(3)]          # 30 %
    res = evaluar(filas, p)
    r = res[("revisar_margen_cliente", "A")]
    assert r.evidencia["margen_pct"] == 10.0
    assert r.saldo_vencido == pytest.approx(0.08 * 30 * M)            # llevarlo de 10 % a 18 %
    assert "Bajar el descuento" in r.recomendacion                     # 13 % de descuento > 8 %
    assert ("revisar_margen_cliente", "B") not in res


def test_descuento_fuera_de_politica_con_margen_sano(p):
    filas = [linea("C", f"C{i}", "Café", 10 * M, 7 * M, 1.2 * M, i) for i in range(3)]     # 30 %, 10,7 % dcto
    res = evaluar(filas, p)
    r = res[("descuento_fuera_politica", "C")]
    assert r.evidencia["descuento_pct"] == pytest.approx(10.71, abs=0.01)
    assert ("revisar_margen_cliente", "C") not in res


def test_producto_bajo_el_costo(p):
    filas = [linea(n, f"{n}{i}", "Aceite", 5 * M, 5.5 * M, 0, i) for n in ("A", "B") for i in range(2)]
    filas += [linea("C", f"C{i}", "Café", 30 * M, 20 * M, 0, i) for i in range(2)]
    res = evaluar(filas, p)
    ident = margen.id_producto("Aceite")
    r = res[("revisar_margen_producto", ident)]
    assert len(ident) <= 20 and r.nombre == "Aceite"
    assert "por debajo del costo" in r.recomendacion and r.evidencia["lineas_bajo_costo"] == 4
    assert r.evidencia["perdida_bajo_costo"] == 2 * M


def test_umbrales_y_excepciones(p):
    pequeno = [linea("A", "A1", "Arroz", 3 * M, 2.9 * M)]                     # ventas bajo el mínimo
    assert evaluar(pequeno, p) == {}
    filas = [linea("A", f"A{i}", "Gancho", 10 * M, 9.5 * M, 0, i) for i in range(2)]
    p["productos_excepcion"] = ["gancho"]
    p["clientes_excepcion"] = ["A"]
    assert evaluar(filas, p) == {}


def test_delegacion_y_cliente_clave(p):
    p["pct_clientes_clave"] = 50
    grande = [linea("A", f"A{i}", "Arroz", 10 * M, 9 * M, 0, i) for i in range(3)]        # 30 M al 10 %: clave
    chico = [linea("B", f"B{i}", "Arroz", 4 * M, 3.6 * M, 0, i) for i in range(3)]        # 12 M al 10 %
    res = evaluar(grande + chico, p)
    assert res[("revisar_margen_cliente", "A")].nivel_aprobacion == "gerente"
    assert res[("revisar_margen_cliente", "B")].nivel_aprobacion == "lider"                # 0,96 M en juego
    p["aprobacion_lider_max_cop"] = 500_000
    assert evaluar(grande + chico, p)[("revisar_margen_cliente", "B")].nivel_aprobacion == "gerente"


def test_cruces_con_otros_paquetes(p):
    filas = [linea("A", f"A{i}", "Arroz", 10 * M, 9 * M, 0, i) for i in range(3)]
    otras = {"A": [{"id": 3, "tipo": "ampliar_cupo", "evidencia": {}},
                   {"id": 9, "tipo": "crecer_cliente", "evidencia": {}}]}
    cruces = evaluar(filas, p, otras)[("revisar_margen_cliente", "A")].evidencia["cruces"]
    assert "ampliarle el cupo (decisión #3)" in cruces[0] and "decisión #9" in cruces[1]


def test_semaforo_de_lineas():
    pl = ontologia.cargar_plantilla(objeto="LineaVenta")
    crudo = pd.DataFrame({"NIT": ["900123456", "900123456", "800111222"], "Cliente": ["Uno"] * 2 + ["Dos"],
                          "Factura": ["1", "1", "2"], "Fecha": ["01/10/2026"] * 3,
                          "Producto": ["Arroz", "Aceite", "Sal"], "Venta neta": ["$ 1.000", "$ 500", "$ 800"],
                          "Costo": ["$ 700", "$ 600", "0"]})
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns), pl)
    assert {mapeo["valor"], mapeo["costo"], mapeo["producto"]} == {"Venta neta", "Costo", "Producto"}
    informe, validas = calidad.evaluar_lineas(ingesta.convertir(crudo, mapeo, pl), mapeo, pl)
    nombres = {v.nombre: v.filas_afectadas for v in informe.validaciones}
    assert nombres["Costo en cero"] == 1 and nombres["Venta bajo el costo"] == 1
    assert informe.filas_validas == 2


def cargar(motor, nombre, corte, tipo):
    pl = ontologia.cargar_plantilla(objeto=paquetes.REPORTES[tipo]["objeto"])
    crudo = ingesta.leer_archivo((PRUEBA / nombre).read_bytes(), nombre)
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns), pl)
    evaluar_ = {"cartera": calidad.evaluar, "ventas": calidad.evaluar_ventas, "lineas": calidad.evaluar_lineas}[tipo]
    informe, validas = evaluar_(ingesta.convertir(crudo, mapeo, pl), mapeo, pl)
    assert informe.puede_cargar and informe.filas_validas == informe.filas_total
    return decisiones.registrar_carga(motor, validas, informe, mapeo, corte, nombre, "prueba", tipo)


def test_flujo_completo_de_margen(motor):
    c2 = date(2026, 11, 14)
    cargar(motor, "prueba_cartera_corte_2026-10-31.csv", CORTE, "cartera")
    cargar(motor, "prueba_ventas_2026-10-31.csv", CORTE, "ventas")
    r = cargar(motor, "prueba_lineas_2026-10-31.csv", CORTE, "lineas")
    assert r["paquetes"] == ["margen"] and r["decisiones_nuevas"] > 0
    lista = decisiones.listar(motor, None, paquete="margen")
    tipos = {d["tipo"] for d in lista}
    assert tipos == {"revisar_margen_cliente", "revisar_margen_producto", "descuento_fuera_politica"}
    aceite = next(d for d in lista if d["evidencia"].get("producto") == "Aceite de girasol 3 L")
    assert aceite["evidencia"]["margen_pct"] < 0 and aceite["cliente"] is None
    assert any(d["evidencia"]["cruces"] for d in lista)
    assert all(d["grupo"] == "fuera" for d in lista)                         # Margen no usa grupo de control

    cosecha = next(d for d in lista if d["cliente"] == "Supermercados La Cosecha")
    decisiones.registrar_accion(motor, cosecha["id"], "aprobar", "gerente")
    cargar(motor, "prueba_lineas_2026-11-14.csv", c2, "lineas")
    with motor.connect() as cx:
        fila = cx.execute(select(resultados_margen).where(resultados_margen.c.decision_id == cosecha["id"])
                          ).mappings().one()
    assert fila["atendida"] and fila["margen_despues_pct"] > fila["margen_antes_pct"]
    res = margen.resumen(motor)
    assert res["puntos"] > 0 and res["utilidad_adicional_atendidas"] > 0
    assert len(margen.margen_mensual(motor)) == 7
    with motor.connect() as cx:
        assert cx.execute(select(func.count()).select_from(resultados_margen)).scalar() > 1


def test_reporte_de_lineas_sin_margen_activo(motor, monkeypatch):
    monkeypatch.setenv("ONTARA_PAQUETES", "cartera,comercial")
    r = cargar(motor, "prueba_lineas_2026-10-31.csv", CORTE, "lineas")
    assert r["paquetes"] == [] and decisiones.listar(motor, None, paquete="margen") == []
