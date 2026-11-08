import pandas as pd

from ontara import calidad, ingesta, ontologia

COLS = ["NIT", "Cliente", "Factura", "Fecha", "Vence", "Saldo", "Valor"]


def evaluar(filas):
    crudo = pd.DataFrame(filas, columns=COLS)
    mapeo = ontologia.sugerir_mapeo(COLS)
    return calidad.evaluar(ingesta.convertir(crudo, mapeo), mapeo)


def niveles(informe):
    return {v.nombre: (v.nivel, v.filas_afectadas) for v in informe.validaciones}


def test_archivo_limpio_es_verde():
    inf, val = evaluar([["900123456", "A", "F1", "01/09/2026", "01/10/2026", "100", "100"]])
    assert inf.semaforo == "verde" and len(val) == 1


def test_filas_malas_se_excluyen_y_se_reportan_con_numero_de_fila():
    inf, val = evaluar([
        ["900123456", "A", "F1", "01/09/2026", "01/10/2026", "100", "100"],
        ["12", "B", "F2", "01/09/2026", "01/10/2026", "100", "100"],          # NIT inválido
        ["900123456", "A", "F1", "01/09/2026", "01/10/2026", "100", "100"],   # duplicada
        ["800111222", "C", "F3", "sin fecha", "01/10/2026", "100", "100"],    # fecha ilegible
    ])
    n = niveles(inf)
    assert n["NIT válido"] == ("rojo", 1)
    assert n["Facturas únicas"][1] == 1 and n["Fechas legibles"][1] == 1
    assert inf.semaforo == "rojo" and len(val) == 1
    assert inf.validaciones[0].ejemplos == [3]


def test_advertencias_no_excluyen_filas():
    inf, val = evaluar([
        ["900123456", "A", "F1", "01/10/2026", "01/09/2026", "200", "100"],   # vence antes y saldo > valor
        ["900123456", "A S.A.S.", "F2", "01/09/2026", "01/10/2026", "100", "100"],
    ])
    n = niveles(inf)
    assert n["Vencimiento coherente"][0] == "amarillo"
    assert n["Saldo ≤ valor"][0] == "amarillo"
    assert n["Un nombre por NIT"][1] == 2
    assert len(val) == 2 and inf.puede_cargar


def test_faltan_columnas_obligatorias_bloquea():
    crudo = pd.DataFrame([["900123456", "100"]], columns=["NIT", "Saldo"])
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns))
    inf, _ = calidad.evaluar(ingesta.convertir(crudo, mapeo), mapeo)
    assert not inf.puede_cargar and "fecha_vencimiento" in inf.faltan_campos
