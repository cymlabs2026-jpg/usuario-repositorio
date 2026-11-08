"""Piso 4: memoria de decisiones.

Ciclo de vida de una decisión:
    pendiente -> aprobada | ajustada | descartada
    aprobada/ajustada -> ejecutada
    cualquiera abierta -> cerrada (el sistema la cierra cuando la condición desaparece)

Cada cambio queda en `acciones` con usuario y fecha; cada carga nueva mide el
resultado en pesos contra el saldo vencido que tenía el cliente al detectarla.
"""
from __future__ import annotations

from collections import Counter
from datetime import date

import pandas as pd
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from . import impacto, reglas
from .calidad import Informe
from .db import acciones, ahora, cargas, clientes, decisiones, facturas, linea_base, resultados

ABIERTAS = ("pendiente", "aprobada", "ajustada", "ejecutada")
TRANSICIONES = {
    "aprobar": ({"pendiente", "ajustada"}, "aprobada"),
    "ajustar": ({"pendiente", "aprobada"}, "ajustada"),
    "descartar": ({"pendiente", "aprobada", "ajustada"}, "descartada"),
    "ejecutar": ({"aprobada", "ajustada"}, "ejecutada"),
    "nota": (set(ABIERTAS) | {"descartada", "cerrada"}, None),
}
REQUIEREN_DETALLE = {"ajustar", "descartar", "ejecutar", "nota"}


# --- Carga de datos -----------------------------------------------------------
def registrar_carga(motor: Engine, validas: pd.DataFrame, informe: Informe, mapeo: dict,
                    fecha_corte: date, archivo: str, usuario: str) -> dict:
    """Guarda la carga, actualiza clientes, fija la línea base (si es la primera) y recalcula."""
    if not informe.puede_cargar:
        raise ValueError("El semáforo de calidad no permite cargar este archivo.")
    with motor.begin() as cx:
        carga_id = cx.execute(insert(cargas).values(
            fecha_corte=fecha_corte, archivo=archivo, usuario=usuario,
            filas_total=informe.filas_total, filas_validas=informe.filas_validas,
            semaforo=informe.semaforo, calidad=_json_seguro(informe.a_dict()), mapeo=mapeo,
        )).inserted_primary_key[0]
        filas = [{
            "carga_id": carga_id, "nit": r.nit, "numero": str(r.numero),
            "fecha_emision": r.fecha_emision, "fecha_vencimiento": r.fecha_vencimiento,
            "valor": None if pd.isna(r.valor) else float(r.valor), "saldo": float(r.saldo),
            "vendedor": r.vendedor if isinstance(r.vendedor, str) else None,
        } for r in validas.itertuples(index=False)]
        if filas:
            cx.execute(insert(facturas), filas)

        existentes = set(cx.execute(select(clientes.c.nit)).scalars())
        for nit, grupo in validas.groupby("nit"):
            nombre = Counter(grupo["cliente"].dropna()).most_common(1)
            cupo = grupo["cupo"].dropna()
            vend = grupo["vendedor"].dropna()
            datos = {"nombre": nombre[0][0] if nombre else nit,
                     "cupo": float(cupo.max()) if len(cupo) else None,
                     "vendedor": vend.iloc[-1] if len(vend) else None, "actualizado_en": ahora()}
            if nit in existentes:
                cx.execute(update(clientes).where(clientes.c.nit == nit).values(**datos))
            else:
                cx.execute(insert(clientes).values(nit=nit, **datos))

        primera = cx.execute(select(linea_base.c.id).limit(1)).first() is None
    if primera:
        with motor.begin() as cx:
            cx.execute(insert(linea_base).values(carga_id=carga_id,
                                                 metricas=impacto.metricas_carga(motor, carga_id)))
    resumen = recalcular(motor, carga_id)
    return {"carga_id": carga_id, "linea_base_creada": primera, **resumen}


