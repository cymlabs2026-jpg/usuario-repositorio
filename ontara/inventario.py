"""Paquete de decisión Inventario.

Pregunta que responde: ¿qué producto se va a agotar y hay que pedir ya, y qué plata está quieta en la bodega?
Cruza el reporte de existencias (cantidad y costo por producto) con la demanda de cada producto, que sale del
reporte de ventas por producto de Margen. También cruza con Margen (no reponer lo que se vende bajo el costo) y
con Caja (el inventario lento es caja que se puede liberar cuando hay semanas en rojo).

Todo es determinístico: mismos datos + misma versión de reglas = mismas decisiones.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine

from . import margen, reglas
from .db import acciones, cargas, decisiones, existencias, resultados_inventario

PAQUETE = "inventario"
TIPOS = {"reponer_producto": "Reponer antes del quiebre", "liberar_inventario": "Liberar capital en inventario"}
ABIERTAS = ("pendiente", "aprobada", "ajustada", "ejecutada")


def _pesos(x: float) -> str:
    return ("-$" if x < 0 else "$") + f"{abs(x):,.0f}".replace(",", ".")


def _num(x: float) -> str:
    return f"{x:,.0f}".replace(",", ".")


# --- Datos --------------------------------------------------------------------------
def existencias_de_carga(motor: Engine, carga_id: int) -> pd.DataFrame:
    """Existencias por producto (se suman las bodegas)."""
    with motor.connect() as cx:
        filas = cx.execute(select(existencias).where(existencias.c.carga_id == carga_id)).mappings().all()
    df = pd.DataFrame([dict(f) for f in filas], columns=["id", "carga_id", "producto", "bodega", "cantidad",
                                                         "costo_total"])
    if df.empty:
        return pd.DataFrame(columns=["producto", "cantidad", "costo_total", "costo_unitario", "bodegas"])
    g = df.groupby("producto").agg(cantidad=("cantidad", "sum"), costo_total=("costo_total", "sum"),
                                   bodegas=("bodega", lambda b: ", ".join(sorted({x for x in b if x})))).reset_index()
    g["costo_unitario"] = (g["costo_total"] / g["cantidad"]).where(g["cantidad"] > 0)
    return g


def demanda(motor: Engine, corte: date, dias: int) -> pd.DataFrame:
    """Demanda diaria por producto con las ventas por producto de los últimos `dias`."""
    lineas = margen.lineas_historicas(motor, corte)
    cols = ["producto", "unidades_dia", "costo_dia", "ventas_dia", "margen_pct", "ultima_venta"]
    if lineas.empty:
        return pd.DataFrame(columns=cols)
    ultima = lineas.groupby("producto")["fecha"].max()
    v = lineas[lineas["fecha"] > corte - timedelta(days=dias)]
    g = v.groupby("producto").agg(unidades=("cantidad", "sum"), costo=("costo", "sum"),
                                  ventas=("valor", "sum")).reindex(ultima.index, fill_value=0.0)
    salida = pd.DataFrame({"producto": ultima.index, "unidades_dia": (g["unidades"] / dias).values,
                           "costo_dia": (g["costo"] / dias).values, "ventas_dia": (g["ventas"] / dias).values,
                           "margen_pct": ((100 * (g["ventas"] - g["costo"]) / g["ventas"])
                                          .where(g["ventas"] > 0)).values,
                           "ultima_venta": ultima.values})
    return salida[cols]


def hay_ventas_por_producto(motor: Engine) -> bool:
    with motor.connect() as cx:
        return cx.execute(select(cargas.c.id).where(cargas.c.tipo == "lineas").limit(1)).first() is not None


# --- Reglas ---------------------------------------------------------------------------
def _otras_abiertas(motor: Engine) -> list[dict]:
    with motor.connect() as cx:
        return [dict(f) for f in cx.execute(
            select(decisiones.c.id, decisiones.c.nit, decisiones.c.tipo, decisiones.c.evidencia,
                   decisiones.c.saldo_vencido_inicial)
            .where(decisiones.c.paquete.in_(("margen", "caja")), decisiones.c.estado.in_(ABIERTAS))).mappings()]


def evaluar_inventario(exist: pd.DataFrame, dem: pd.DataFrame, corte: date, parametros: dict,
                       otras: list[dict] | None = None, regla_version: int | None = None
                       ) -> list[reglas.ResultadoRegla]:
    """Aplica las reglas de Inventario y devuelve decisiones, sin tocar la base."""
    p = {**reglas.parametros_base(PAQUETE), **parametros}
    otras = otras or []
    if exist.empty:
        return []
    if dem.empty:
        dem = pd.DataFrame(columns=["producto", "unidades_dia", "costo_dia", "ventas_dia", "margen_pct", "ultima_venta"])
    rep, seg, obj = float(p["dias_reposicion"]), float(p["dias_seguridad"]), float(p["dias_objetivo"])
    exceso_dias, quieto_dias = float(p["dias_exceso"]), float(p["dias_sin_movimiento"])
    minimo, limite = float(p["capital_minimo_cop"]), float(p["aprobacion_lider_max_cop"] or 0)
    excluidos = {str(x).strip().lower() for x in p.get("productos_excepcion", [])}
    margen_dec = {(d["evidencia"] or {}).get("producto"): d["id"] for d in otras
                  if d["tipo"] == "revisar_margen_producto"}
    caja_roja = [d for d in otras if d["tipo"] == "caja_bajo_minimo"]
    datos = exist.merge(dem, on="producto", how="left")
    datos[["unidades_dia", "costo_dia", "ventas_dia"]] = datos[["unidades_dia", "costo_dia", "ventas_dia"]].fillna(0.0)
    total_inv = float(datos["costo_total"].sum()) or 1.0
    base_ev = {"fecha_corte": corte.isoformat(), "regla_version": regla_version, "dias_reposicion": rep,
               "dias_seguridad": seg, "dias_objetivo": obj, "capital_total_inventario": total_inv}
    candidatas: list[tuple] = []          # (tipo, producto, monto, severidad, recomendacion, motivo, evidencia)

    for r in datos.itertuples(index=False):
        if str(r.producto).strip().lower() in excluidos:
            continue
        costo_u = float(r.costo_unitario) if r.costo_unitario == r.costo_unitario and r.costo_unitario else \
            (float(r.costo_dia / r.unidades_dia) if r.unidades_dia else 0.0)
        cobertura = float(r.cantidad / r.unidades_dia) if r.unidades_dia > 0 else math.inf
        ultima = r.ultima_venta if isinstance(r.ultima_venta, date) else None
        dias_sin_venta = (corte - ultima).days if ultima else None
        ev = {**base_ev, "producto": r.producto, "cantidad": float(r.cantidad), "costo_total": float(r.costo_total),
              "costo_unitario": round(costo_u, 0), "unidades_dia": round(float(r.unidades_dia), 2),
              "ventas_dia": round(float(r.ventas_dia), 0),
              "cobertura_dias": None if math.isinf(cobertura) else round(cobertura, 1),
              "ultima_venta": ultima.isoformat() if ultima else None, "dias_sin_venta": dias_sin_venta,
              "margen_pct": None if r.margen_pct != r.margen_pct or r.margen_pct is None else round(float(r.margen_pct), 1),
              "bodegas": r.bodegas, "cruces": []}

        if r.unidades_dia > 0 and cobertura < rep + seg:
            unidades = max(0.0, r.unidades_dia * (rep + obj) - r.cantidad)
            compra = unidades * costo_u
            if compra < minimo:
                continue
            sin_stock = max(0.0, rep - cobertura)
            riesgo = r.ventas_dia * sin_stock
            ev.update(unidades_sugeridas=math.ceil(unidades), compra_sugerida=round(compra, 0),
                      dias_sin_stock=round(sin_stock, 1), ventas_en_riesgo=round(riesgo, 0))
            agotado = r.cantidad <= 0
            if ev["margen_pct"] is not None and ev["margen_pct"] < 0:
                ref = margen_dec.get(r.producto)
                ev["cruces"].append(f"Se vende bajo el costo (margen {ev['margen_pct']:.1f}%"
                                    + (f", decisión de Margen #{ref}" if ref else "") + "): reponer al precio actual "
                                    "amplía la pérdida.")
                accion = (f"Corregir el precio de {r.producto} antes de reponer; luego pedir "
                          f"{_num(math.ceil(unidades))} unidades ({_pesos(compra)}).")
            else:
                accion = (f"Pedir {_num(math.ceil(unidades))} unidades de {r.producto} ({_pesos(compra)}) para cubrir "
                          f"{obj:.0f} días después de que llegue el pedido.")
            for d in caja_roja:
                ev["cruces"].append(f"Caja tiene semanas bajo el mínimo desde el {d['evidencia'].get('inicio')} "
                                    f"(decisión #{d['id']}): negociar el plazo de esta compra.")
            estado = "Está agotado" if agotado else f"Alcanza para {cobertura:.0f} días"
            motivo = (f"{estado} y un pedido tarda {rep:.0f} días en llegar (más {seg:.0f} de colchón). Vende "
                      f"{r.unidades_dia:.1f} unidades por día ({_pesos(r.ventas_dia)})."
                      + (f" Si no se pide hoy, quedan {sin_stock:.0f} días sin producto: {_pesos(riesgo)} en ventas "
                         f"en riesgo." if sin_stock > 0 else ""))
            severidad = 1.0 if agotado else 1 - cobertura / (rep + seg)
            candidatas.append(("reponer_producto", r.producto, compra, severidad, accion, motivo, ev))
            continue

        quieto = r.cantidad > 0 and (r.unidades_dia == 0 or (dias_sin_venta is not None and dias_sin_venta >= quieto_dias))
        if quieto:
            capital = float(r.costo_total)
            desde = f"no se vende hace {dias_sin_venta} días" if dias_sin_venta is not None else \
                "no tiene ventas registradas"
            accion = (f"Liquidar o devolver al proveedor {_num(r.cantidad)} unidades de {r.producto}: "
                      f"{_pesos(capital)} quietos en la bodega.")
            motivo = f"{r.producto} {desde}. Todo su inventario ({_pesos(capital)}) es capital sin movimiento."
        elif cobertura > exceso_dias:
            sobra = r.cantidad - r.unidades_dia * obj
            capital = sobra * costo_u
            accion = (f"No volver a comprar {r.producto} hasta bajar a {obj:.0f} días y mover {_num(sobra)} unidades "
                      f"con promoción o devolución: libera {_pesos(capital)}.")
            motivo = (f"Hay inventario para {cobertura:.0f} días (lento a partir de {exceso_dias:.0f}). Por encima de "
                      f"{obj:.0f} días sobran {_num(sobra)} unidades: {_pesos(capital)} de capital inmovilizado.")
        else:
            continue
        if capital < minimo:
            continue
        ev.update(capital_liberable=round(capital, 0), sin_movimiento=bool(quieto))
        for d in caja_roja:
            ev["cruces"].append(f"Caja tiene semanas bajo el mínimo (decisión #{d['id']}): liberar este inventario "
                                f"aporta hasta {_pesos(capital)}.")
        severidad = 1.0 if quieto else min(1.0, (cobertura - exceso_dias) / exceso_dias)
        candidatas.append(("liberar_inventario", r.producto, capital, severidad, accion, motivo, ev))

    if not candidatas:
        return []
    maximo = max(c[2] for c in candidatas) or 1.0
    salida = []
    for tipo, producto, monto, sev, accion, motivo, ev in candidatas:
        peso_urgencia = 0.6 if tipo == "reponer_producto" else 0.3
        prioridad = round(100 * ((1 - peso_urgencia) * monto / maximo + peso_urgencia * max(0.0, min(sev, 1.0))), 1)
        nivel = "gerente" if limite and monto > limite else "lider"
        salida.append(reglas.ResultadoRegla(margen.id_producto(producto), producto, tipo, prioridad, accion, motivo,
                                            float(round(monto, 0)), ev, nivel))
    return sorted(salida, key=lambda x: x.prioridad, reverse=True)


def detectar(motor: Engine, carga_id: int, regla: dict) -> list[reglas.ResultadoRegla]:
    p = {**reglas.parametros_base(PAQUETE), **regla["parametros"]}
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
    if not hay_ventas_por_producto(motor):
        return []                        # sin demanda por producto no se puede saber qué rota y qué no
    return evaluar_inventario(existencias_de_carga(motor, carga_id), demanda(motor, corte, int(p["dias_demanda"])),
                              corte, p, _otras_abiertas(motor), regla["version"])


# --- Medición ---------------------------------------------------------------------------
def medir(motor: Engine, carga_id: int) -> int:
    """Con el siguiente reporte de existencias: si el producto se agotó y cuánto capital se liberó."""
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
    p = reglas.regla_activa(motor, PAQUETE)["parametros"]
    hoy = existencias_de_carga(motor, carga_id).set_index("producto")
    dem = demanda(motor, corte, int(p["dias_demanda"])).set_index("producto")
    n = 0
    with motor.begin() as cx:
        objetivo = cx.execute(
            select(decisiones).join(cargas, cargas.c.id == decisiones.c.carga_origen_id)
            .where(decisiones.c.paquete == PAQUETE, decisiones.c.carga_origen_id != carga_id,
                   decisiones.c.estado != "descartada", cargas.c.fecha_corte < corte)).mappings().all()
        for d in objetivo:
            if cx.execute(select(resultados_inventario.c.id).where(
                    resultados_inventario.c.decision_id == d["id"],
                    resultados_inventario.c.carga_id == carga_id)).first():
                continue
            ev = d["evidencia"]
            prod = ev["producto"]
            cantidad = float(hoy["cantidad"].get(prod, 0.0))
            uds = float(dem["unidades_dia"].get(prod, 0.0)) if prod in dem.index else 0.0
            cobertura = cantidad / uds if uds > 0 else None
            antes = float(d["saldo_vencido_inicial"])
            if d["tipo"] == "reponer_producto":
                despues, liberado = antes, 0.0
            else:
                costo_u = float(ev.get("costo_unitario") or 0)
                despues = cantidad * costo_u if ev.get("sin_movimiento") else \
                    max(0.0, cantidad - uds * float(ev["dias_objetivo"])) * costo_u
                liberado = max(0.0, antes - despues)
            atendida = cx.execute(select(acciones.c.id).where(
                acciones.c.decision_id == d["id"],
                acciones.c.tipo.in_(("aprobar", "ajustar", "ejecutar")))).first() is not None
            cx.execute(insert(resultados_inventario).values(
                decision_id=d["id"], carga_id=carga_id, capital_antes=antes, capital_despues=round(despues, 0),
                liberado=round(liberado, 0), quiebre=cantidad <= 0 and uds > 0,
                cobertura_dias=None if cobertura is None else round(cobertura, 1), atendida=atendida))
            n += 1
    return n


def metricas(motor: Engine, carga_id: int, p: dict) -> dict:
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
    ex = existencias_de_carga(motor, carga_id)
    dem = demanda(motor, corte, int(p["dias_demanda"]))
    d = ex.merge(dem, on="producto", how="left").fillna({"unidades_dia": 0.0, "costo_dia": 0.0})
    total = float(d["costo_total"].sum())
    costo_dia = float(dem["costo_dia"].sum()) if len(dem) else 0.0
    cobertura = d["cantidad"] / d["unidades_dia"].where(d["unidades_dia"] > 0)
    lento = d[(cobertura.isna() & (d["cantidad"] > 0)) | (cobertura > float(p["dias_exceso"]))]
    return {"fecha": corte, "capital": total, "dias_inventario": round(total / costo_dia, 1) if costo_dia else None,
            "capital_lento": float(lento["costo_total"].sum()),
            "pct_lento": round(100 * float(lento["costo_total"].sum()) / total, 1) if total else 0.0,
            "agotados": int(((d["cantidad"] <= 0) & (d["unidades_dia"] > 0)).sum()), "productos": int(len(d))}


def resumen(motor: Engine) -> dict:
    with motor.connect() as cx:
        cortes = cx.execute(select(cargas.c.id).where(cargas.c.tipo == "inventario")
                            .order_by(cargas.c.fecha_corte, cargas.c.id)).scalars().all()
        sub = (select(resultados_inventario.c.decision_id, func.max(resultados_inventario.c.id).label("rid"))
               .group_by(resultados_inventario.c.decision_id).subquery())
        res = cx.execute(select(resultados_inventario, decisiones.c.tipo)
                         .join(sub, resultados_inventario.c.id == sub.c.rid)
                         .join(decisiones, decisiones.c.id == resultados_inventario.c.decision_id)).mappings().all()
        estados = dict(cx.execute(select(decisiones.c.estado, func.count()).where(decisiones.c.paquete == PAQUETE)
                                  .group_by(decisiones.c.estado)).all())
    if not cortes:
        return {"ultimo_reporte": None}
    p = reglas.regla_activa(motor, PAQUETE)["parametros"]
    base, hoy = metricas(motor, cortes[0], p), metricas(motor, cortes[-1], p)
    reponer = [r for r in res if r["tipo"] == "reponer_producto"]
    liberar = [r for r in res if r["tipo"] == "liberar_inventario"]
    return {
        "ultimo_reporte": hoy["fecha"], "base": base, "hoy": hoy, "hay_demanda": hay_ventas_por_producto(motor),
        "decisiones_por_estado": estados,
        "reposiciones_medidas": len(reponer), "quiebres": sum(1 for r in reponer if r["quiebre"]),
        "quiebres_atendidas": sum(1 for r in reponer if r["quiebre"] and r["atendida"]),
        "capital_liberado_atendidas": float(sum(r["liberado"] for r in liberar if r["atendida"])),
        "capital_liberado_no_atendidas": float(sum(r["liberado"] for r in liberar if not r["atendida"])),
    }
