"""Impacto medido en pesos contra la línea base.

La línea base se toma con la primera carga, antes de que Ontara recomiende
nada. Sin línea base no hay forma de demostrar el valor del piloto.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from .db import acciones, cargas, decisiones, facturas, linea_base, resultados


def facturas_de_carga(motor: Engine, carga_id: int) -> pd.DataFrame:
    with motor.connect() as cx:
        filas = cx.execute(select(facturas).where(facturas.c.carga_id == carga_id)).mappings().all()
    cols = ["nit", "numero", "fecha_emision", "fecha_vencimiento", "valor", "saldo", "vendedor"]
    return pd.DataFrame([dict(f) for f in filas], columns=["id", "carga_id", *cols])


def metricas(fact: pd.DataFrame, corte: date) -> dict:
    """Indicadores de cartera. 'Vencida' = pasada su fecha de vencimiento, sin días de gracia,
    para que la definición no cambie cuando el cliente ajusta sus reglas."""
    f = fact[fact["saldo"] > 0]
    if f.empty:
        return {"cartera_total": 0.0, "cartera_vencida": 0.0, "pct_vencida": 0.0,
                "mora_promedio_dias": 0.0, "clientes_con_mora": 0, "clientes": 0}
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
    }


def metricas_carga(motor: Engine, carga_id: int) -> dict:
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
    return metricas(facturas_de_carga(motor, carga_id), corte)


def obtener_linea_base(motor: Engine) -> dict | None:
    with motor.connect() as cx:
        fila = cx.execute(select(linea_base).order_by(linea_base.c.id).limit(1)).mappings().first()
    return dict(fila) if fila else None


def historial(motor: Engine) -> pd.DataFrame:
    with motor.connect() as cx:
        filas = cx.execute(select(cargas.c.id, cargas.c.fecha_corte).order_by(cargas.c.fecha_corte)).all()
    datos = [{"carga_id": i, "fecha_corte": c, **metricas_carga(motor, i)} for i, c in filas]
    return pd.DataFrame(datos)


def resumen(motor: Engine) -> dict:
    """Lo que ve el gerente: base vs. hoy, pesos recuperados y uso de la bandeja."""
    base = obtener_linea_base(motor)
    with motor.connect() as cx:
        ultima = cx.execute(select(cargas.c.id).order_by(cargas.c.fecha_corte.desc(), cargas.c.id.desc())
                            .limit(1)).scalar()
        # último resultado medido por decisión
        sub = (select(resultados.c.decision_id, func.max(resultados.c.id).label("rid"))
               .group_by(resultados.c.decision_id).subquery())
        res = cx.execute(select(resultados).join(sub, resultados.c.id == sub.c.rid)).mappings().all()
        estados = dict(cx.execute(select(decisiones.c.estado, func.count())
                                  .group_by(decisiones.c.estado)).all())
        n_acciones = cx.execute(select(func.count()).select_from(acciones)
                                .where(acciones.c.tipo != "sistema")).scalar()
    actual = metricas_carga(motor, ultima) if ultima else None
    rec_atendidas = sum(r["pesos_recuperados"] for r in res if r["atendida"])
    rec_no = sum(r["pesos_recuperados"] for r in res if not r["atendida"])
    return {
        "linea_base": base["metricas"] if base else None,
        "actual": actual,
        "pesos_recuperados_atendidas": rec_atendidas,
        "pesos_recuperados_no_atendidas": rec_no,
        "decisiones_medidas": len(res),
        "decisiones_por_estado": estados,
        "acciones_registradas": n_acciones,
    }
