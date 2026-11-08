"""Datos de prueba de una distribuidora inventada de Palmira y Cali (40 clientes) que activan todas las
decisiones de Ontara: dos cortes de cartera y dos reportes de ventas (31/10/2026 y 14/11/2026).
Nombres y NIT son inventados. Uso:  python datos_ejemplo/generar_prueba.py
"""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent / "prueba"
OUT.mkdir(parents=True, exist_ok=True)
C1, C2 = date(2026, 10, 31), date(2026, 11, 14)
VEND = ["Ana Gómez", "Luis Pérez", "Marta Ríos", "Jorge Cuero"]
M = 1_000_000

# (nombre, perfil, cupo en millones)
CLIENTES = [
    ("Supermercados La Cosecha", "ampliar_grande", 200), ("Distribuidora Cañaveral", "ampliar", 80),
    ("Panadería Pan del Valle", "ampliar", 30), ("Ferretería El Tornillo", "ampliar", 50),
    ("Droguería San Rafael", "ampliar", 40), ("Restaurante Sabor Caleño", "ampliar", 20),
    ("Minimercado El Paisa", "sobrecupo_al_dia", 15), ("Agrotienda La Palma", "sobrecupo_al_dia", 25),
    ("Constructora Río Cauca", "clave_moroso", 150), ("Hoteles Farallones", "clave_moroso", 120),
]
CLIENTES += [(n, "moroso_pequeno", c) for n, c in [
    ("Tienda Doña Inés", 10), ("Cafetería El Parque", 8), ("Papelería Tuluá", 12), ("Lácteos La Vaquita", 15),
    ("Frutería El Mango", 8), ("Peluquería Estilo", 6), ("Taller El Rin", 10), ("Granero San José", 12),
    ("Carnicería La Res", 10), ("Miscelánea Palmira", 8)]]
CLIENTES += [(n, "moroso_medio", c) for n, c in [
    ("Comercializadora Pacífico", 40), ("Avícola El Gallo", 35), ("Colegio Los Andes", 30),
    ("Clínica Veterinaria Huellas", 30), ("Mercados La 14 Sur", 45), ("Transportes Buga", 40),
    ("Industrias Plásticas Yumbo", 50), ("Distribuciones Jamundí", 35)]]
CLIENTES += [(n, "normal", c) for n, c in [
    ("Almacén El Ahorro", 30), ("Hotel Plaza Cali", 50), ("Licorera del Sur", 60), ("Empaques Candelaria", 40),
    ("Muebles El Roble", 25), ("Óptica Visión", 15), ("Gimnasio Fuerza", 10), ("Floristería Orquídea", 8),
    ("Repuestos Autovalle", 30), ("Pinturas Color Vivo", 20), ("Textiles Ginebra", 35), ("Café Tostado Dagua", 20)]]


def pesos(x):
    return "$ " + f"{x:,.0f}".replace(",", ".")


def fila(nit, nombre, num, emision, valor, saldo, i, cupo):
    return {"NIT": nit, "Nombre del tercero": nombre, "Factura": num, "Fecha": emision.strftime("%d/%m/%Y"),
            "Vence": (emision + timedelta(days=30)).strftime("%d/%m/%Y"), "Valor": pesos(valor),
            "Saldo": pesos(saldo), "Vendedor": VEND[i % 4], "Cupo": pesos(cupo)}


def facturas_cliente(i, nombre, perfil, cupo_m):
    """Historia de facturas del cliente: (numero, emision, valor, saldo_c1, saldo_c2)."""
    r = random.Random(100 + i)
    cupo = cupo_m * M
    fs = []
    n = 0

    def nueva(emision, valor, s1, s2):
        nonlocal n
        n += 1
        fs.append((f"FE-{i:02d}{n:03d}", emision, valor, s1, s2))

    if perfil in ("ampliar", "ampliar_grande"):
        # Paga al día, usa ~85-92 % del cupo, compra cada semana
        objetivo = cupo * r.uniform(0.85, 0.92)
        k = 4
        for j in range(k):
            nueva(C1 - timedelta(days=3 + 7 * j), objetivo / k, objetivo / k, 0 if j >= 2 else objetivo / k)
        for j in range(2):   # compras nuevas antes del corte 2
            nueva(C2 - timedelta(days=2 + 6 * j), objetivo / k, None, objetivo / k)
    elif perfil == "sobrecupo_al_dia":
        objetivo = cupo * 1.15
        for j in range(3):
            nueva(C1 - timedelta(days=4 + 8 * j), objetivo / 3, objetivo / 3, 0 if j == 2 else objetivo / 3)
        nueva(C2 - timedelta(days=5), objetivo / 3, None, objetivo / 3)
    elif perfil == "clave_moroso":
        # Gran comprador con facturas de 120+ días y que sigue comprando
        nueva(C1 - timedelta(days=150), 28 * M, 28 * M, 28 * M)
        nueva(C1 - timedelta(days=125), 18 * M, 18 * M, 9 * M)
        for j in range(5):
            nueva(C1 - timedelta(days=5 + 9 * j), 22 * M, 22 * M, 22 * M if j < 2 else 0)
        nueva(C2 - timedelta(days=4), 20 * M, None, 20 * M)
    elif perfil in ("moroso_pequeno", "moroso_medio"):
        escala = 1 if perfil == "moroso_pequeno" else 4
        paga = r.random() < 0.5            # la mitad se pone al día en el corte 2
        for j in range(r.randint(2, 4)):
            valor = r.randint(8, 30) * 100_000 * escala
            emision = C1 - timedelta(days=r.randint(45, 140))
            nueva(emision, valor, valor, 0 if paga and r.random() < 0.8 else valor)
        nueva(C1 - timedelta(days=r.randint(3, 20)), 1.5 * M * escala, 1.5 * M * escala, 1.5 * M * escala)
    else:  # normal: al día, uso bajo, algunas compras recientes
        for j in range(r.randint(2, 3)):
            valor = r.randint(10, 60) * 100_000
            nueva(C1 - timedelta(days=r.randint(2, 28)), valor, valor, 0 if r.random() < 0.6 else valor)
        valor = r.randint(10, 50) * 100_000
        nueva(C2 - timedelta(days=r.randint(1, 10)), valor, None, valor)
    return fs



