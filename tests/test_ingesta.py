from datetime import date

import pandas as pd
import pytest

from ontara import ingesta, ontologia


@pytest.mark.parametrize("texto,esperado", [
    ("$ 1.234.567", 1234567.0), ("1.234.567,50", 1234567.5), ("1,234,567.50", 1234567.5),
    ("2.500", 2500.0), ("2,5", 2.5), ("(15.000)", -15000.0), ("", None), ("abc", None), (3200, 3200.0),
])
def test_pesos_en_formatos_colombianos(texto, esperado):
    assert ingesta.a_pesos(texto) == esperado


@pytest.mark.parametrize("texto,esperado", [
    ("900.123.456-7", "900123456"), ("900123456", "900123456"), (" 16.789.012 ", "16789012"),
    ("123", None), ("", None), (None, None),
])
def test_nit_sin_digito_de_verificacion(texto, esperado):
    assert ingesta.a_nit(texto) == esperado


def test_fechas_dia_mes_e_iso():
    assert ingesta.a_fecha("05/10/2026") == date(2026, 10, 5)
    assert ingesta.a_fecha("2026-10-05") == date(2026, 10, 5)
    assert ingesta.a_fecha("no es fecha") is None


def test_mapeo_reconoce_exporte_tipo_siigo():
    cols = ["NIT", "Nombre del tercero", "Factura", "Fecha", "Vence", "Valor", "Saldo", "Vendedor", "Cupo"]
    m = ontologia.sugerir_mapeo(cols)
    assert m["cliente"] == "Nombre del tercero"
    assert m["fecha_emision"] == "Fecha" and m["fecha_vencimiento"] == "Vence"
    assert set(ontologia.cargar_plantilla().requeridos) <= set(m)


def test_mapeo_no_reutiliza_columnas():
    m = ontologia.sugerir_mapeo(["Fecha factura", "Fecha vencimiento", "Saldo pendiente"])
    assert m["fecha_emision"] == "Fecha factura"
    assert m["fecha_vencimiento"] == "Fecha vencimiento"
    assert len(set(m.values())) == len(m)


def test_csv_con_punto_y_coma_y_latin1():
    contenido = "NIT;Cliente;Saldo\n900123456;Panadería Ñoño;$ 1.000\n".encode("latin-1")
    df = ingesta.leer_archivo(contenido, "x.csv")
    assert list(df.columns) == ["NIT", "Cliente", "Saldo"]
    assert df.iloc[0]["Cliente"] == "Panadería Ñoño"


def test_excel(tmp_path):
    ruta = tmp_path / "c.xlsx"
    pd.DataFrame({"NIT": ["900123456"], "Saldo": ["1000"]}).to_excel(ruta, index=False)
    df = ingesta.leer_archivo(ruta.read_bytes(), "c.xlsx")
    assert df.iloc[0]["NIT"] == "900123456"
