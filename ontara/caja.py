"""Paquete de decisión Caja a 13 semanas.

Pregunta que responde: ¿en qué semana se queda corta la caja y qué mover para que no pase?
Proyecta semana a semana el saldo en bancos con:
  - el saldo en bancos al corte (se digita al cargar el reporte de cuentas por pagar),
  - los cobros de la cartera abierta, en la fecha en que cada cliente suele pagar de verdad,
  - los cobros de las ventas nuevas (ritmo del último trimestre, con un escenario de crecimiento),
  - los pagos del reporte de cuentas por pagar, las compras nuevas y los gastos fijos.

Todo es determinístico: mismos datos + misma versión de reglas = mismas decisiones.
"""
from __future__ import annotations

import math
import unicodedata
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import delete, func, insert, select
from sqlalchemy.engine import Engine

from . import impacto, reglas
from .db import (acciones, cargas, clientes, cuentas_por_pagar, decisiones, proyecciones_caja,
                 resultados_caja, saldos_caja)

PAQUETE = "caja"
TIPOS = {"caja_bajo_minimo": "Caja bajo el mínimo", "aplazar_pago_proveedor": "Aplazar pago a proveedor",
         "excedente_de_caja": "Excedente de caja"}
ABIERTAS = ("pendiente", "aprobada", "ajustada", "ejecutada")
SEMANAS = 13
COLUMNAS = ["semana", "inicio", "fin", "cobros_cartera", "cobros_ventas_nuevas", "pagos_proveedores",
            "compras_nuevas", "gastos_fijos", "saldo_final"]


def _pesos(x: float) -> str:
    return ("-$" if x < 0 else "$") + f"{abs(x):,.0f}".replace(",", ".")


