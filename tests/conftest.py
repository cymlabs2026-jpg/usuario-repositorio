import sys
from datetime import date
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from ontara import calidad, db, ingesta, ontologia  # noqa: E402

CORTE_1, CORTE_2 = date(2026, 10, 31), date(2026, 11, 7)


@pytest.fixture
def motor():
    return db.crear_motor("sqlite://")


def preparar(nombre: str):
    contenido = (RAIZ / "datos_ejemplo" / nombre).read_bytes()
    crudo = ingesta.leer_archivo(contenido, nombre)
    mapeo = ontologia.sugerir_mapeo(list(crudo.columns))
    datos = ingesta.convertir(crudo, mapeo)
    informe, validas = calidad.evaluar(datos, mapeo)
    return informe, validas, mapeo


@pytest.fixture
def corte1():
    return preparar("cartera_corte_2026-10-31.csv")


@pytest.fixture
def corte2():
    return preparar("cartera_corte_2026-11-07.csv")
