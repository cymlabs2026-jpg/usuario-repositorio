"""Paquete de decisión Margen.

Pregunta que responde: ¿qué clientes, productos y descuentos dejan utilidad y cuáles la destruyen?
Usa el reporte de ventas por producto (líneas de factura con venta neta y costo) y cruza con las
decisiones abiertas de Cartera y Foco comercial.

Todo es determinístico: mismos datos + misma versión de reglas = mismas decisiones.
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine

from . import reglas
from .db import acciones, cargas, clientes, decisiones, lineas_venta, resultados_margen

PAQUETE = "margen"
TIPOS = {"revisar_margen_cliente": "Margen bajo de cliente", "revisar_margen_producto": "Margen bajo de producto",
         "descuento_fuera_politica": "Descuento fuera de política"}
ABIERTAS = ("pendiente", "aprobada", "ajustada", "ejecutada")


def _pesos(x: float) -> str:
    return "$" + f"{x:,.0f}".replace(",", ".")


def id_producto(producto: str) -> str:
    """Las decisiones de producto se guardan con un identificador corto en lugar del NIT."""
    return "P-" + hashlib.sha1(producto.strip().lower().encode()).hexdigest()[:12]


# --- Datos --------------------------------------------------------------------------
def lineas_historicas(motor: Engine, hasta: date) -> pd.DataFrame:
    """Líneas de todos los reportes cargados hasta `hasta`; cada línea (factura + producto) cuenta una vez."""
    with motor.connect() as cx:
        filas = cx.execute(
            select(lineas_venta, cargas.c.fecha_corte).join(cargas, cargas.c.id == lineas_venta.c.carga_id)
            .where(cargas.c.fecha_corte <= hasta)).mappings().all()
    cols = ["nit", "numero", "fecha", "producto", "valor", "costo", "descuento", "cantidad", "vendedor",
            "carga_id", "fecha_corte"]
    df = pd.DataFrame([dict(f) for f in filas], columns=[*cols, "id"])
    if df.empty:
        return df[cols]
    df = (df.sort_values(["fecha_corte", "carga_id"])
          .drop_duplicates(["nit", "numero", "producto"], keep="last"))
    df["descuento"] = df["descuento"].fillna(0.0)
    return df[df["fecha"] <= hasta][cols].reset_index(drop=True)


def _ventana(lineas: pd.DataFrame, corte: date, dias: int) -> pd.DataFrame:
    return lineas[(lineas["fecha"] > corte - timedelta(days=dias)) & (lineas["fecha"] <= corte)]


def margen_pct(lineas: pd.DataFrame) -> float | None:
    ventas = float(lineas["valor"].sum())
    return round(100 * (ventas - float(lineas["costo"].sum())) / ventas, 2) if ventas > 0 else None


def _agrupar(lineas: pd.DataFrame, por: str) -> pd.DataFrame:
    g = lineas.assign(perdida=(lineas["costo"] - lineas["valor"]).clip(lower=0),
                      bajo_costo=lineas["valor"] < lineas["costo"])
    r = g.groupby(por).agg(ventas=("valor", "sum"), costo=("costo", "sum"), descuento=("descuento", "sum"),
                           lineas=("valor", "size"), perdida=("perdida", "sum"),
                           bajo_costo=("bajo_costo", "sum")).reset_index()
    r["margen_pct"] = (100 * (r["ventas"] - r["costo"]) / r["ventas"]).where(r["ventas"] > 0)
    r["descuento_pct"] = (100 * r["descuento"] / (r["ventas"] + r["descuento"])).where(r["ventas"] > 0)
    return r


# --- Reglas ---------------------------------------------------------------------------
def _cruces(nit: str, otras: dict[str, list[dict]]) -> list[str]:
    textos = []
    for d in otras.get(nit, []):
        ev = d["evidencia"] or {}
        if d["tipo"] == "ampliar_cupo":
            textos.append(f"Cartera recomienda ampliarle el cupo (decisión #{d['id']}): con este margen, revisar "
                          f"precios antes de aprobarlo.")
        elif d["tipo"] in ("cobrar", "congelar_credito"):
            textos.append(f"Tiene {_pesos(ev.get('saldo_vencido', 0))} vencidos (decisión #{d['id']}): financiar "
                          f"esa cartera reduce aún más su margen real.")
        elif d["tipo"] == "crecer_cliente":
            textos.append(f"Foco comercial lo marca para visita de crecimiento (decisión #{d['id']}): llevar la "
                          f"propuesta de precios a esa visita.")
        elif d["tipo"] == "recuperar_cliente":
            textos.append(f"Foco comercial alerta que está comprando menos (decisión #{d['id']}): cuidado al subir "
                          f"precios; primero entender la caída.")
    return textos


def _otras_abiertas(motor: Engine) -> dict[str, list[dict]]:
    with motor.connect() as cx:
        filas = cx.execute(select(decisiones.c.id, decisiones.c.nit, decisiones.c.tipo, decisiones.c.evidencia)
                           .where(decisiones.c.paquete != PAQUETE, decisiones.c.estado.in_(ABIERTAS),
                                  decisiones.c.grupo != "control")).mappings().all()
    salida: dict[str, list[dict]] = {}
    for f in filas:
        salida.setdefault(f["nit"], []).append(dict(f))
    return salida


def evaluar_margen(lineas: pd.DataFrame, corte: date, parametros: dict, nombres: dict[str, str],
                   otras: dict[str, list[dict]] | None = None,
                   regla_version: int | None = None) -> list[reglas.ResultadoRegla]:
    """Aplica las reglas de Margen y devuelve decisiones, sin tocar la base."""
    p = {**reglas.parametros_base(PAQUETE), **parametros}
    otras = otras or {}
    if lineas.empty:
        return []
    ventana = _ventana(lineas, corte, int(round(int(p["meses_analisis"]) * 30.4)))
    if ventana.empty:
        return []
    minimo, desc_max = float(p["margen_minimo_pct"]), float(p["descuento_maximo_pct"])
    global_pct = margen_pct(ventana)
    por_cliente = _agrupar(ventana, "nit")
    por_producto = _agrupar(ventana, "producto")
    clave = reglas.clientes_clave(dict(zip(por_cliente["nit"], por_cliente["ventas"].clip(lower=0))),
                                  float(p["pct_clientes_clave"]))
    excl_cli = {str(n) for n in p.get("clientes_excepcion", [])}
    excl_prod = {str(x).strip().lower() for x in p.get("productos_excepcion", [])}
    limite = float(p["aprobacion_lider_max_cop"] or 0)
    candidatas: list[tuple] = []          # (tipo, id, nombre, monto, severidad, recomendacion, motivo, evidencia, clave)
    base_ev = {"fecha_corte": corte.isoformat(), "meses_analisis": int(p["meses_analisis"]),
               "margen_minimo_pct": minimo, "margen_empresa_pct": global_pct, "regla_version": regla_version}

    for r in por_cliente.itertuples(index=False):
        if r.nit in excl_cli or r.ventas < p["ventas_minimas_cliente"]:
            continue
        nombre = nombres.get(r.nit) or r.nit
        es_clave = r.nit in clave
        prods = _agrupar(ventana[ventana["nit"] == r.nit], "producto").sort_values("margen_pct")
        peores = prods[prods["margen_pct"] < minimo].head(3)
        ev = {**base_ev, "cliente": nombre, "nit": r.nit, "ventas": float(r.ventas), "costo": float(r.costo),
              "margen_pct": round(float(r.margen_pct), 2), "descuento": float(r.descuento),
              "descuento_pct": round(float(r.descuento_pct), 2), "cliente_clave": es_clave,
              "lineas_bajo_costo": int(r.bajo_costo), "perdida_bajo_costo": float(r.perdida),
              "cruces": _cruces(r.nit, otras),
              "productos": [{"producto": x.producto, "ventas": float(x.ventas),
                             "margen_pct": round(float(x.margen_pct), 1),
                             "descuento_pct": round(float(x.descuento_pct), 1)}
                            for x in prods.head(8).itertuples(index=False)]}
        quien = " Por ser cliente clave, la negociación la lleva el gerente comercial." if es_clave else ""
        nombres_peores = ", ".join(peores["producto"]) or "los de menor margen"
        if r.margen_pct < minimo:
            faltante = (minimo - r.margen_pct) / 100 * r.ventas
            ev["utilidad_en_juego"] = round(faltante, 0)
            if r.descuento_pct > desc_max:
                accion = (f"Bajar el descuento del {r.descuento_pct:.1f}% a la política ({desc_max:.0f}%) y "
                          f"renegociar precios de {nombres_peores}.{quien}")
            else:
                accion = (f"Revisar precios de {nombres_peores} o mover su compra hacia productos de mejor "
                          f"margen.{quien}")
            motivo = (f"{nombre} deja un margen bruto de {r.margen_pct:.1f}% sobre {_pesos(r.ventas)} vendidos en "
                      f"{int(p['meses_analisis'])} meses (mínimo {minimo:.0f}%, empresa {global_pct:.1f}%). Llevarlo "
                      f"al mínimo suma {_pesos(faltante)} de utilidad. Descuento promedio: {r.descuento_pct:.1f}%.")
            candidatas.append(("revisar_margen_cliente", r.nit, nombre, faltante, (minimo - r.margen_pct) / minimo,
                               accion, motivo, ev, es_clave))
        elif r.descuento_pct > desc_max:
            exceso = (r.descuento_pct - desc_max) / 100 * (r.ventas + r.descuento)
            ev["utilidad_en_juego"] = round(exceso, 0)
            accion = f"Volver a la política de descuento ({desc_max:.0f}%) en los próximos pedidos.{quien}"
            motivo = (f"{nombre} recibe un descuento promedio de {r.descuento_pct:.1f}% (política {desc_max:.0f}%). "
                      f"El exceso equivale a {_pesos(exceso)} en {int(p['meses_analisis'])} meses. Su margen es "
                      f"{r.margen_pct:.1f}%.")
            candidatas.append(("descuento_fuera_politica", r.nit, nombre, exceso,
                               (r.descuento_pct - desc_max) / max(desc_max, 1), accion, motivo, ev, es_clave))

    for r in por_producto.itertuples(index=False):
        if r.producto.strip().lower() in excl_prod or r.ventas < p["ventas_minimas_producto"] \
                or r.margen_pct >= minimo:
            continue
        faltante = (minimo - r.margen_pct) / 100 * r.ventas
        clientes_p = _agrupar(ventana[ventana["producto"] == r.producto], "nit").sort_values("margen_pct")
        ev = {**base_ev, "producto": r.producto, "ventas": float(r.ventas), "costo": float(r.costo),
              "margen_pct": round(float(r.margen_pct), 2), "descuento_pct": round(float(r.descuento_pct), 2),
              "lineas_bajo_costo": int(r.bajo_costo), "perdida_bajo_costo": float(r.perdida),
              "utilidad_en_juego": round(faltante, 0), "cruces": [],
              "clientes": [{"cliente": nombres.get(x.nit) or x.nit, "nit": x.nit, "ventas": float(x.ventas),
                            "margen_pct": round(float(x.margen_pct), 1)}
                           for x in clientes_p.head(8).itertuples(index=False)]}
        if r.margen_pct < 0:
            accion = ("Se está vendiendo por debajo del costo: subir el precio de lista o negociar el costo con el "
                      "proveedor antes del próximo pedido.")
        else:
            accion = ("Subir el precio de lista o negociar el costo con el proveedor. Si es un producto gancho, "
                      "marcarlo como excepción en las reglas.")
        bajo = (f" {int(r.bajo_costo)} líneas se vendieron bajo el costo ({_pesos(r.perdida)} de pérdida)."
                if r.bajo_costo else "")
        motivo = (f"{r.producto} deja un margen de {r.margen_pct:.1f}% sobre {_pesos(r.ventas)} vendidos "
                  f"(mínimo {minimo:.0f}%). Llevarlo al mínimo suma {_pesos(faltante)} de utilidad.{bajo}")
        candidatas.append(("revisar_margen_producto", id_producto(r.producto), r.producto, faltante,
                           min(1.0, (minimo - r.margen_pct) / minimo), accion, motivo, ev, False))

    if not candidatas:
        return []
    max_monto = max(c[3] for c in candidatas) or 1.0
    salida = []
    for tipo, ident, nombre, monto, sev, accion, motivo, ev, es_clave in candidatas:
        prioridad = round(100 * (0.7 * monto / max_monto + 0.3 * min(1.0, max(sev, 0.0))), 1)
        nivel = "gerente" if (limite and monto > limite) or es_clave else "lider"
        salida.append(reglas.ResultadoRegla(ident, nombre, tipo, prioridad, accion, motivo, float(round(monto, 0)),
                                            ev, nivel))
    return sorted(salida, key=lambda x: x.prioridad, reverse=True)


def detectar(motor: Engine, carga_id: int, regla: dict) -> list[reglas.ResultadoRegla]:
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
        nombres = {r.nit: r.nombre for r in cx.execute(select(clientes.c.nit, clientes.c.nombre))}
    return evaluar_margen(lineas_historicas(motor, corte), corte, regla["parametros"], nombres,
                          _otras_abiertas(motor), regla["version"])


# --- Medición ---------------------------------------------------------------------------
def medir(motor: Engine, carga_id: int) -> int:
    """Margen del cliente o producto desde que se detectó la decisión, frente al margen que tenía."""
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
    hist = lineas_historicas(motor, corte)
    n = 0
    with motor.begin() as cx:
        objetivo = cx.execute(
            select(decisiones, cargas.c.fecha_corte.label("corte_origen"))
            .join(cargas, cargas.c.id == decisiones.c.carga_origen_id)
            .where(decisiones.c.paquete == PAQUETE, decisiones.c.carga_origen_id != carga_id,
                   decisiones.c.estado != "descartada", cargas.c.fecha_corte < corte)).mappings().all()
        for d in objetivo:
            if cx.execute(select(resultados_margen.c.id).where(
                    resultados_margen.c.decision_id == d["id"], resultados_margen.c.carga_id == carga_id)).first():
                continue
            despues = hist[hist["fecha"] > d["corte_origen"]]
            if d["tipo"] == "revisar_margen_producto":
                despues = despues[despues["producto"] == d["evidencia"]["producto"]]
            else:
                despues = despues[despues["nit"] == d["nit"]]
            m_despues = margen_pct(despues)
            if m_despues is None:          # sin ventas desde la decisión: no hay nada que medir todavía
                continue
            ventas_d = float(despues["valor"].sum())
            m_antes = float(d["evidencia"]["margen_pct"])
            atendida = cx.execute(select(acciones.c.id).where(
                acciones.c.decision_id == d["id"],
                acciones.c.tipo.in_(("aprobar", "ajustar", "ejecutar")))).first() is not None
            cx.execute(insert(resultados_margen).values(
                decision_id=d["id"], carga_id=carga_id, margen_antes_pct=m_antes, margen_despues_pct=m_despues,
                ventas_despues=ventas_d, utilidad_adicional=round((m_despues - m_antes) / 100 * ventas_d, 0),
                atendida=atendida))
            n += 1
    return n


def margen_mensual(motor: Engine) -> pd.DataFrame:
    """Ventas, costo y margen % por mes con todo lo cargado."""
    with motor.connect() as cx:
        ultimo = cx.execute(select(func.max(cargas.c.fecha_corte)).where(cargas.c.tipo == "lineas")).scalar()
    if not ultimo:
        return pd.DataFrame(columns=["mes", "ventas", "margen_pct"])
    h = lineas_historicas(motor, ultimo)
    h["mes"] = [f"{f.year}-{f.month:02d}" for f in h["fecha"]]
    m = h.groupby("mes").agg(ventas=("valor", "sum"), costo=("costo", "sum")).reset_index()
    m["margen_pct"] = (100 * (m["ventas"] - m["costo"]) / m["ventas"]).round(1)
    return m[["mes", "ventas", "margen_pct"]]


def resumen(motor: Engine) -> dict:
    """Margen de los últimos 90 días frente a la línea base (primer reporte) y resultados de las decisiones."""
    with motor.connect() as cx:
        cortes = cx.execute(select(cargas.c.id, cargas.c.fecha_corte).where(cargas.c.tipo == "lineas")
                            .order_by(cargas.c.fecha_corte, cargas.c.id)).all()
        sub = (select(resultados_margen.c.decision_id, func.max(resultados_margen.c.id).label("rid"))
               .group_by(resultados_margen.c.decision_id).subquery())
        res = cx.execute(select(resultados_margen).join(sub, resultados_margen.c.id == sub.c.rid)).mappings().all()
        abiertas = cx.execute(select(decisiones.c.saldo_vencido_inicial, decisiones.c.evidencia)
                              .where(decisiones.c.paquete == PAQUETE,
                                     decisiones.c.estado.in_(ABIERTAS))).mappings().all()
        estados = dict(cx.execute(select(decisiones.c.estado, func.count()).where(decisiones.c.paquete == PAQUETE)
                                  .group_by(decisiones.c.estado)).all())
    if not cortes:
        return {"ultimo_reporte": None}
    base_id, base_corte = cortes[0]
    ult_corte = cortes[-1][1]
    with motor.connect() as cx:
        filas_base = cx.execute(select(lineas_venta).where(lineas_venta.c.carga_id == base_id)).mappings().all()
    base = _ventana(pd.DataFrame([dict(f) for f in filas_base]), base_corte, 90)
    hoy = _ventana(lineas_historicas(motor, ult_corte), ult_corte, 90)
    m_base, m_hoy = margen_pct(base), margen_pct(hoy)
    puntos = round(m_hoy - m_base, 2) if m_base is not None and m_hoy is not None else None
    ventas_90 = float(hoy["valor"].sum())
    return {
        "ultimo_reporte": ult_corte, "margen_base_pct": m_base, "margen_hoy_pct": m_hoy, "puntos": puntos,
        "utilidad_adicional_90d": round(puntos / 100 * ventas_90, 0) if puntos is not None else None,
        "ventas_90d": ventas_90, "decisiones_por_estado": estados,
        "utilidad_en_juego_abiertas": float(sum(a["saldo_vencido_inicial"] for a in abiertas)),
        "decisiones_medidas": len(res),
        "utilidad_adicional_atendidas": float(sum(r["utilidad_adicional"] for r in res if r["atendida"])),
        "utilidad_adicional_no_atendidas": float(sum(r["utilidad_adicional"] for r in res if not r["atendida"])),
    }
