"""Datos de prueba de una distribuidora inventada de Palmira y Cali (40 clientes) que activan todas las
decisiones de Ontara: dos cortes de cartera, dos reportes de ventas, dos de ventas por producto, dos de cuentas
por pagar, dos de existencias de inventario y dos de pedidos (31/10/2026 y 14/11/2026), más un reporte de
recaudos con los pagos
recibidos entre los dos cortes. Cada archivo lleva el nombre del paquete que lo usa en la aplicación.
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
            if s2 and emision <= C1:
                filas2.append(fila(nit, nombre, num, emision, valor, round(s2, -3), i, cupo_m * M))
        # Lo facturado entre los dos cortes sale del reporte de ventas, para que cartera y ventas cuadren.
        for num, dia, valor in ventas_cliente(i, nombre, perfil):
            if C1 < dia <= C2:
                filas2.append(fila(nit, nombre, num, dia, valor, valor, i, cupo_m * M))
    escribir("credito_y_cobro_cartera_2026-10-31.csv", filas1)
    escribir("credito_y_cobro_cartera_2026-11-14.csv", filas2)


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
    escribir("foco_comercial_ventas_2026-10-31.csv", v1)
    escribir("foco_comercial_ventas_2026-11-14.csv", v2)


# --- Ventas por producto: cada factura del reporte de ventas, partida en líneas con costo -----------
# producto -> margen bruto a precio de lista
PRODUCTOS = {
    "Arroz blanco 25 kg": 0.28, "Aceite de girasol 3 L": 0.26, "Azúcar refinada 50 kg": 0.11,
    "Harina de trigo 50 kg": 0.27, "Panela 24 und": 0.34, "Café molido 500 g": 0.38, "Fríjol cargamanto 25 kg": 0.31,
    "Lentejas 25 kg": 0.30, "Atún en lata x 48": 0.29, "Leche en polvo 25 kg": 0.28, "Pasta spaghetti x 24": 0.33,
    "Sal refinada 25 kg": 0.35, "Detergente 10 kg": 0.32, "Jabón de barra x 50": 0.36, "Papel higiénico x 48": 0.30,
    "Gaseosa 1,5 L x 12": 0.27, "Agua 600 ml x 24": 0.37, "Galletas surtidas x 24": 0.34,
}
# Descuento promedio por cliente (el resto recibe entre 2 % y 5 %)
DESCUENTO = {"Supermercados La Cosecha": 0.14, "Hoteles Farallones": 0.11, "Mercados La 14 Sur": 0.12,
             "Distribuidora Cañaveral": 0.10, "Constructora Río Cauca": 0.10, "Industrias Plásticas Yumbo": 0.09}
SUBE_ACEITE = date(2026, 8, 1)          # desde aquí el costo del aceite subió y se vende bajo el costo
CORRIGE = date(2026, 11, 1)             # desde aquí La Cosecha baja su descuento y el aceite sube de precio


def margen_producto(producto, dia):
    if producto == "Aceite de girasol 3 L" and SUBE_ACEITE <= dia < CORRIGE:
        return -0.06
    return PRODUCTOS[producto]


def lineas():
    l1, l2 = [], []
    nombres_prod = list(PRODUCTOS)
    for i, (nombre, perfil, _) in enumerate(CLIENTES):
        nit, r = nit_de(i), random.Random(900 + i)
        favoritos = r.sample(nombres_prod, 6)
        if perfil in ("ampliar_grande", "clave_moroso"):
            favoritos[0] = "Aceite de girasol 3 L"
        if perfil == "ampliar" and "Azúcar refinada 50 kg" not in favoritos:
            favoritos[0] = "Azúcar refinada 50 kg"
        for num, dia, total in ventas_cliente(i, nombre, perfil):
            if dia <= C1 - timedelta(days=184):
                continue
            desc = DESCUENTO.get(nombre, r.uniform(0.02, 0.05))
            if nombre == "Supermercados La Cosecha" and dia >= CORRIGE:
                desc = 0.07
            partes = r.randint(2, 4)
            pesos_l = [r.random() + 0.3 for _ in range(partes)]
            for k, prod in enumerate(r.sample(favoritos, partes)):
                valor = round(total * pesos_l[k] / sum(pesos_l), -2)
                lista = valor / (1 - desc)
                costo = round(lista * (1 - margen_producto(prod, dia)), -2)
                fila_l = {"NIT": nit, "Cliente": nombre, "Factura": num, "Fecha": dia.strftime("%d/%m/%Y"),
                          "Producto": prod, "Cantidad": max(1, round(valor / 180_000)),
                          "Descuento": pesos(round(lista - valor, -2)), "Venta neta": pesos(valor),
                          "Costo": pesos(costo), "Vendedor": VEND[i % 4]}
                if dia <= C1:
                    l1.append(fila_l)
                if dia > C2 - timedelta(days=184):
                    l2.append(fila_l)
    escribir("margen_ventas_por_producto_2026-10-31.csv", l1)
    escribir("margen_ventas_por_producto_2026-11-14.csv", l2)


# --- Recaudos: pagos recibidos entre los dos cortes, cruzados con la factura que pagan ---------------
def recaudos():
    filas = []
    for i, (nombre, perfil, cupo_m) in enumerate(CLIENTES):
        nit, r = nit_de(i), random.Random(1300 + i)
        for num, emision, valor, s1, s2 in facturas_cliente(i, nombre, perfil, cupo_m):
            if not s1:
                continue
            pagado = round(s1, -3) - round(s2 or 0, -3)
            if pagado <= 0:
                continue
            dia = C1 + timedelta(days=r.randint(1, (C2 - C1).days))
            filas.append({"NIT": nit, "Cliente": nombre, "Recibo de caja": f"RC-{len(filas) + 1:05d}",
                          "Fecha de pago": dia.strftime("%d/%m/%Y"), "Documento cruce": num,
                          "Valor pagado": pesos(pagado)})
    filas.sort(key=lambda f: (f["Fecha de pago"][6:], f["Fecha de pago"][3:5], f["Fecha de pago"][:2]))
    escribir("aprendizaje_recaudos_2026-11-14.csv", filas)


# --- Cuentas por pagar: proveedores y obligaciones programadas ------------------------------------
# (proveedor, compras mensuales en millones, plazo en días)
PROVEEDORES = [
    ("Ingenio Providencia Comercial", 120, 30), ("Aceites del Pacífico", 95, 30), ("Molinos Harineros del Valle", 85, 45),
    ("Lácteos Colanta Distribución", 60, 30), ("Granos y Cereales Andinos", 55, 45), ("Empaques Plásticos Yumbo", 25, 60),
    ("Café Tostadores del Cauca", 40, 30), ("Arrocera La Esperanza", 70, 30), ("Distribuidora de Sal Marina", 18, 30),
    ("Transportes Logísticos Cali", 22, 15), ("Enlatados del Sur", 35, 45), ("Bebidas Andinas", 30, 30),
]
APLAZA = {"Ingenio Providencia Comercial"}    # acepta correr 30 días su compra de temporada (segundo reporte)
# Compras de temporada para diciembre: son las que rompen la caja.
TEMPORADA = {"Aceites del Pacífico": (date(2026, 10, 15), date(2026, 12, 15), 150 * M),
             "Ingenio Providencia Comercial": (date(2026, 10, 22), date(2026, 12, 22), 100 * M)}
SALDO_BANCOS = {C1: 250 * M, C2: 390 * M}   # se digitan al cargar cada reporte
OBLIGACIONES = []                        # (nit, nombre, documento, vence, valor, categoría)
for mes, anio in ((11, 2026), (12, 2026), (1, 2027), (2, 2027)):
    OBLIGACIONES += [
        ("860000001", "Nómina y prestaciones", f"NOM-{anio}{mes:02d}-1", date(anio, mes, 15), 40 * M, "Nómina"),
        ("860000001", "Nómina y prestaciones", f"NOM-{anio}{mes:02d}-2", date(anio, mes, 28 if mes == 2 else 30), 40 * M,
         "Nómina"),
        ("800197268", "DIAN", f"RTE-{anio}{mes:02d}", date(anio, mes, 20), 10 * M, "Impuestos"),
        ("890903938", "Bancolombia cuota crédito", f"CR-{anio}{mes:02d}", date(anio, mes, 10), 20 * M, "Crédito"),
        ("900555444", "Inmobiliaria Bodega Palmira", f"ARR-{anio}{mes:02d}", date(anio, mes, 5), 12 * M, "Arriendo"),
    ]
OBLIGACIONES += [("860000001", "Nómina y prestaciones", "PRIMA-2026-12", date(2026, 12, 18), 75 * M, "Nómina"),
                 ("800197268", "DIAN", "IVA-2026-06", date(2026, 11, 19), 60 * M, "Impuestos"),
                 ("800197268", "DIAN", "IVA-2027-01", date(2027, 1, 21), 65 * M, "Impuestos")]


def cuentas_proveedor(i, nombre, mensual, plazo, corte):
    """Facturas abiertas al corte: compras de cada semana que aún no vencen o que vencen pronto."""
    r = random.Random(1300 + i)
    filas, dia, n = [], C1 - timedelta(days=75), 0
    while dia <= corte:
        n += 1
        valor = round(mensual * M / 4.33 * r.uniform(0.8, 1.2), -3)
        vence = dia + timedelta(days=plazo)
        if vence > corte - (timedelta(days=3) if r.random() < 0.1 else timedelta(0)):
            filas.append((f"FP-{i:02d}{n:03d}", dia, vence, valor))
        dia += timedelta(days=7)
    return filas


def pagos():
    for corte, archivo in ((C1, "caja_13_semanas_cuentas_por_pagar_2026-10-31.csv"), (C2, "caja_13_semanas_cuentas_por_pagar_2026-11-14.csv")):
        filas = []
        for i, (nombre, mensual, plazo) in enumerate(PROVEEDORES):
            nit = f"8{random.Random(70 + i).randint(10_000_000, 99_999_999)}"
            for num, emision, vence, valor in cuentas_proveedor(i, nombre, mensual, plazo, corte):
                filas.append({"NIT proveedor": nit, "Proveedor": nombre, "Documento": num,
                              "Fecha": emision.strftime("%d/%m/%Y"), "Vence": vence.strftime("%d/%m/%Y"),
                              "Saldo": pesos(valor), "Concepto": "Compras"})
            if nombre in TEMPORADA:
                emision, vence, valor = TEMPORADA[nombre]
                if corte == C2 and nombre in APLAZA:
                    vence += timedelta(days=30)                        # negociado tras la alerta de caja
                filas.append({"NIT proveedor": nit, "Proveedor": nombre, "Documento": f"FP-{i:02d}TEMP",
                              "Fecha": emision.strftime("%d/%m/%Y"), "Vence": vence.strftime("%d/%m/%Y"),
                              "Saldo": pesos(valor), "Concepto": "Compras de temporada"})
        for nit, nombre, doc, vence, valor, cat in OBLIGACIONES:
            if vence > corte:
                filas.append({"NIT proveedor": nit, "Proveedor": nombre, "Documento": doc, "Fecha": "",
                              "Vence": vence.strftime("%d/%m/%Y"), "Saldo": pesos(valor), "Concepto": cat})
        escribir(archivo, filas)



# --- Inventario: existencias por producto, calculadas con la demanda real del reporte de ventas por producto ------
# días de cobertura de cada producto al 31/10 (los demás quedan entre 30 y 60)
COBERTURA = {"Arroz blanco 25 kg": 6, "Leche en polvo 25 kg": 3, "Panela 24 und": 10, "Aceite de girasol 3 L": 8,
             "Café molido 500 g": 150, "Detergente 10 kg": 210, "Galletas surtidas x 24": 125}
# productos que ya no se venden: (cantidad, costo unitario)
SIN_MOVIMIENTO = {"Harina de maíz 25 kg": (90, 98_000), "Vinagre 3 L x 6": (160, 26_000),
                  "Gelatina caja x 24": (110, 54_000)}
BODEGAS = ["Palmira", "Cali"]


def _demanda(archivo, corte):
    """Unidades por día y costo unitario de cada producto en los 90 días antes del corte."""
    with open(OUT / archivo, encoding="utf-8") as f:
        filas = list(csv.DictReader(f, delimiter=";"))
    uds, costo = {}, {}
    for r in filas:
        d, m, a = r["Fecha"].split("/")
        if date(int(a), int(m), int(d)) <= corte - timedelta(days=90):
            continue
        uds[r["Producto"]] = uds.get(r["Producto"], 0) + int(r["Cantidad"])
        costo[r["Producto"]] = costo.get(r["Producto"], 0) + int(r["Costo"].replace("$", "").replace(".", "").strip())
    return {k: (uds[k] / 90, costo[k] / uds[k]) for k in uds}


def inventario():
    dem = _demanda("margen_ventas_por_producto_2026-10-31.csv", C1)
    cobertura2 = {}
    for corte, archivo in ((C1, "inventario_existencias_2026-10-31.csv"), (C2, "inventario_existencias_2026-11-14.csv")):
        filas = []
        for k, prod in enumerate(sorted(dem)):
            por_dia, costo_u = dem[prod]
            dias = COBERTURA.get(prod, 30 + (k * 7) % 31)
            if corte == C2:
                # Se repuso el arroz y la leche; la panela se agotó; el café bajó con promoción; lo demás rota.
                dias = {"Arroz blanco 25 kg": 38, "Leche en polvo 25 kg": 41, "Panela 24 und": 0,
                        "Café molido 500 g": 70}.get(prod, max(dias - 14, 25) if dias < 100 else dias - 10)
            cantidad = round(por_dia * dias)
            for b, parte in zip(BODEGAS, (0.6, 0.4)):
                filas.append({"Referencia": prod, "Bodega": b, "Existencia": round(cantidad * parte),
                              "Costo promedio": pesos(round(costo_u, -2))})
        for prod, (cantidad, costo_u) in SIN_MOVIMIENTO.items():
            if corte == C2 and prod == "Harina de maíz 25 kg":
                cantidad = 0                                       # se liquidó tras la alerta
            filas.append({"Referencia": prod, "Bodega": "Palmira", "Existencia": cantidad,
                          "Costo promedio": pesos(costo_u)})
        escribir(archivo, filas)


# --- Despacho: pedidos con fecha prometida y de despacho (cada factura de venta salió de un pedido) ---------
# Clientes a los que se les despacha tarde la mitad de los pedidos: (días de atraso mínimos, máximos)
INCUMPLIDOS = {"Licorera del Sur": (2, 5), "Hotel Plaza Cali": (2, 4), "Textiles Ginebra": (3, 6)}
CORRIGE_ENTREGAS = {"Licorera del Sur", "Textiles Ginebra"}   # después del 31/10 sus pedidos salen a tiempo
LEJOS = {"Transportes Buga": "Buga", "Papelería Tuluá": "Tuluá", "Distribuciones Jamundí": "Jamundí",
         "Industrias Plásticas Yumbo": "Yumbo", "Textiles Ginebra": "Ginebra", "Café Tostado Dagua": "Dagua"}
# Pedidos abiertos al 31/10: cliente -> (pedido, fecha pedido, prometida, despacho real o None, [(producto, und)])
ABIERTOS = {
    "Supermercados La Cosecha": ("PD-9001", date(2026, 10, 30), date(2026, 11, 1), date(2026, 11, 1),
                                 [("Arroz blanco 25 kg", 60), ("Azúcar refinada 50 kg", 40), ("Atún en lata x 48", 30)]),
    "Distribuidora Cañaveral": ("PD-9002", date(2026, 10, 28), date(2026, 10, 30), date(2026, 11, 3),
                                [("Azúcar refinada 50 kg", 30), ("Fríjol cargamanto 25 kg", 20)]),
    "Constructora Río Cauca": ("PD-9003", date(2026, 10, 24), date(2026, 10, 27), None,
                               [("Agua 600 ml x 24", 70), ("Gaseosa 1,5 L x 12", 60)]),
    "Avícola El Gallo": ("PD-9004", date(2026, 10, 26), date(2026, 10, 28), date(2026, 11, 6),
                         [("Sal refinada 25 kg", 25), ("Jabón de barra x 50", 10)]),
    "Hotel Plaza Cali": ("PD-9005", date(2026, 10, 28), date(2026, 10, 30), date(2026, 11, 5),
                         [("Leche en polvo 25 kg", 30), ("Café molido 500 g", 10)]),
    "Licorera del Sur": ("PD-9006", date(2026, 10, 27), date(2026, 10, 29), date(2026, 11, 1),
                         [("Gaseosa 1,5 L x 12", 18), ("Agua 600 ml x 24", 12)]),
    "Empaques Candelaria": ("PD-9007", date(2026, 10, 29), date(2026, 10, 31), date(2026, 11, 1),
                            [("Papel higiénico x 48", 12), ("Jabón de barra x 50", 8)]),
    "Mercados La 14 Sur": ("PD-9008", date(2026, 10, 30), date(2026, 11, 3), date(2026, 11, 3),
                           [("Leche en polvo 25 kg", 12), ("Pasta spaghetti x 24", 25)]),
}
# Pedidos nuevos que quedan abiertos al 14/11 (aún no vencen su fecha prometida)
ABIERTOS_C2 = {"Droguería San Rafael": ("PD-9101", date(2026, 11, 13), date(2026, 11, 17),
                                        [("Jabón de barra x 50", 20), ("Papel higiénico x 48", 15)]),
               "Ferretería El Tornillo": ("PD-9102", date(2026, 11, 14), date(2026, 11, 18),
                                          [("Detergente 10 kg", 25)])}
PRECIO_UND = 180_000


def pedidos():
    nombres_prod = list(PRODUCTOS)
    reportes = {C1: [], C2: []}
    for i, (nombre, perfil, _) in enumerate(CLIENTES):
        nit, r = nit_de(i), random.Random(1300 + i)
        ciudad = LEJOS.get(nombre, "Palmira" if i % 3 == 0 else "Cali")
        plazo = 3 if nombre in LEJOS else 2
        favoritos = r.sample(nombres_prod, 5)

        def lineas_pedido(num, pedido_dia, prometida, despacho, productos):
            return [{"NIT": nit, "Cliente": nombre, "Pedido": num, "Fecha pedido": pedido_dia.strftime("%d/%m/%Y"),
                     "Fecha entrega prometida": prometida.strftime("%d/%m/%Y"),
                     "Fecha despacho": despacho.strftime("%d/%m/%Y") if despacho else "", "Producto": prod,
                     "Cantidad": und, "Valor": pesos(und * PRECIO_UND), "Ciudad": ciudad, "Vendedor": VEND[i % 4]}
                    for prod, und in productos]

        k = 0
        for num, dia, total in ventas_cliente(i, nombre, perfil):
            atraso = 0
            if nombre in INCUMPLIDOS and (dia <= C1 or nombre not in CORRIGE_ENTREGAS):
                k += 1
                atraso = r.randint(*INCUMPLIDOS[nombre]) if k % 2 == 0 or k % 5 == 0 else 0
            pedido_dia = dia - timedelta(days=plazo + atraso)
            prometida = pedido_dia + timedelta(days=plazo)
            partes = r.randint(1, 3)
            pesos_l = [r.random() + 0.3 for _ in range(partes)]
            productos = [(prod, max(1, round(total * pesos_l[j] / sum(pesos_l) / PRECIO_UND)))
                         for j, prod in enumerate(r.sample(favoritos, partes))]
            for corte in (C1, C2):
                if corte - timedelta(days=100) <= pedido_dia and dia <= corte:
                    reportes[corte] += lineas_pedido(num.replace("FV", "PD"), pedido_dia, prometida, dia, productos)
                elif pedido_dia <= corte < dia:          # pedido que al corte todavía no ha salido
                    reportes[corte] += lineas_pedido(num.replace("FV", "PD"), pedido_dia, prometida, None, productos)
        if nombre in ABIERTOS:
            num, pedido_dia, prometida, despacho, productos = ABIERTOS[nombre]
            reportes[C1] += lineas_pedido(num, pedido_dia, prometida, None, productos)
            reportes[C2] += lineas_pedido(num, pedido_dia, prometida, despacho, productos)
        if nombre in ABIERTOS_C2:
            num, pedido_dia, prometida, productos = ABIERTOS_C2[nombre]
            reportes[C2] += lineas_pedido(num, pedido_dia, prometida, None, productos)
    escribir("despacho_pedidos_2026-10-31.csv", reportes[C1])
    escribir("despacho_pedidos_2026-11-14.csv", reportes[C2])


if __name__ == "__main__":
    cartera()
    ventas()
    lineas()
    recaudos()
    pagos()
    inventario()
    pedidos()
