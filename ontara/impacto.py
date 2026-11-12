"""Impacto medido en pesos contra la línea base.

La línea base se toma con la primera carga, antes de que Ontara recomiende
nada. Sin línea base no hay forma de demostrar el valor del piloto.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from . import estadistica, reglas
from .db import acciones, cargas, decisiones, facturas, linea_base, resultados


def facturas_de_carga(motor: Engine, carga_id: int) -> pd.DataFrame:
    with motor.connect() as cx:
        filas = cx.execute(select(facturas).where(facturas.c.carga_id == carga_id)).mappings().all()
    cols = ["nit", "numero", "fecha_emision", "fecha_vencimiento", "valor", "saldo", "vendedor"]
    return pd.DataFrame([dict(f) for f in filas], columns=["id", "carga_id", *cols])


def facturas_historicas(motor: Engine, hasta: date) -> pd.DataFrame:
    """Todas las facturas de los cortes hasta `hasta` (pueden repetirse entre cortes)."""
    with motor.connect() as cx:
        filas = cx.execute(select(facturas).join(cargas, cargas.c.id == facturas.c.carga_id)
                           .where(cargas.c.fecha_corte <= hasta)).mappings().all()
    cols = ["id", "carga_id", "nit", "numero", "fecha_emision", "fecha_vencimiento", "valor", "saldo", "vendedor"]
    return pd.DataFrame([dict(f) for f in filas], columns=cols)


def ventas_diarias_estimadas(fact: pd.DataFrame, corte: date, dias: int = 30) -> float:
    """Ventas a crédito por día, estimadas con las facturas emitidas en los últimos `dias`.
    Con plazos de 30 días o más, casi todas siguen abiertas en el reporte de cartera."""
    if fact.empty:
        return 0.0
    rec = fact[(fact["fecha_emision"] > corte - timedelta(days=dias)) & (fact["fecha_emision"] <= corte)].copy()
    if rec.empty:
        return 0.0
    rec["monto"] = rec["valor"].where(rec["valor"].notna() & (rec["valor"] > 0), rec["saldo"])
    # Una factura puede aparecer en varios cortes: se cuenta una vez.
    return float(rec.groupby(["nit", "numero"])["monto"].max().sum()) / dias


def metricas(fact: pd.DataFrame, corte: date, historico: pd.DataFrame | None = None) -> dict:
    """Indicadores de cartera. 'Vencida' = pasada su fecha de vencimiento, sin días de gracia,
    para que la definición no cambie cuando el cliente ajusta sus reglas."""
    f = fact[fact["saldo"] > 0]
    # Con el histórico de cortes se recuperan facturas recientes que ya se pagaron y salieron del reporte.
    ventas_dia = ventas_diarias_estimadas(fact if historico is None else historico, corte)
    if f.empty:
        return {"cartera_total": 0.0, "cartera_vencida": 0.0, "pct_vencida": 0.0,
                "mora_promedio_dias": 0.0, "clientes_con_mora": 0, "clientes": 0,
                "ventas_diarias_estimadas": ventas_dia, "dias_cartera": 0.0}
    mora = pd.Series([(corte - v).days for v in f["fecha_vencimiento"]], index=f.index)
    venc = f[mora > 0]
    total, vencida = float(f["saldo"].sum()), float(venc["saldo"].sum())
    ponderada = float((mora[mora > 0] * venc["saldo"]).sum() / vencida) if vencida else 0.0
    return {
        "cartera_total": total,
        "cartera_vencida": vencida,
        "pct_vencida": round(100 * vencida / total, 1) if total else 0.0,
        "mora_promedio_dias": round(ponderada, 1),
        "clientes_con_mora": int(venc["nit"].nunique()),
        "clientes": int(f["nit"].nunique()),
        "ventas_diarias_estimadas": round(ventas_dia, 0),
        # Días de cartera (DSO): cuántos días de ventas están por cobrar.
        "dias_cartera": round(total / ventas_dia, 1) if ventas_dia else None,
    }


def metricas_carga(motor: Engine, carga_id: int) -> dict:
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
    return metricas(facturas_de_carga(motor, carga_id), corte, facturas_historicas(motor, corte))


def obtener_linea_base(motor: Engine) -> dict | None:
    with motor.connect() as cx:
        fila = cx.execute(select(linea_base).order_by(linea_base.c.id).limit(1)).mappings().first()
    return dict(fila) if fila else None


def historial(motor: Engine) -> pd.DataFrame:
    with motor.connect() as cx:
        filas = cx.execute(select(cargas.c.id, cargas.c.fecha_corte).where(cargas.c.tipo == "cartera")
                           .order_by(cargas.c.fecha_corte)).all()
    datos = [{"carga_id": i, "fecha_corte": c, **metricas_carga(motor, i)} for i, c in filas]
    return pd.DataFrame(datos)


def resumen(motor: Engine) -> dict:
    """Lo que ve el gerente: base vs. hoy, pesos recuperados y uso de la bandeja."""
    base = obtener_linea_base(motor)
    with motor.connect() as cx:
        ultima = cx.execute(select(cargas.c.id).where(cargas.c.tipo == "cartera")
                            .order_by(cargas.c.fecha_corte.desc(), cargas.c.id.desc()).limit(1)).scalar()
        # último resultado medido por decisión
        sub = (select(resultados.c.decision_id, func.max(resultados.c.id).label("rid"))
               .group_by(resultados.c.decision_id).subquery())
        res = cx.execute(select(resultados).join(sub, resultados.c.id == sub.c.rid)).mappings().all()
        estados = dict(cx.execute(select(decisiones.c.estado, func.count())
                                  .where(decisiones.c.paquete == "cartera")
                                  .group_by(decisiones.c.estado)).all())
        n_acciones = cx.execute(select(func.count()).select_from(acciones)
                                .where(acciones.c.tipo != "sistema")).scalar()
    actual = metricas_carga(motor, ultima) if ultima else None
    rec_atendidas = sum(r["pesos_recuperados"] for r in res if r["atendida"])
    rec_no = sum(r["pesos_recuperados"] for r in res if not r["atendida"])
    base_m = None
    if base:
        base_m = dict(base["metricas"])
        if "dias_cartera" not in base_m:        # línea base guardada antes de existir la métrica
            base_m = {**metricas_carga(motor, base["carga_id"]), **base_m}
    ventas_mes = float(reglas.regla_activa(motor)["parametros"].get("ventas_mensuales_cop") or 0)
    return {
        "caja": caja_liberada(base_m, actual, ventas_mes / 30.4 if ventas_mes else None),
        "control": comparar_control(motor),
        "linea_base": base_m,
        "actual": actual,
        "pesos_recuperados_atendidas": rec_atendidas,
        "pesos_recuperados_no_atendidas": rec_no,
        "decisiones_medidas": len(res),
        "decisiones_por_estado": estados,
        "acciones_registradas": n_acciones,
    }


def caja_liberada(base: dict | None, actual: dict | None, ventas_dia: float | None = None) -> dict:
    """Caja que dejó de estar atrapada en cartera: días de cartera ganados × ventas diarias.

    Si el gerente registró las ventas mensuales en las reglas, se usan para los dos cortes (más estable);
    si no, cada corte usa su propia estimación con las facturas recientes."""
    vacio = {"dias_base": None, "dias_hoy": None, "dias_ganados": None, "pesos": None, "fuente_ventas": None}
    if not base or not actual:
        return vacio
    if ventas_dia:
        dias_b, dias_h, v, fuente = (base["cartera_total"] / ventas_dia, actual["cartera_total"] / ventas_dia,
                                     ventas_dia, "contabilidad")
    else:
        dias_b, dias_h = base.get("dias_cartera"), actual.get("dias_cartera")
        v, fuente = actual.get("ventas_diarias_estimadas"), "estimada"
    if not dias_b or not dias_h or not v:
        return vacio
    ganados = round(dias_b - dias_h, 1)
    return {"dias_base": round(dias_b, 1), "dias_hoy": round(dias_h, 1), "dias_ganados": ganados,
            "pesos": round(ganados * v, 0), "fuente_ventas": fuente}


def comparar_control(motor: Engine) -> dict:
    """Recuperación con Ontara (tratamiento) frente al grupo de control, en decisiones de cobro elegibles.
    El efecto estimado es la recuperación adicional atribuible a la bandeja."""
    with motor.connect() as cx:
        sub = (select(resultados.c.decision_id, func.max(resultados.c.id).label("rid"))
               .group_by(resultados.c.decision_id).subquery())
        filas = cx.execute(
            select(decisiones.c.grupo, resultados.c.saldo_vencido_antes, resultados.c.pesos_recuperados)
            .join(sub, resultados.c.id == sub.c.rid)
            .join(decisiones, decisiones.c.id == resultados.c.decision_id)
            .where(decisiones.c.grupo.in_(("control", "tratamiento")))).all()
    casos = {"control": [], "tratamiento": []}
    for grupo, antes, rec in filas:
        casos[grupo].append((float(antes or 0), float(rec or 0)))
    inicial_t = sum(a for a, _ in casos["tratamiento"])

    def tasa(lista):
        inicial = sum(a for a, _ in lista)
        return sum(r for _, r in lista) / inicial if inicial else None

    def efecto(t, c):
        # Recuperación adicional en pesos: diferencia de tasas aplicada a la cartera tratada real.
        tt, tc = tasa(t), tasa(c)
        return None if tt is None or tc is None else (tt - tc) * inicial_t

    tasas = {k: tasa(v) for k, v in casos.items()}
    estimado = efecto(casos["tratamiento"], casos["control"])
    n_t, n_c = len(casos["tratamiento"]), len(casos["control"])
    return {
        "n_control": n_c, "n_tratamiento": n_t,
        "tasa_control_pct": None if tasas["control"] is None else round(100 * tasas["control"], 1),
        "tasa_tratamiento_pct": None if tasas["tratamiento"] is None else round(100 * tasas["tratamiento"], 1),
        "efecto_estimado": None if estimado is None else round(estimado, 0),
        # Rango del 90 % y si la muestra alcanza para afirmar el resultado; la app lo muestra.
        **estadistica.lectura(n_t, n_c, estadistica.rango_efecto(casos["tratamiento"], casos["control"], efecto)),
    }