def nit_de(i):
    rn = random.Random(7 + i)
    return f"9{rn.randint(10_000_000, 99_999_999)}-{rn.randint(0, 9)}"


def escribir(nombre, filas):
    with open(OUT / nombre, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0]), delimiter=";")
        w.writeheader()
        w.writerows(filas)
    print(nombre, len(filas), "filas")


def cartera():
    filas1, filas2 = [], []
    for i, (nombre, perfil, cupo_m) in enumerate(CLIENTES):
        nit = nit_de(i)
        for num, emision, valor, s1, s2 in facturas_cliente(i, nombre, perfil, cupo_m):
            valor = round(valor, -3)
            if s1:
                filas1.append(fila(nit, nombre, num, emision, valor, round(s1, -3), i, cupo_m * M))
            if s2:
                filas2.append(fila(nit, nombre, num, emision, valor, round(s2, -3), i, cupo_m * M))
    escribir("prueba_cartera_corte_2026-10-31.csv", filas1)
    escribir("prueba_cartera_corte_2026-11-14.csv", filas2)


# --- Ventas: 12 meses de facturas emitidas (pagadas o no) -------------------------------------
INICIO = C1 - timedelta(days=365)
# Clientes que se enfrían (dejan de comprar) o caen, y los que vuelven antes del segundo reporte.
ENFRIAN = {"Transportes Buga": 50, "Tienda Doña Inés": 55, "Frutería El Mango": 60, "Taller El Rin": 48,
           "Almacén El Ahorro": 45, "Muebles El Roble": 58, "Floristería Orquídea": 50, "Repuestos Autovalle": 52,
           "Café Tostado Dagua": 47, "Granero San José": 62, "Gimnasio Fuerza": 49, "Óptica Visión": 56}
CAEN = {"Hoteles Farallones": 0.45, "Pinturas Color Vivo": 0.4, "Textiles Ginebra": 0.5, "Colegio Los Andes": 0.5}
CRECEN = {"Supermercados La Cosecha": 1.6, "Distribuidora Cañaveral": 1.5, "Ferretería El Tornillo": 1.45,
          "Droguería San Rafael": 1.5}
VUELVEN = {"Tienda Doña Inés", "Taller El Rin", "Almacén El Ahorro", "Repuestos Autovalle", "Gimnasio Fuerza",
           "Muebles El Roble"}
MENSUAL = {"ampliar_grande": 120, "ampliar": 30, "sobrecupo_al_dia": 14, "clave_moroso": 70, "moroso_medio": 18,
           "moroso_pequeno": 4, "normal": 9}           # ventas mensuales normales, en millones


def ventas_cliente(i, nombre, perfil):
    r = random.Random(500 + i)
    ritmo = {"ampliar_grande": 7, "ampliar": 7, "clave_moroso": 10}.get(perfil, r.choice([10, 14, 21]))
    base = MENSUAL[perfil] * M * ritmo / 30.4
    corte_90 = C1 - timedelta(days=90)
    dia, n, filas = INICIO + timedelta(days=r.randint(0, ritmo)), 0, []
    while dia <= C2:
        factor = 1.0
        if dia > corte_90:
            factor = CRECEN.get(nombre, 1.0) * (1 - CAEN.get(nombre, 0.0))
        parado = nombre in ENFRIAN and C1 - timedelta(days=ENFRIAN[nombre]) < dia <= C1
        vuelve = dia > C1 and (nombre not in ENFRIAN or nombre in VUELVEN)
        if (dia <= C1 and not parado) or vuelve:
            n += 1
            valor = round(base * factor * r.uniform(0.8, 1.2), -3)
            filas.append((f"FV-{i:02d}{n:04d}", dia, valor))
        dia += timedelta(days=ritmo + r.randint(-2, 2))
    return filas


def ventas():
    v1, v2 = [], []
    for i, (nombre, perfil, _) in enumerate(CLIENTES):
        nit = nit_de(i)
        for num, dia, valor in ventas_cliente(i, nombre, perfil):
            fila_v = {"NIT": nit, "Cliente": nombre, "Factura": num, "Fecha": dia.strftime("%d/%m/%Y"),
                      "Total": pesos(valor), "Vendedor": VEND[i % 4]}
            if dia <= C1:
                v1.append(fila_v)
            if dia > C2 - timedelta(days=365):
                v2.append(fila_v)
    escribir("prueba_ventas_2026-10-31.csv", v1)
    escribir("prueba_ventas_2026-11-14.csv", v2)


if __name__ == "__main__":
    cartera()
    ventas()
