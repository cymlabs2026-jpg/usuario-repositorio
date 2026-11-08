"""Paquete de decisión Foco comercial.

Pregunta que responde: ¿qué clientes están dejando de comprar y a cuáles deben visitar los
vendedores esta semana? Usa el reporte de ventas (todas las facturas emitidas, incluidas las
pagadas) y cruza con las decisiones abiertas de Cartera.

Todo es determinístico: mismos datos + misma versión de reglas = mismas decisiones.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine

from . import reglas
from .db import acciones, cargas, clientes, decisiones, resultados_comercial, ventas

PAQUETE = "comercial"
TIPOS = {"recuperar_cliente": "Recuperar cliente", "crecer_cliente": "Visita de crecimiento"}
ABIERTAS = ("pendiente", "aprobada", "ajustada", "ejecutada")


def _pesos(x: float) -> str:
    return "$" + f"{x:,.0f}".replace(",", ".")


# --- Datos --------------------------------------------------------------------------
def ventas_historicas(motor: Engine, hasta: date) -> pd.DataFrame:
    """Ventas de todos los reportes cargados hasta `hasta`. Cada factura cuenta una vez
    (si aparece en varios reportes, vale la del reporte más reciente)."""
    with motor.connect() as cx:
        filas = cx.execute(
            select(ventas, cargas.c.fecha_corte).join(cargas, cargas.c.id == ventas.c.carga_id)
            .where(cargas.c.fecha_corte <= hasta)).mappings().all()
    cols = ["nit", "numero", "fecha", "valor", "vendedor", "carga_id", "fecha_corte"]
    df = pd.DataFrame([dict(f) for f in filas], columns=[*cols, "id"])
    if df.empty:
        return df[cols]
    df = df.sort_values(["fecha_corte", "carga_id"]).drop_duplicates(["nit", "numero"], keep="last")
    return df[df["fecha"] <= hasta][cols].reset_index(drop=True)


def compras_desde_ventas(motor: Engine, corte: date, meses: int) -> dict[str, float] | None:
    """Compras por cliente según el reporte de ventas, o None si nunca se cargó uno."""
    hist = ventas_historicas(motor, corte)
    if hist.empty:
        return None
    h = hist.rename(columns={"fecha": "fecha_emision"}).assign(saldo=hist["valor"])
    return reglas.compras_por_cliente(h, corte, meses)


# --- Reglas ---------------------------------------------------------------------------
def perfil_clientes(hist: pd.DataFrame, corte: date, meses: int) -> pd.DataFrame:
    """Una fila por cliente: ventas del año, de 90 días, promedio trimestral previo, ritmo de compra."""
    desde = corte - timedelta(days=int(round(meses * 30.4)))
    h = hist[(hist["fecha"] > desde) & (hist["fecha"] <= corte)]
    filas = []
    corte_90 = corte - timedelta(days=90)
    trimestres_previos = max((corte_90 - desde).days / 91.25, 1e-9)
    for nit, g in h.groupby("nit"):
        fechas = sorted(set(g["fecha"]))
        intervalos = [(b - a).days for a, b in zip(fechas, fechas[1:])]
        v90 = float(g.loc[g["fecha"] > corte_90, "valor"].sum())
        previas = float(g.loc[g["fecha"] <= corte_90, "valor"].sum())
        ult = g.sort_values("fecha").iloc[-1]
        # Mayor intervalo sin compras dentro de los últimos 90 días (contando desde la compra anterior).
        recientes = [f for f in fechas if f > corte_90]
        previa = [f for f in fechas if f <= corte_90][-1:]
        puntos = previa + recientes
        hueco_90 = max([(b - a).days for a, b in zip(puntos, puntos[1:])], default=0)
        filas.append({
            "nit": nit, "ventas_12m": float(g["valor"].sum()), "ventas_90d": v90,
            "promedio_trimestral": previas / trimestres_previos, "compras": len(fechas),
            "frecuencia_dias": float(pd.Series(intervalos).median()) if intervalos else None,
            "ultima_compra": fechas[-1], "dias_sin_comprar": (corte - fechas[-1]).days,
            "vendedor": ult["vendedor"] if isinstance(ult["vendedor"], str) else None,
            "hueco_90d": hueco_90,
        })
    return pd.DataFrame(filas)


def _decisiones_cartera_abiertas(motor: Engine) -> dict[str, list[dict]]:
    """Decisiones abiertas de Cartera por NIT, para cruzarlas con las comerciales."""
    with motor.connect() as cx:
        filas = cx.execute(select(decisiones.c.id, decisiones.c.nit, decisiones.c.tipo,
                                  decisiones.c.recomendacion, decisiones.c.evidencia)
                           .where(decisiones.c.paquete == reglas.PAQUETE,
                                  decisiones.c.estado.in_(ABIERTAS),
                                  decisiones.c.grupo != "control")).mappings().all()
    salida: dict[str, list[dict]] = {}
    for f in filas:
        salida.setdefault(f["nit"], []).append(dict(f))
    return salida


def _cruces(nit: str, cartera: dict[str, list[dict]]) -> list[str]:
    textos = []
    for d in cartera.get(nit, []):
        ev = d["evidencia"] or {}
        if d["tipo"] == "congelar_credito":
            textos.append(f"Cartera recomienda frenarle el crédito (decisión #{d['id']}): coordinar con cartera "
                          f"antes de ofrecer nuevas condiciones.")
        elif d["tipo"] == "cobrar":
            textos.append(f"Tiene {_pesos(ev.get('saldo_vencido', 0))} vencidos (decisión de cobro #{d['id']}): "
                          f"puede ser la razón de la caída.")
        elif d["tipo"] == "ampliar_cupo":
            textos.append(f"Cartera recomienda ampliarle el cupo a {_pesos(ev.get('cupo_sugerido', 0))} "
                          f"(decisión #{d['id']}): llévelo como propuesta a la visita.")
    return textos


def evaluar_comercial(hist: pd.DataFrame, corte: date, parametros: dict, nombres: dict[str, str],
                      cartera: dict[str, list[dict]] | None = None,
                      regla_version: int | None = None) -> list[reglas.ResultadoRegla]:
    """Aplica las reglas de Foco comercial y devuelve decisiones, sin tocar la base."""
    p = {**reglas.parametros_base(PAQUETE), **parametros}
    cartera = cartera or {}
    if hist.empty:
        return []
    perfil = perfil_clientes(hist, corte, int(p["meses_historia"]))
    if perfil.empty:
        return []
    excepciones = {str(n) for n in p.get("clientes_excepcion", [])}
    total = float(perfil["ventas_12m"].clip(lower=0).sum())
    clave = reglas.clientes_clave(dict(zip(perfil["nit"], perfil["ventas_12m"].clip(lower=0))),
                                  float(p["pct_clientes_clave"]))
    max_v = max(float(perfil["ventas_12m"].max()), 1.0)
    salida: list[reglas.ResultadoRegla] = []

    for r in perfil.itertuples(index=False):
        if r.nit in excepciones or r.ventas_12m < p["ventas_minimas_12m"] or r.compras < p["compras_minimas"]:
            continue
        nombre = nombres.get(r.nit) or r.nit
        es_clave = r.nit in clave
        participacion = round(100 * r.ventas_12m / total, 1) if total else 0.0
        frec = r.frecuencia_dias or 0.0
        caida = (1 - r.ventas_90d / r.promedio_trimestral) if r.promedio_trimestral > 0 else 0.0
        cliente_h = hist[hist["nit"] == r.nit].sort_values("fecha", ascending=False)
        cruces = _cruces(r.nit, cartera)
        evidencia = {
            "cliente": nombre, "nit": r.nit, "fecha_corte": corte.isoformat(), "vendedor": r.vendedor,
            "ventas_12m": r.ventas_12m, "ventas_90d": r.ventas_90d,
            "promedio_trimestral": round(r.promedio_trimestral, 0), "caida_pct": round(100 * caida, 1),
            "compras_12m": int(r.compras), "frecuencia_dias": frec, "dias_sin_comprar": int(r.dias_sin_comprar),
            "ultima_compra": r.ultima_compra.isoformat(), "cliente_clave": es_clave,
            "participacion_ventas_pct": participacion, "regla_version": regla_version, "cruces_cartera": cruces,
            "ultimas_compras": [{"fecha": x.fecha.isoformat(), "numero": str(x.numero), "valor": float(x.valor)}
                                for x in cliente_h.head(10).itertuples(index=False)],
        }
        atrasado = frec > 0 and r.dias_sin_comprar > p["factor_atraso"] * frec \
            and r.dias_sin_comprar >= p["dias_minimos_sin_comprar"]
        # La caída cuenta si siguió comprando a su ritmo pero menos. Si la caída se explica por un periodo sin
        # comprar y ya volvió, el cliente se considera recuperado (la alerta se cierra).
        a_su_ritmo = not frec or r.hueco_90d <= p["factor_atraso"] * max(frec, 1)
        cayendo = 100 * caida >= p["caida_minima_pct"] and a_su_ritmo
        valor_n = r.ventas_12m / max_v
        txt_clave = (f" Es cliente clave: {participacion}% de las ventas del año." if es_clave else "")

        if atrasado or cayendo:
            razones = []
            if atrasado:
                razones.append(f"lleva {r.dias_sin_comprar} días sin comprar cuando suele comprar cada "
                               f"{frec:.0f} días")
            if cayendo:
                razones.append(f"sus ventas de los últimos 90 días ({_pesos(r.ventas_90d)}) cayeron "
                               f"{100 * caida:.0f}% frente a su trimestre promedio ({_pesos(r.promedio_trimestral)})")
            severidad = min(1.0, max((r.dias_sin_comprar / frec - 1) / 2 if frec else 0.0, caida, 0.0))
            prioridad = round(100 * (0.6 * valor_n + 0.4 * severidad), 1)
            accion = ("Visita esta semana del vendedor con el gerente comercial: entender por qué dejó de comprar "
                      "y llevar una propuesta." if es_clave else
                      "Llamada del vendedor esta semana para entender la caída y agendar el próximo pedido.")
            motivo = f"{nombre} " + " y ".join(razones) + "." + txt_clave
            salida.append(reglas.ResultadoRegla(
                r.nit, nombre, "recuperar_cliente", prioridad, accion, motivo,
                float(round(r.promedio_trimestral, 0)), evidencia))
        elif (r.promedio_trimestral > 0 and r.ventas_90d >= (1 + p["crecimiento_minimo_pct"] / 100)
              * r.promedio_trimestral and (not frec or r.dias_sin_comprar <= max(frec, 7))):
            crec = r.ventas_90d / r.promedio_trimestral - 1
            prioridad = round(100 * (0.6 * valor_n + 0.4 * min(1.0, crec)), 1)
            motivo = (f"{nombre} compró {_pesos(r.ventas_90d)} en los últimos 90 días, {100 * crec:.0f}% más que "
                      f"su trimestre promedio ({_pesos(r.promedio_trimestral)}).{txt_clave}")
            salida.append(reglas.ResultadoRegla(
                r.nit, nombre, "crecer_cliente", prioridad,
                "Visitar para consolidar el crecimiento: revisar portafolio, volumen y condiciones de crédito.",
                motivo, float(round(r.ventas_90d - r.promedio_trimestral, 0)), evidencia))
    return sorted(salida, key=lambda x: x.prioridad, reverse=True)


def detectar(motor: Engine, carga_id: int, regla: dict) -> list[reglas.ResultadoRegla]:
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
        nombres = {r.nit: r.nombre for r in cx.execute(select(clientes.c.nit, clientes.c.nombre))}
    return evaluar_comercial(ventas_historicas(motor, corte), corte, regla["parametros"], nombres,
                             _decisiones_cartera_abiertas(motor), regla["version"])


# --- Medición ---------------------------------------------------------------------------
def medir(motor: Engine, carga_id: int) -> int:
    """Para cada alerta de recuperar cliente de un reporte anterior, mide si volvió a comprar y cuánto."""
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
    hist = ventas_historicas(motor, corte)
    n = 0
    with motor.begin() as cx:
        objetivo = cx.execute(
            select(decisiones, cargas.c.fecha_corte.label("corte_origen"))
            .join(cargas, cargas.c.id == decisiones.c.carga_origen_id)
            .where(decisiones.c.paquete == PAQUETE, decisiones.c.tipo == "recuperar_cliente",
                   decisiones.c.carga_origen_id != carga_id, decisiones.c.estado != "descartada",
                   cargas.c.fecha_corte < corte)).mappings().all()
        for d in objetivo:
            if cx.execute(select(resultados_comercial.c.id).where(
                    resultados_comercial.c.decision_id == d["id"],
                    resultados_comercial.c.carga_id == carga_id)).first():
                continue
            despues = hist[(hist["nit"] == d["nit"]) & (hist["fecha"] > d["corte_origen"])]
            monto = float(despues["valor"].sum()) if not despues.empty else 0.0
            atendida = cx.execute(select(acciones.c.id).where(
                acciones.c.decision_id == d["id"],
                acciones.c.tipo.in_(("aprobar", "ajustar", "ejecutar")))).first() is not None
            cx.execute(insert(resultados_comercial).values(
                decision_id=d["id"], carga_id=carga_id, ventas_despues=monto, volvio_a_comprar=monto > 0,
                atendida=atendida))
            n += 1
    return n


def resumen(motor: Engine) -> dict:
    """Clientes alertados que volvieron a comprar y ventas recuperadas, con y sin la bandeja."""
    with motor.connect() as cx:
        sub = (select(resultados_comercial.c.decision_id, func.max(resultados_comercial.c.id).label("rid"))
               .group_by(resultados_comercial.c.decision_id).subquery())
        filas = cx.execute(
            select(decisiones.c.grupo, resultados_comercial.c.ventas_despues,
                   resultados_comercial.c.volvio_a_comprar, resultados_comercial.c.atendida)
            .join(sub, resultados_comercial.c.id == sub.c.rid)
            .join(decisiones, decisiones.c.id == resultados_comercial.c.decision_id)).all()
        estados = dict(cx.execute(select(decisiones.c.estado, func.count())
                                  .where(decisiones.c.paquete == PAQUETE)
                                  .group_by(decisiones.c.estado)).all())
        ultima = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.tipo == "ventas")
                            .order_by(cargas.c.fecha_corte.desc()).limit(1)).scalar()
    grupos = {k: {"n": 0, "volvieron": 0, "ventas": 0.0} for k in ("tratamiento", "control", "fuera")}
    atendidas = {"n": 0, "volvieron": 0, "ventas": 0.0}
    for grupo, monto, volvio, atendida in filas:
        g = grupos[grupo if grupo in grupos else "fuera"]
        g["n"] += 1
        g["volvieron"] += int(volvio)
        g["ventas"] += monto
        if atendida:
            atendidas["n"] += 1
            atendidas["volvieron"] += int(volvio)
            atendidas["ventas"] += monto
    t, c = grupos["tratamiento"], grupos["control"]
    efecto = None
    if t["n"] and c["n"]:
        efecto = round((t["ventas"] / t["n"] - c["ventas"] / c["n"]) * t["n"], 0)
    pct = lambda g: round(100 * g["volvieron"] / g["n"], 1) if g["n"] else None  # noqa: E731
    return {
        "ultimo_reporte": ultima, "decisiones_por_estado": estados,
        "alertas_medidas": sum(g["n"] for g in grupos.values()),
        "volvieron_atendidas": atendidas["volvieron"], "ventas_recuperadas_atendidas": atendidas["ventas"],
        "pct_volvio_tratamiento": pct(t), "pct_volvio_control": pct(c),
        "n_tratamiento": t["n"], "n_control": c["n"], "efecto_estimado": efecto,
        "muestra_suficiente": t["n"] >= 10 and c["n"] >= 10,
    }