def _json_seguro(obj):
    """Convierte tipos de numpy a tipos nativos para guardarlos como JSON."""
    if isinstance(obj, dict):
        return {k: _json_seguro(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_seguro(v) for v in obj]
    if hasattr(obj, "item"):
        return obj.item()
    return obj


# --- Motor de decisiones --------------------------------------------------------
def _carga_anterior(motor: Engine, carga_id: int) -> int | None:
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
        return cx.execute(select(cargas.c.id).where(
            (cargas.c.fecha_corte < corte) | ((cargas.c.fecha_corte == corte) & (cargas.c.id < carga_id)))
            .order_by(cargas.c.fecha_corte.desc(), cargas.c.id.desc()).limit(1)).scalar()


def recalcular(motor: Engine, carga_id: int) -> dict:
    """Aplica la regla activa a una carga y sincroniza la bandeja de decisiones."""
    regla = reglas.regla_activa(motor)
    p = regla["parametros"]
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
        info = {r.nit: {"nombre": r.nombre, "cupo": r.cupo} for r in cx.execute(select(clientes))}
    fact = impacto.facturas_de_carga(motor, carga_id)
    anterior = _carga_anterior(motor, carga_id)
    previo = None
    if anterior:
        fa = impacto.facturas_de_carga(motor, anterior)
        with motor.connect() as cx:
            corte_ant = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == anterior)).scalar_one()
        previo = dict(reglas.resumen_por_cliente(fa, corte_ant, p["dias_gracia"])
                      [["nit", "saldo_vencido"]].values)
    vencido_hoy = dict(reglas.resumen_por_cliente(fact, corte, p["dias_gracia"])[["nit", "saldo_vencido"]].values)
    detectadas = reglas.evaluar_cartera(fact, corte, p, info, previo, regla["version"])

    creadas = actualizadas = cerradas = 0
    with motor.begin() as cx:
        abiertas = {(d.nit, d.tipo): d for d in cx.execute(
            select(decisiones).where(decisiones.c.estado.in_(ABIERTAS),
                                     decisiones.c.paquete == reglas.PAQUETE)).mappings()}
        descartadas_aqui = {(d.nit, d.tipo) for d in cx.execute(
            select(decisiones.c.nit, decisiones.c.tipo).where(
                decisiones.c.estado == "descartada", decisiones.c.carga_actual_id == carga_id))}
        vistas = set()
        for r in detectadas:
            clave = (r.nit, r.tipo)
            vistas.add(clave)
            if clave in descartadas_aqui:
                continue
            valores = dict(prioridad=r.prioridad, recomendacion=r.recomendacion, motivo=r.motivo,
                           evidencia=_json_seguro(r.evidencia), regla_version_id=regla["id"],
                           carga_actual_id=carga_id, actualizada_en=ahora())
            if clave in abiertas:
                cx.execute(update(decisiones).where(decisiones.c.id == abiertas[clave]["id"]).values(**valores))
                actualizadas += 1
            else:
                cx.execute(insert(decisiones).values(
                    paquete=reglas.PAQUETE, tipo=r.tipo, nit=r.nit, estado="pendiente",
                    carga_origen_id=carga_id, saldo_vencido_inicial=r.saldo_vencido, **valores))
                creadas += 1
        for clave, d in abiertas.items():
            if clave not in vistas:
                actual = float(vencido_hoy.get(d["nit"], 0.0))
                cx.execute(update(decisiones).where(decisiones.c.id == d["id"]).values(
                    estado="cerrada", carga_actual_id=carga_id, actualizada_en=ahora()))
                cx.execute(insert(acciones).values(
                    decision_id=d["id"], tipo="sistema", usuario="ontara",
                    detalle=f"Cerrada automáticamente en el corte {corte.isoformat()}: ya no cumple la "
                            f"condición de la regla (saldo vencido actual ${actual:,.0f}).".replace(",", ".")))
                cerradas += 1

    medidas = _medir_resultados(motor, carga_id, vencido_hoy)
    return {"decisiones_nuevas": creadas, "decisiones_actualizadas": actualizadas,
            "decisiones_cerradas": cerradas, "resultados_medidos": medidas,
            "regla_version": regla["version"]}


