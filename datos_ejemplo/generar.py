"""Genera dos cortes de cartera ficticios con formato parecido a un exporte de ERP colombiano.

Todos los nombres y NIT son inventados. Uso:
    python datos_ejemplo/generar.py
"""
from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

DIR = Path(__file__).resolve().parent
NOMBRES = [
    "Distribuidora El Samán", "Agroinsumos La Ceiba", "Ferretería Los Guaduales", "Supermercado La Esquina",
    "Tienda Naturista Bienestar", "Panificadora San Isidro", "Droguería Vida Plena", "Comercializadora Andina",
    "Restaurante Fogón Vallecaucano", "Minimercado Doña Rosa", "Hotel Mirador del Río", "Cafetería Central",
    "Lácteos El Recreo", "Avícola Santa Elena", "Constructora Palmeras", "Papelería Escolar Arcoíris",
    "Frutas y Verduras El Huerto", "Carnes Selectas Buga", "Distribuciones Pacífico", "Granero La Abundancia",
    "Colegio Nuevo Horizonte", "Clínica Veterinaria Patitas", "Taller Mecánico El Pistón",
    "Almacén Agropecuario Tuluá", "Mercados Express Palmira",
]
VENDEDORES = ["Ana Gómez", "Luis Pérez", "Marta Ríos"]


def pesos(x: float) -> str:
    return "$ " + f"{x:,.0f}".replace(",", ".")


def generar(corte: date, semilla: int, pagos: set[int]) -> list[dict]:
    rnd = random.Random(semilla)
    filas = []
    for i, nombre in enumerate(NOMBRES):
        rbase = random.Random(1000 + i)             # perfil estable del cliente entre cortes
        nit = f"9{rbase.randint(10_000_000, 99_999_999)}-{rbase.randint(0, 9)}"
        cupo = rbase.choice([20, 30, 50, 80, 120]) * 1_000_000
        perfil = rbase.random()                      # < 0.3 = mal pagador
        for j in range(rbase.randint(2, 7)):
            antig = rbase.randint(5, 200 if perfil < 0.3 else 75)
            emision = date(2026, 10, 31) - timedelta(days=antig)
            vence = emision + timedelta(days=30)
            valor = rbase.randint(8, 90) * 100_000
            saldo = valor if rbase.random() < 0.8 else valor * 0.5
            if i in pagos and vence < corte:
                saldo = 0 if rnd.random() < 0.8 else saldo * 0.3
            if saldo > 0:
                filas.append({"NIT": nit, "Nombre del tercero": nombre, "Factura": f"FV-{1000 + i * 20 + j}",
                              "Fecha": emision.strftime("%d/%m/%Y"), "Vence": vence.strftime("%d/%m/%Y"),
                              "Valor": pesos(valor), "Saldo": pesos(saldo), "Vendedor": VENDEDORES[i % 3],
                              "Cupo": pesos(cupo)})
        if corte > date(2026, 10, 31) and perfil < 0.5:   # compras nuevas de la semana
            emision = corte - timedelta(days=rnd.randint(0, 6))
            valor = rnd.randint(5, 40) * 100_000
            filas.append({"NIT": nit, "Nombre del tercero": nombre, "Factura": f"FV-{5000 + i}",
                          "Fecha": emision.strftime("%d/%m/%Y"),
                          "Vence": (emision + timedelta(days=30)).strftime("%d/%m/%Y"),
                          "Valor": pesos(valor), "Saldo": pesos(valor), "Vendedor": VENDEDORES[i % 3],
                          "Cupo": pesos(cupo)})
    return filas


def escribir(nombre: str, filas: list[dict]) -> None:
    with open(DIR / nombre, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0]), delimiter=";")
        w.writeheader()
        w.writerows(filas)


if __name__ == "__main__":
    escribir("cartera_corte_2026-10-31.csv", generar(date(2026, 10, 31), 1, set()))
    escribir("cartera_corte_2026-11-07.csv", generar(date(2026, 11, 7), 2, {0, 2, 3, 5, 8, 13}))
    print("Listo: dos cortes en", DIR)