def _f(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _norm(texto) -> str:
    s = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
    return s.strip()


def semana_de(fecha: date, corte: date) -> int:
    """Semana 1 = los 7 días después del corte. Lo que ya venció cae en la semana 1."""
    return max(1, math.ceil((fecha - corte).days / 7))


# --- Datos --------------------------------------------------------------------------
def cuentas_de_carga(motor: Engine, carga_id: int) -> pd.DataFrame:
    with motor.connect() as cx:
        filas = cx.execute(select(cuentas_por_pagar).where(cuentas_por_pagar.c.carga_id == carga_id)).mappings().all()
    cols = ["nit", "proveedor", "numero", "fecha_emision", "fecha_vencimiento", "saldo", "categoria"]
    return pd.DataFrame([dict(f) for f in filas], columns=["id", "carga_id", *cols])[cols]


def saldo_bancos(motor: Engine, carga_id: int) -> float | None:
    with motor.connect() as cx:
        return cx.execute(select(saldos_caja.c.saldo_bancos).where(saldos_caja.c.carga_id == carga_id)
                          .order_by(saldos_caja.c.id.desc()).limit(1)).scalar()


def _cartera_al_corte(motor: Engine, corte: date) -> tuple[int | None, date | None]:
    with motor.connect() as cx:
        fila = cx.execute(select(cargas.c.id, cargas.c.fecha_corte).where(
            cargas.c.tipo == "cartera", cargas.c.fecha_corte <= corte)
            .order_by(cargas.c.fecha_corte.desc(), cargas.c.id.desc()).limit(1)).first()
    return (fila[0], fila[1]) if fila else (None, None)


def _recaudos(motor: Engine) -> pd.DataFrame | None:
    """Fechas reales de pago por factura, si el cliente carga su reporte de recaudos."""
    try:
        from . import aprendizaje
        df = aprendizaje.recaudos_por_factura(motor)
    except (ImportError, AttributeError):
        return None
    return df if df is not None and not df.empty else None


def atrasos_por_cliente(motor: Engine, corte: date) -> dict[str, float]:
    """Días que cada cliente suele pagar después del vencimiento.

    1. Con el reporte de recaudos: fecha real de pago menos vencimiento.
    2. Si no, se infiere de los cortes de cartera: una factura que estaba en un corte y no en el
       siguiente se pagó entre los dos (se toma el punto medio)."""
    hist = impacto.facturas_historicas(motor, corte)
    if hist.empty:
        return {}
    venc = hist.drop_duplicates(["nit", "numero"], keep="last").set_index(["nit", "numero"])["fecha_vencimiento"]
    obs: list[tuple[str, float, float]] = []            # (nit, días de atraso, peso)
    rec = _recaudos(motor)
    if rec is not None:
        for r in rec.itertuples(index=False):
            v = venc.get((r.nit, str(r.factura)))
            if v is not None and r.fecha_pago <= corte:
                obs.append((r.nit, (r.fecha_pago - v).days, float(r.valor or 1)))
    if not obs:
        with motor.connect() as cx:
            cortes = cx.execute(select(cargas.c.id, cargas.c.fecha_corte).where(
                cargas.c.tipo == "cartera", cargas.c.fecha_corte <= corte)
                .order_by(cargas.c.fecha_corte, cargas.c.id)).all()
        for (id_a, f_a), (id_b, f_b) in zip(cortes, cortes[1:]):
            a = hist[(hist["carga_id"] == id_a) & (hist["saldo"] > 0)]
            b = set(zip(hist.loc[hist["carga_id"] == id_b, "nit"], hist.loc[hist["carga_id"] == id_b, "numero"]))
            medio = f_a + (f_b - f_a) / 2
            for r in a.itertuples(index=False):
                if (r.nit, r.numero) not in b:
                    obs.append((r.nit, (medio - r.fecha_vencimiento).days, float(r.saldo)))
    if not obs:
        return {}
    df = pd.DataFrame(obs, columns=["nit", "dias", "peso"])
    df["dias"] = df["dias"].clip(lower=0)
    return {n: round(float((g["dias"] * g["peso"]).sum() / g["peso"].sum()), 1) for n, g in df.groupby("nit")}


def ventas_semanales(motor: Engine, corte: date, fact: pd.DataFrame) -> float:
    """Ritmo de ventas del último trimestre: con el reporte de ventas si existe; si no, con la cartera."""
    from . import comercial
    v = comercial.ventas_historicas(motor, corte)
    if not v.empty:
        reciente = v[v["fecha"] > corte - timedelta(days=84)]
        return float(reciente["valor"].sum()) / 12
    return impacto.ventas_diarias_estimadas(fact, corte, 30) * 7


def _compras_pct(motor: Engine, corte: date, p: dict) -> float:
    if float(p["compras_pct_ventas"] or 0) > 0:
        return float(p["compras_pct_ventas"])
    from . import margen
    lineas = margen.lineas_historicas(motor, corte)
    m = margen.margen_pct(lineas[lineas["fecha"] > corte - timedelta(days=90)]) if not lineas.empty else None
    return round(100 - m, 1) if m is not None else 75.0


# --- Proyección ------------------------------------------------------------------------
def cobros_esperados(fact: pd.DataFrame, corte: date, atrasos: dict[str, float], p: dict) -> pd.DataFrame:
    """Fecha y monto esperados de cobro de cada factura abierta."""
    filas = []
    for r in (fact[fact["saldo"] > 0].itertuples(index=False) if len(fact) else []):
        mora = (corte - r.fecha_vencimiento).days
        atraso = atrasos.get(r.nit, float(p["atraso_por_defecto_dias"]))
        if mora > p["dias_incobrable"]:
            filas.append((r.nit, r.numero, r.fecha_vencimiento, mora, float(r.saldo), None, 0.0, "incobrable"))
            continue
        esperado = r.fecha_vencimiento + timedelta(days=round(atraso))
        monto, estado = float(r.saldo), "normal"
        if esperado <= corte:              # ya pasó la fecha en que suele pagar
            if mora > p["dias_mora_alta"]:
                esperado, monto, estado = corte + timedelta(days=38), float(r.saldo) * p["pct_recaudo_mora_alta"] / 100, \
                    "mora_alta"
            else:                          # entre 1 y 4 semanas: la mitad de lo que ya lleva de mora
                esperado, estado = corte + timedelta(days=min(max(mora // 2, 7), 28)), "vencida"
        filas.append((r.nit, r.numero, r.fecha_vencimiento, mora, float(r.saldo), esperado, monto, estado))
    df = pd.DataFrame(filas, columns=["nit", "numero", "vencimiento", "mora", "saldo", "esperado", "monto", "estado"])
    df["semana"] = [semana_de(e, corte) if e else None for e in df["esperado"]]
    return df


def proyectar(corte: date, saldo_inicial: float, cobros: pd.DataFrame, cxp: pd.DataFrame, venta_semanal: float,
              plazo_clientes: float, atraso_medio: float, plazo_prov: float, compras_pct: float,
              p: dict) -> pd.DataFrame:
    """Saldo en bancos al final de cada una de las 13 semanas."""
    semanas = range(1, SEMANAS + 1)
    cob = cobros.dropna(subset=["semana"]).groupby("semana")["monto"].sum()
    pag = cxp.assign(semana=[semana_de(v, corte) for v in cxp["fecha_vencimiento"]]).groupby("semana")["saldo"].sum() \
        if not cxp.empty else pd.Series(dtype=float)
    venta = venta_semanal * (1 + float(p["crecimiento_ventas_pct"]) / 100)
    gasto = float(p["gastos_fijos_mensuales"]) * 12 / 52
    saldo, filas = float(saldo_inicial), []
    for s in semanas:
        cv = venta * _fraccion_llegada(s, plazo_clientes + atraso_medio)
        cn = venta * compras_pct / 100 * _fraccion_llegada(s, plazo_prov)
        fila = {"semana": s, "inicio": corte + timedelta(days=7 * (s - 1) + 1), "fin": corte + timedelta(days=7 * s),
                "cobros_cartera": float(cob.get(s, 0.0)), "cobros_ventas_nuevas": cv,
                "pagos_proveedores": float(pag.get(s, 0.0)), "compras_nuevas": cn, "gastos_fijos": gasto}
        saldo += fila["cobros_cartera"] + cv - fila["pagos_proveedores"] - cn - gasto
        fila["saldo_final"] = saldo
        filas.append(fila)
    return pd.DataFrame(filas, columns=COLUMNAS)


def _fraccion_llegada(semana: int, dias: float) -> float:
    """Fracción del flujo semanal que ya llega en `semana` si cada venta (o compra) se cobra (o paga)
    `dias` después: las ventas de una semana se reparten entre las dos semanas que tocan."""
    rezago = max(dias, 0.0) / 7
    return min(1.0, max(0.0, semana - rezago))


def episodios(proy: pd.DataFrame, minimo: float) -> list[dict]:
    """Rachas de semanas seguidas con el saldo bajo la caja mínima."""
    salida, actual = [], None
    for r in proy.itertuples(index=False):
        if r.saldo_final < minimo:
            if actual is None:
                actual = {"desde": r.semana, "hasta": r.semana, "inicio": r.inicio, "fin": r.fin,
                          "deficit": minimo - r.saldo_final, "saldo_minimo": r.saldo_final}
            actual.update(hasta=r.semana, fin=r.fin)
            if minimo - r.saldo_final > actual["deficit"]:
                actual.update(deficit=minimo - r.saldo_final, saldo_minimo=r.saldo_final)
        elif actual:
            salida.append(actual)
            actual = None
    if actual:
        salida.append(actual)
    return salida


# --- Reglas ---------------------------------------------------------------------------
def _otras_abiertas(motor: Engine) -> dict[str, list[dict]]:
    with motor.connect() as cx:
        filas = cx.execute(select(decisiones.c.id, decisiones.c.nit, decisiones.c.tipo, decisiones.c.evidencia)
                           .where(decisiones.c.paquete != PAQUETE, decisiones.c.estado.in_(ABIERTAS),
                                  decisiones.c.grupo != "control")).mappings().all()
    salida: dict[str, list[dict]] = {}
    for f in filas:
        salida.setdefault(f["nit"], []).append(dict(f))
    return salida


def evaluar_caja(proy: pd.DataFrame, cobros: pd.DataFrame, cxp: pd.DataFrame, corte: date, parametros: dict,
                 nombres: dict[str, str], otras: dict[str, list[dict]] | None = None,
                 regla_version: int | None = None, supuestos: dict | None = None) -> list[reglas.ResultadoRegla]:
    """Aplica las reglas de Caja sobre la proyección y devuelve decisiones, sin tocar la base."""
    p = {**reglas.parametros_base(PAQUETE), **parametros}
    otras = otras or {}
    if proy.empty:
        return []
    minimo, limite = float(p["caja_minima"]), float(p["aprobacion_lider_max_cop"] or 0)
    criticas = {_norm(c) for c in p.get("categorias_criticas", [])}
    prov_criticos = {str(n) for n in p.get("proveedores_criticos", [])}
    tabla = [{"semana": int(r.semana), "inicio": r.inicio.isoformat(), "saldo_final": round(float(r.saldo_final), 0)}
             for r in proy.itertuples(index=False)]
    base_ev = {"fecha_corte": corte.isoformat(), "caja_minima": minimo, "regla_version": regla_version,
               "saldo_inicial": (supuestos or {}).get("saldo_bancos"),
               "proyeccion": tabla, "supuestos": supuestos or {}}
    ampliaciones = [(d["id"], float((d["evidencia"] or {}).get("aumento_sugerido", 0)))
                    for ds in otras.values() for d in ds if d["tipo"] == "ampliar_cupo"]
    salida: list[reglas.ResultadoRegla] = []
    eps = episodios(proy, minimo)
    max_def = max((e["deficit"] for e in eps), default=1.0)

    for k, e in enumerate(eps):
        urgencia = (SEMANAS - e["desde"] + 1) / SEMANAS
        # Palanca 1: cobrar ya lo que está vencido o vence antes y hoy se espera después de la semana en rojo.
        acelerables = cobros[(cobros["vencimiento"] < e["inicio"]) & (cobros["estado"] != "incobrable")
                             & ((cobros["semana"].isna()) | (cobros["semana"] >= e["desde"])
                                | (cobros["estado"] == "mora_alta"))]
        acelerables = acelerables.assign(extra=[s if (sem is None or sem >= e["desde"]) else s - m
                                                for s, m, sem in zip(acelerables["saldo"], acelerables["monto"],
                                                                     acelerables["semana"])])
        por_cliente = acelerables.groupby("nit")["extra"].sum().sort_values(ascending=False)
        cobrar, acumulado = [], 0.0
        for nit, monto in por_cliente.items():
            if acumulado >= e["deficit"] or monto <= 0:
                break
            ref = next((d["id"] for d in otras.get(nit, []) if d["tipo"] in ("cobrar", "congelar_credito")), None)
            cobrar.append({"nit": nit, "cliente": nombres.get(nit) or nit, "monto": round(float(monto), 0),
                           "decision_cartera": ref})
            acumulado += float(monto)
        falta = max(0.0, e["deficit"] - acumulado)
        # Palanca 2: aplazar pagos no críticos que vencen justo antes o durante la racha en rojo.
        # Se prefieren los que más se acercan a lo que falta, para pedir lo justo y no de más.
        dias = int(p["dias_aplazamiento"])
        pool = cxp[(cxp["fecha_vencimiento"] >= e["inicio"] - timedelta(days=7))
                   & (cxp["fecha_vencimiento"] <= e["fin"]) & ~cxp["nit"].isin(prov_criticos)
                   & ~cxp["categoria"].map(_norm).isin(criticas)].sort_values(
            ["saldo", "numero"], ascending=[False, True]).to_dict("records")
        aplazar, mover = [], 0.0
        while mover < falta and pool:
            restante = falta - mover
            r = next((x for x in pool if x["saldo"] <= 1.5 * restante), None)
            parcial = r is None                    # todos son grandes: se pide correr solo una parte del menor
            r = r or pool[-1]
            pool.remove(r)
            monto = round(restante, -3) if parcial else float(r["saldo"])
            aplazar.append({"nit": r["nit"], "proveedor": r["proveedor"], "numero": r["numero"],
                            "vencimiento": r["fecha_vencimiento"].isoformat(), "saldo": monto,
                            "saldo_documento": float(r["saldo"]), "parcial": parcial,
                            "nueva_fecha": (r["fecha_vencimiento"] + timedelta(days=dias)).isoformat()})
            mover += monto
        financiar = max(0.0, falta - mover)
        ev = {**base_ev, "semana_desde": e["desde"], "semana_hasta": e["hasta"], "inicio": e["inicio"].isoformat(),
              "fin": e["fin"].isoformat(), "deficit": round(e["deficit"], 0), "saldo_minimo": round(e["saldo_minimo"], 0),
              "cobrar": cobrar, "aplazar": aplazar, "financiar": round(financiar, 0),
              "cruces": [], "nombre_mostrar": f"Semana del {_f(e['inicio'])}"}
        if ampliaciones:
            total = sum(m for _, m in ampliaciones)
            ev["cruces"].append(f"Hay {len(ampliaciones)} ampliaciones de cupo pendientes en Cartera por "
                                f"{_pesos(total)} (decisiones {', '.join('#' + str(i) for i, _ in ampliaciones)}): "
                                f"aprobarlas inmoviliza más caja justo antes de esta semana.")
        partes = []
        if cobrar:
            partes.append(f"cobrar {_pesos(acumulado)} de cartera vencida o por vencer a {len(cobrar)} clientes "
                          f"({', '.join(c['cliente'] for c in cobrar[:3])}{'...' if len(cobrar) > 3 else ''})")
        if aplazar:
            partes.append(f"negociar el aplazamiento de {_pesos(mover)} con "
                          f"{len({a['nit'] for a in aplazar})} proveedores")
        if financiar > 0:
            partes.append(f"tramitar {_pesos(financiar)} de crédito de tesorería o factoring")
        recomendacion = (f"Cubrir {_pesos(e['deficit'])} antes del {_f(e['inicio'])}: " + "; ".join(partes) + "."
                         if partes else f"Cubrir {_pesos(e['deficit'])} antes del {_f(e['inicio'])}.")
        semanas_txt = (f"la semana {e['desde']}" if e["desde"] == e["hasta"]
                       else f"las semanas {e['desde']} a {e['hasta']}")
        motivo = (f"Con los cobros esperados según cómo paga cada cliente y los pagos comprometidos, la caja queda "
                  f"bajo el mínimo de {_pesos(minimo)} en {semanas_txt} (del {_f(e['inicio'])} al {_f(e['fin'])}). "
                  f"El punto más bajo es {_pesos(e['saldo_minimo'])}. Quedan {e['desde'] - 1} semanas para actuar.")
        prioridad = round(100 * (0.5 * urgencia + 0.5 * e["deficit"] / max_def), 1)
        iso = e["inicio"].isocalendar()
        salida.append(reglas.ResultadoRegla(
            f"CAJA-{iso[0]}-S{iso[1]:02d}", ev["nombre_mostrar"], "caja_bajo_minimo", prioridad, recomendacion,
            motivo, float(round(e["deficit"], 0)), ev, "gerente" if not limite or e["deficit"] > limite else "lider"))

        if k == 0:            # Los aplazamientos concretos se piden para la racha más próxima.
            for nit, grupo in pd.DataFrame(aplazar).groupby("nit", sort=False) if aplazar else []:
                monto = float(grupo["saldo"].sum())
                proveedor = grupo["proveedor"].iloc[0]
                facturas = grupo.to_dict("records")
                ev_p = {**base_ev, "proveedor": proveedor, "nit": nit, "monto": monto, "facturas": facturas,
                        "dias_aplazamiento": dias, "semana_en_rojo": e["inicio"].isoformat(),
                        "deficit": round(e["deficit"], 0), "nombre_mostrar": proveedor, "cruces": []}
                docs = ", ".join(f"{'parte de ' if f['parcial'] else ''}{f['numero']} del "
                                 f"{_f(date.fromisoformat(f['vencimiento']))}" for f in facturas)
                rec = f"Negociar con {proveedor} correr {dias} días el pago de {_pesos(monto)} ({docs})."
                mot = (f"Vence en plena racha de caja baja: desde la semana del {_f(e['inicio'])} la caja queda hasta "
                       f"{_pesos(e['deficit'])} bajo el mínimo. No es un pago crítico (nómina, impuestos, créditos).")
                prio = round(100 * (0.5 * urgencia + 0.5 * min(1.0, monto / max_def)) * 0.9, 1)
                salida.append(reglas.ResultadoRegla(
                    nit, proveedor, "aplazar_pago_proveedor", prio, rec, mot, monto, ev_p,
                    "gerente" if limite and monto > limite else "lider"))

    if not eps:
        holgura = float(proy["saldo_final"].min()) - minimo
        if holgura >= float(p["excedente_minimo_cop"]):
            peor = proy.loc[proy["saldo_final"].idxmin()]
            usos = ["pronto pago a proveedores que den descuento", "abonar la deuda más costosa"]
            if ampliaciones:
                total = sum(m for _, m in ampliaciones)
                usos.insert(0, f"respaldar las {len(ampliaciones)} ampliaciones de cupo pendientes "
                               f"({_pesos(total)})")
            ev = {**base_ev, "excedente": round(holgura, 0), "semana_mas_baja": int(peor["semana"]),
                  "nombre_mostrar": "Próximas 13 semanas", "cruces": []}
            salida.append(reglas.ResultadoRegla(
                "CAJA-EXCEDENTE", "Próximas 13 semanas", "excedente_de_caja", 30.0,
                f"Usar hasta {_pesos(holgura)} que no se necesitan en 13 semanas: " + ", ".join(usos) + ".",
                f"Aun en la semana más baja (semana {int(peor['semana'])}) la caja queda {_pesos(holgura)} por encima "
                f"del mínimo de {_pesos(minimo)}. Plata quieta en el banco no financia el crecimiento.",
                float(round(holgura, 0)), ev, "gerente"))
    return sorted(salida, key=lambda x: x.prioridad, reverse=True)


def calcular(motor: Engine, carga_id: int, p: dict) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    """Proyección de una carga de cuentas por pagar: (proyección, cobros, cuentas por pagar, supuestos)."""
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
    cxp = cuentas_de_carga(motor, carga_id)
    inicial = saldo_bancos(motor, carga_id) or 0.0
    cart_id, cart_corte = _cartera_al_corte(motor, corte)
    fact = impacto.facturas_de_carga(motor, cart_id) if cart_id else impacto.facturas_de_carga(motor, -1)
    atrasos = atrasos_por_cliente(motor, corte)
    cobros = cobros_esperados(fact, corte, atrasos, p)
    plazos = [(v - e).days for v, e in zip(fact["fecha_vencimiento"], fact["fecha_emision"])]
    plazo_cli = float(pd.Series(plazos).median()) if plazos else 30.0
    vivas = cobros[cobros["estado"] != "incobrable"]
    atraso_medio = (float(sum(atrasos.get(n, p["atraso_por_defecto_dias"]) * s
                              for n, s in zip(vivas["nit"], vivas["saldo"])) / vivas["saldo"].sum())
                    if len(vivas) and vivas["saldo"].sum() else float(p["atraso_por_defecto_dias"]))
    con_emision = cxp.dropna(subset=["fecha_emision"])
    plazo_prov = (float(pd.Series([(v - e).days for v, e in zip(con_emision["fecha_vencimiento"],
                                                                  con_emision["fecha_emision"])]).median())
                  if len(con_emision) else float(p["plazo_proveedores_dias"]))
    venta = ventas_semanales(motor, corte, impacto.facturas_historicas(motor, corte))
    compras_pct = _compras_pct(motor, corte, p)
    proy = proyectar(corte, inicial, cobros, cxp, venta, plazo_cli, atraso_medio, plazo_prov, compras_pct, p)
    supuestos = {"saldo_bancos": inicial, "corte_cartera": cart_corte.isoformat() if cart_corte else None,
                 "venta_semanal": round(venta, 0), "crecimiento_ventas_pct": float(p["crecimiento_ventas_pct"]),
                 "plazo_clientes_dias": round(plazo_cli, 1), "atraso_medio_dias": round(atraso_medio, 1),
                 "plazo_proveedores_dias": round(plazo_prov, 1), "compras_pct_ventas": compras_pct,
                 "clientes_con_historia_de_pago": len(atrasos),
                 "cartera_no_proyectada": round(float(cobros.loc[cobros["estado"] == "incobrable", "saldo"].sum()), 0)}
    return proy, cobros, cxp, supuestos


def detectar(motor: Engine, carga_id: int, regla: dict) -> list[reglas.ResultadoRegla]:
    p = {**reglas.parametros_base(PAQUETE), **regla["parametros"]}
    proy, cobros, cxp, supuestos = calcular(motor, carga_id, p)
    with motor.begin() as cx:
        cx.execute(delete(proyecciones_caja).where(proyecciones_caja.c.carga_id == carga_id))
        cx.execute(insert(proyecciones_caja), [{"carga_id": carga_id, **{k: (v.item() if hasattr(v, "item") else v)
                                                                        for k, v in f.items()}}
                                               for f in proy.to_dict("records")])
    # Se mide antes de sincronizar: la evidencia de cada decisión todavía es la del reporte en que se detectó.
    _medir(motor, carga_id)
    with motor.begin() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
        nombres = {r.nit: r.nombre for r in cx.execute(select(clientes.c.nit, clientes.c.nombre))}
    return evaluar_caja(proy, cobros, cxp, corte, p, nombres, _otras_abiertas(motor), regla["version"], supuestos)


# --- Medición ---------------------------------------------------------------------------
def proyeccion(motor: Engine, carga_id: int) -> pd.DataFrame:
    with motor.connect() as cx:
        filas = cx.execute(select(proyecciones_caja).where(proyecciones_caja.c.carga_id == carga_id)
                           .order_by(proyecciones_caja.c.semana)).mappings().all()
    return pd.DataFrame([dict(f) for f in filas], columns=["id", "carga_id", *COLUMNAS])[COLUMNAS]


def medir(motor: Engine, carga_id: int) -> int:
    """La medición de Caja ya corrió dentro de `detectar` (antes de actualizar la evidencia)."""
    with motor.connect() as cx:
        return cx.execute(select(func.count()).select_from(resultados_caja)
                          .where(resultados_caja.c.carga_id == carga_id)).scalar()


def _medir(motor: Engine, carga_id: int) -> int:
    """Con cada reporte nuevo: cuánto del déficit de cada semana en rojo quedó cubierto y qué pagos se aplazaron."""
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
    proy = proyeccion(motor, carga_id)
    real = saldo_bancos(motor, carga_id)
    cxp = cuentas_de_carga(motor, carga_id)
    vigentes = {(r.nit, str(r.numero)): r.fecha_vencimiento for r in cxp.itertuples(index=False)}
    minimo = float(reglas.regla_activa(motor, PAQUETE)["parametros"]["caja_minima"])
    n = 0
    with motor.begin() as cx:
        objetivo = cx.execute(
            select(decisiones, cargas.c.fecha_corte.label("corte_origen"))
            .join(cargas, cargas.c.id == decisiones.c.carga_origen_id)
            .where(decisiones.c.paquete == PAQUETE, decisiones.c.carga_origen_id != carga_id,
                   decisiones.c.tipo != "excedente_de_caja", decisiones.c.estado != "descartada",
                   cargas.c.fecha_corte < corte)).mappings().all()
        for d in objetivo:
            if cx.execute(select(resultados_caja.c.id).where(
                    resultados_caja.c.decision_id == d["id"], resultados_caja.c.carga_id == carga_id)).first():
                continue
            ev = d["evidencia"]
            antes = float(d["saldo_vencido_inicial"])
            if d["tipo"] == "caja_bajo_minimo":
                ini, fin = date.fromisoformat(ev["inicio"]), date.fromisoformat(ev["fin"])
                semanas = proy[(proy["fin"] >= ini) & (proy["inicio"] <= fin)]
                if len(semanas):
                    despues = max(0.0, minimo - float(semanas["saldo_final"].min()))
                elif real is not None and ini <= corte <= fin + timedelta(days=7):
                    despues = max(0.0, minimo - real)
                else:
                    continue
            else:
                despues = 0.0
                for f in ev["facturas"]:
                    nueva = vigentes.get((f["nit"], str(f["numero"])))
                    original = date.fromisoformat(f["vencimiento"])
                    if nueva is None or nueva <= original:      # se pagó o sigue con la misma fecha
                        despues += float(f["saldo"])
            atendida = cx.execute(select(acciones.c.id).where(
                acciones.c.decision_id == d["id"],
                acciones.c.tipo.in_(("aprobar", "ajustar", "ejecutar")))).first() is not None
            cx.execute(insert(resultados_caja).values(
                decision_id=d["id"], carga_id=carga_id, monto_antes=antes, monto_despues=round(despues, 0),
                cubierto=round(max(0.0, antes - despues), 0), atendida=atendida))
            n += 1
    return n


def precision(motor: Engine) -> dict | None:
    """Saldo real en bancos del último reporte frente a lo que proyectó el reporte anterior para esa fecha."""
    with motor.connect() as cx:
        cortes = cx.execute(select(cargas.c.id, cargas.c.fecha_corte).where(cargas.c.tipo == "pagos")
                            .order_by(cargas.c.fecha_corte.desc(), cargas.c.id.desc()).limit(2)).all()
    if len(cortes) < 2:
        return None
    (ult, f_ult), (ant, _) = cortes
    real = saldo_bancos(motor, ult)
    proy = proyeccion(motor, ant)
    semana = proy[(proy["inicio"] <= f_ult) & (proy["fin"] >= f_ult)]
    if real is None or semana.empty:
        return None
    proyectado = float(semana["saldo_final"].iloc[0])
    return {"fecha": f_ult, "real": real, "proyectado": proyectado,
            "error_pct": round(100 * abs(proyectado - real) / abs(real), 1) if real else None}


def resumen(motor: Engine) -> dict:
    with motor.connect() as cx:
        cortes = cx.execute(select(cargas.c.id, cargas.c.fecha_corte).where(cargas.c.tipo == "pagos")
                            .order_by(cargas.c.fecha_corte, cargas.c.id)).all()
        sub = (select(resultados_caja.c.decision_id, func.max(resultados_caja.c.id).label("rid"))
               .group_by(resultados_caja.c.decision_id).subquery())
        res = cx.execute(select(resultados_caja, decisiones.c.tipo)
                         .join(sub, resultados_caja.c.id == sub.c.rid)
                         .join(decisiones, decisiones.c.id == resultados_caja.c.decision_id)).mappings().all()
        estados = dict(cx.execute(select(decisiones.c.estado, func.count()).where(decisiones.c.paquete == PAQUETE)
                                  .group_by(decisiones.c.estado)).all())
    if not cortes:
        return {"ultimo_reporte": None}
    minimo = float(reglas.regla_activa(motor, PAQUETE)["parametros"]["caja_minima"])
    base, hoy = proyeccion(motor, cortes[0][0]), proyeccion(motor, cortes[-1][0])

    def rojo(df):
        return int((df["saldo_final"] < minimo).sum()) if len(df) else 0

    def deficit(df):
        return float(max(0.0, minimo - df["saldo_final"].min())) if len(df) else 0.0

    deficits = [r for r in res if r["tipo"] == "caja_bajo_minimo"]
    aplazados = [r for r in res if r["tipo"] == "aplazar_pago_proveedor"]
    return {
        "ultimo_reporte": cortes[-1][1], "ultima_carga": cortes[-1][0], "caja_minima": minimo,
        "saldo_bancos": saldo_bancos(motor, cortes[-1][0]), "proyeccion": hoy,
        "semanas_rojo_base": rojo(base), "semanas_rojo_hoy": rojo(hoy),
        "deficit_max_base": deficit(base), "deficit_max_hoy": deficit(hoy),
        "saldo_minimo_hoy": float(hoy["saldo_final"].min()) if len(hoy) else None,
        "precision": precision(motor), "decisiones_por_estado": estados,
        "deficit_cubierto_atendidas": float(sum(r["cubierto"] for r in deficits if r["atendida"])),
        "deficit_cubierto_no_atendidas": float(sum(r["cubierto"] for r in deficits if not r["atendida"])),
        "pagos_aplazados": float(sum(r["cubierto"] for r in aplazados)),
    }