def _medir_resultados(motor: Engine, carga_id: int, vencido_hoy: dict) -> int:
    """Para cada decisión de cobro detectada en un corte anterior, mide cuánto bajó su saldo vencido."""
    n = 0
    with motor.begin() as cx:
        objetivo = cx.execute(select(decisiones).where(
            decisiones.c.tipo == "cobrar", decisiones.c.carga_origen_id != carga_id,
            decisiones.c.estado != "descartada")).mappings().all()
        for d in objetivo:
            ya = cx.execute(select(resultados.c.id).where(resultados.c.decision_id == d["id"],
                                                          resultados.c.carga_id == carga_id)).first()
            if ya:
                continue
            despues = float(vencido_hoy.get(d["nit"], 0.0))
            atendida = cx.execute(select(acciones.c.id).where(
                acciones.c.decision_id == d["id"],
                acciones.c.tipo.in_(("aprobar", "ajustar", "ejecutar")))).first() is not None
            cx.execute(insert(resultados).values(
                decision_id=d["id"], carga_id=carga_id, saldo_vencido_antes=d["saldo_vencido_inicial"],
                saldo_vencido_despues=despues,
                pesos_recuperados=max(0.0, d["saldo_vencido_inicial"] - despues), atendida=atendida))
            n += 1
    return n


# --- Acciones del usuario -------------------------------------------------------
def registrar_accion(motor: Engine, decision_id: int, tipo: str, usuario: str,
                     detalle: str | None = None) -> str:
    """Aprobar, ajustar, descartar, ejecutar o dejar una nota. Devuelve el nuevo estado."""
    if tipo not in TRANSICIONES:
        raise ValueError(f"Acción desconocida: {tipo}")
    if tipo in REQUIEREN_DETALLE and not (detalle and detalle.strip()):
        raise ValueError("Esta acción necesita un comentario (qué se hizo, el ajuste o el motivo).")
    desde, hacia = TRANSICIONES[tipo]
    with motor.begin() as cx:
        estado = cx.execute(select(decisiones.c.estado).where(decisiones.c.id == decision_id)).scalar_one()
        if estado not in desde:
            raise ValueError(f"No se puede '{tipo}' una decisión en estado '{estado}'.")
        cx.execute(insert(acciones).values(decision_id=decision_id, tipo=tipo, usuario=usuario,
                                           detalle=(detalle or "").strip() or None))
        if hacia:
            cx.execute(update(decisiones).where(decisiones.c.id == decision_id)
                       .values(estado=hacia, actualizada_en=ahora()))
            return hacia
        return estado


# --- Consultas ------------------------------------------------------------------
def listar(motor: Engine, estados: tuple[str, ...] | None = ABIERTAS, tipo: str | None = None,
           limite: int = 200) -> list[dict]:
    q = (select(decisiones, clientes.c.nombre.label("cliente"))
         .join(clientes, clientes.c.nit == decisiones.c.nit, isouter=True)
         .order_by(decisiones.c.prioridad.desc()).limit(limite))
    if estados:
        q = q.where(decisiones.c.estado.in_(estados))
    if tipo:
        q = q.where(decisiones.c.tipo == tipo)
    with motor.connect() as cx:
        return [dict(r) for r in cx.execute(q).mappings()]


def detalle(motor: Engine, decision_id: int) -> dict | None:
    with motor.connect() as cx:
        d = cx.execute(select(decisiones, clientes.c.nombre.label("cliente"))
                       .join(clientes, clientes.c.nit == decisiones.c.nit, isouter=True)
                       .where(decisiones.c.id == decision_id)).mappings().first()
        if not d:
            return None
        acc = cx.execute(select(acciones).where(acciones.c.decision_id == decision_id)
                         .order_by(acciones.c.id)).mappings().all()
        res = cx.execute(select(resultados).where(resultados.c.decision_id == decision_id)
                         .order_by(resultados.c.id)).mappings().all()
    return {**dict(d), "acciones": [dict(a) for a in acc], "resultados": [dict(r) for r in res]}
