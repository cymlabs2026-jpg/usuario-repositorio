"""Paquete de decisión Despacho.

Pregunta que responde: ¿qué pedidos hay que sacar hoy para no incumplirle al cliente, qué los está frenando
(crédito, cupo o inventario) y a qué clientes ya les estamos fallando?
Usa el reporte de pedidos (cada línea con la fecha prometida y, si ya salió, la fecha de despacho) y cruza con
Crédito y cobro (crédito frenado, ampliación de cupo, saldo contra cupo), Inventario (existencias y
reposiciones abiertas) y Foco comercial (clientes que se están yendo).

Todo es determinístico: mismos datos + misma versión de reglas = mismas decisiones.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine

from . import comercial, impacto, reglas
from .db import acciones, cargas, clientes, decisiones, existencias, pedidos, resultados_despacho

PAQUETE = "despacho"
TIPOS = {"despachar_pedidos": "Despachar pedidos en riesgo", "recuperar_cumplimiento": "Recuperar cumplimiento"}
ABIERTAS = ("pendiente", "aprobada", "ajustada", "ejecutada")
COLUMNAS = ["nit", "pedido", "fecha_pedido", "fecha_compromiso", "fecha_despacho", "valor", "valor_pendiente",
            "productos", "ciudad"]


def _pesos(x: float) -> str:
    return ("-$" if x < 0 else "$") + f"{abs(x):,.0f}".replace(",", ".")


def _num(x: float) -> str:
    return f"{x:,.0f}".replace(",", ".")


# --- Datos --------------------------------------------------------------------------
def lineas_de_carga(motor: Engine, carga_id: int) -> pd.DataFrame:
    with motor.connect() as cx:
        filas = cx.execute(select(pedidos).where(pedidos.c.carga_id == carga_id)).mappings().all()
    return pd.DataFrame([dict(f) for f in filas],
                        columns=["id", "carga_id", "nit", "pedido", "producto", "cantidad", "valor", "fecha_pedido",
                                 "fecha_compromiso", "fecha_despacho", "ciudad"])


def pedidos_de_carga(motor: Engine, carga_id: int) -> pd.DataFrame:
    """Un pedido por fila. Está despachado cuando salieron todas sus líneas (fecha = la última que salió)."""
    lin = lineas_de_carga(motor, carga_id)
    if lin.empty:
        return pd.DataFrame(columns=COLUMNAS)
    salida = []
    for (nit, num), g in lin.groupby(["nit", "pedido"], sort=False):
        pendientes = g[g["fecha_despacho"].isna()]
        salida.append({
            "nit": nit, "pedido": num, "fecha_pedido": min(g["fecha_pedido"]),
            "fecha_compromiso": min(g["fecha_compromiso"]),
            "fecha_despacho": None if len(pendientes) else max(g["fecha_despacho"]),
            "valor": float(g["valor"].sum()), "valor_pendiente": float(pendientes["valor"].sum()),
            "productos": [{"producto": p, "cantidad": None if c != c else float(c), "valor": float(v)}
                          for p, c, v in zip(pendientes["producto"], pendientes["cantidad"], pendientes["valor"])
                          if isinstance(p, str)],
            "ciudad": next((c for c in g["ciudad"] if isinstance(c, str)), None),
        })
    return pd.DataFrame(salida, columns=COLUMNAS)


def cumplimiento(ped: pd.DataFrame, desde: date, hasta: date, tolerancia: int) -> pd.DataFrame:
    """% de pedidos despachados a tiempo por cliente entre `desde` (excluido) y `hasta`."""
    d = ped[ped["fecha_despacho"].notna()].copy()
    d = d[(d["fecha_despacho"] > desde) & (d["fecha_despacho"] <= hasta)]
    if d.empty:
        return pd.DataFrame(columns=["nit", "despachados", "a_tiempo", "pct", "dias_atraso_prom", "valor"])
    d["atraso"] = [(a - b).days for a, b in zip(d["fecha_despacho"], d["fecha_compromiso"])]
    d["a_tiempo"] = d["atraso"] <= tolerancia
    g = d.groupby("nit").agg(despachados=("pedido", "count"), a_tiempo=("a_tiempo", "sum"),
                             valor=("valor", "sum")).reset_index()
    tarde = d[~d["a_tiempo"]].groupby("nit")["atraso"].mean()
    g["pct"] = 100 * g["a_tiempo"] / g["despachados"]
    g["dias_atraso_prom"] = g["nit"].map(tarde).fillna(0.0)
    return g


def _cartera_actual(motor: Engine) -> dict[str, float]:
    with motor.connect() as cx:
        ult = cx.execute(select(cargas.c.id).where(cargas.c.tipo == "cartera")
                         .order_by(cargas.c.fecha_corte.desc(), cargas.c.id.desc()).limit(1)).scalar()
    if not ult:
        return {}
    f = impacto.facturas_de_carga(motor, ult)
    return f.groupby("nit")["saldo"].sum().to_dict() if len(f) else {}


def _existencias_actuales(motor: Engine) -> dict[str, float] | None:
    with motor.connect() as cx:
        ult = cx.execute(select(cargas.c.id).where(cargas.c.tipo == "inventario")
                         .order_by(cargas.c.fecha_corte.desc(), cargas.c.id.desc()).limit(1)).scalar()
        if not ult:
            return None
        filas = cx.execute(select(existencias.c.producto, func.sum(existencias.c.cantidad))
                           .where(existencias.c.carga_id == ult).group_by(existencias.c.producto)).all()
    return {p: float(c) for p, c in filas}


def _otras_abiertas(motor: Engine) -> list[dict]:
    with motor.connect() as cx:
        return [dict(f) for f in cx.execute(
            select(decisiones.c.id, decisiones.c.nit, decisiones.c.tipo, decisiones.c.evidencia)
            .where(decisiones.c.paquete.in_(("cartera", "comercial", "inventario")),
                   decisiones.c.estado.in_(ABIERTAS))).mappings()]


# --- Reglas ---------------------------------------------------------------------------
def evaluar_despacho(ped: pd.DataFrame, corte: date, parametros: dict, info: dict | None = None,
                     otras: list[dict] | None = None, saldos: dict | None = None,
                     stock: dict | None = None, compras: dict | None = None,
                     regla_version: int | None = None) -> list[reglas.ResultadoRegla]:
    """Aplica las reglas de Despacho y devuelve decisiones, sin tocar la base.

    info: nit -> {"nombre", "cupo"}; otras: decisiones abiertas de Cartera, Comercial e Inventario;
    saldos: saldo en cartera por cliente; stock: existencias por producto (None = no hay reporte);
    compras: ventas por cliente en 12 meses (para el valor y los clientes clave)."""
    p = {**reglas.parametros_base(PAQUETE), **parametros}
    info, otras, saldos, compras = info or {}, otras or [], saldos or {}, compras or {}
    if ped.empty:
        return []
    alerta, tol = int(p["dias_alerta"]), int(p["dias_tolerancia"])
    minimo, limite = float(p["valor_minimo_cop"]), float(p["aprobacion_lider_max_cop"] or 0)
    excluidos = {str(x).strip() for x in p.get("clientes_excepcion", [])}
    clave = reglas.clientes_clave(compras, float(p["pct_clientes_clave"])) if compras else set()
    por_nit: dict[str, dict[str, dict]] = {}
    for d in otras:
        por_nit.setdefault(d["nit"], {})[d["tipo"]] = d
    reposiciones = {(d["evidencia"] or {}).get("producto"): d["id"] for d in otras if d["tipo"] == "reponer_producto"}
    base_ev = {"fecha_corte": corte.isoformat(), "regla_version": regla_version, "dias_alerta": alerta,
               "dias_tolerancia": tol}

    # Lo que ya está comprometido con pedidos pendientes, para saber si alcanza el inventario.
    pendientes = ped[ped["fecha_despacho"].isna() & (ped["valor_pendiente"] > 0)]
    comprometido: dict[str, float] = {}
    for prods in pendientes.sort_values("fecha_compromiso")["productos"]:
        for x in prods:
            comprometido[x["producto"]] = comprometido.get(x["producto"], 0.0) + (x["cantidad"] or 0.0)

    candidatas: list[tuple] = []          # (tipo, nit, monto, severidad, recomendacion, motivo, evidencia, nivel)

    # 1. Pedidos pendientes que ya pasaron (o están por pasar) la fecha prometida.
    en_alerta = pendientes[pendientes["fecha_compromiso"] <= corte + timedelta(days=alerta)]
    for nit, g in en_alerta.groupby("nit"):
        if nit in excluidos:
            continue
        valor = float(g["valor_pendiente"].sum())
        if valor < minimo:
            continue
        nombre = info.get(nit, {}).get("nombre") or nit
        g = g.sort_values("fecha_compromiso")
        lista = [{"pedido": r.pedido, "fecha_pedido": r.fecha_pedido.isoformat(),
                  "fecha_compromiso": r.fecha_compromiso.isoformat(), "dias_atraso": (corte - r.fecha_compromiso).days,
                  "valor": round(r.valor_pendiente, 0), "ciudad": r.ciudad,
                  "productos": ", ".join(x["producto"] for x in r.productos)} for r in g.itertuples(index=False)]
        atraso_max = max(x["dias_atraso"] for x in lista)
        es_clave = nit in clave
        ev = {**base_ev, "nit": nit, "cliente": nombre, "pedidos": lista, "valor_pendiente": round(valor, 0),
              "dias_atraso_max": atraso_max, "cliente_clave": es_clave, "cruces": [], "faltantes": [],
              "bloqueo": None}
        mias = por_nit.get(nit, {})
        cupo = info.get(nit, {}).get("cupo")
        saldo = float(saldos.get(nit, 0.0))
        ev.update(saldo_cartera=round(saldo, 0), cupo=cupo)
        nums = ", ".join(x["pedido"] for x in lista)
        nivel = "gerente" if limite and valor > limite else "lider"

        faltan = []
        if stock is not None:
            for r in g.itertuples(index=False):
                for x in r.productos:
                    if x["cantidad"] and comprometido.get(x["producto"], 0.0) > stock.get(x["producto"], 0.0):
                        faltan.append({"pedido": r.pedido, "producto": x["producto"], "cantidad": x["cantidad"],
                                       "disponible": stock.get(x["producto"], 0.0),
                                       "comprometido": comprometido[x["producto"]],
                                       "decision_inventario": reposiciones.get(x["producto"])})
        ev["faltantes"] = faltan

        if "congelar_credito" in mias:
            ref = mias["congelar_credito"]["id"]
            ev["bloqueo"] = "credito"
            accion = (f"No despachar a {nombre} hasta que Crédito y cobro cierre un acuerdo de pago o un anticipo "
                      f"(decisión #{ref}); avisarle hoy la nueva fecha de los pedidos {nums}.")
            ev["cruces"].append(f"Crédito y cobro pide frenar el crédito a este cliente (decisión #{ref}).")
            if es_clave:
                nivel = "gerente"           # retener a un cliente clave lo decide el gerente
        elif cupo and saldo + valor > float(cupo):
            ev["bloqueo"] = "cupo"
            exceso = saldo + valor - float(cupo)
            if "ampliar_cupo" in mias:
                ref = mias["ampliar_cupo"]["id"]
                accion = (f"Aprobar la ampliación de cupo de {nombre} (decisión #{ref}) y despachar hoy los pedidos "
                          f"{nums} ({_pesos(valor)}).")
                ev["cruces"].append(f"Crédito y cobro ya recomienda ampliarle el cupo (decisión #{ref}): es buen "
                                    f"pagador.")
            else:
                accion = (f"Los pedidos {nums} de {nombre} pasan el cupo por {_pesos(exceso)}: pedir anticipo o "
                          f"aprobación de Crédito y cobro y despachar.")
        elif faltan:
            ev["bloqueo"] = "inventario"
            prods = sorted({f["producto"] for f in faltan})
            refs = sorted({f["decision_inventario"] for f in faltan if f["decision_inventario"]})
            accion = (f"Despachar hoy lo disponible de los pedidos {nums} y programar {', '.join(prods)} cuando "
                      f"llegue la reposición; avisar al cliente" + (" (cliente clave)." if es_clave else "."))
            ev["cruces"].append(f"No alcanza el inventario de {', '.join(prods)} para los pedidos pendientes"
                                + (f" (Inventario, decisión {', '.join('#' + str(r) for r in refs)})." if refs else "."))
        else:
            accion = (f"Despachar hoy los pedidos {nums} de {nombre} ({_pesos(valor)})"
                      + (": cliente clave, primero en la ruta." if es_clave else "."))
        if "recuperar_cliente" in mias:
            ev["cruces"].append(f"Foco comercial lo marca como cliente que está comprando menos (decisión "
                                f"#{mias['recuperar_cliente']['id']}): no fallarle en la entrega.")
        cuantos = "1 pedido" if len(lista) == 1 else f"{len(lista)} pedidos"
        if atraso_max > 0:
            estado = (f"{cuantos} por {_pesos(valor)}; " + ("lleva" if len(lista) == 1 else "el más atrasado lleva")
                      + f" {atraso_max} día{'s' if atraso_max > 1 else ''} después de la fecha prometida")
        else:
            cuando = {0: "hoy", -1: "mañana"}.get(atraso_max, f"en {-atraso_max} días")
            estado = f"{cuantos} por {_pesos(valor)} {'vence' if len(lista) == 1 else 'vencen'} {cuando} su fecha prometida"
        motivo = estado + "." + {
            "credito": " El cliente tiene el crédito frenado.",
            "cupo": f" Con su saldo en cartera ({_pesos(saldo)}) pasa el cupo de {_pesos(float(cupo or 0))}.",
            "inventario": " No hay inventario suficiente para todo.", None: ""}[ev["bloqueo"]]
        severidad = 0.3 if atraso_max <= 0 else min(1.0, 0.4 + atraso_max / 10)
        candidatas.append(("despachar_pedidos", nit, valor, severidad, accion, motivo, ev, nivel, es_clave))

    # 2. Clientes a los que se les está incumpliendo.
    ventana = int(p["dias_cumplimiento"])
    cump = cumplimiento(ped, corte - timedelta(days=ventana), corte, tol)
    for r in cump.itertuples(index=False):
        if r.nit in excluidos or r.despachados < int(p["pedidos_minimos"]) or r.pct >= float(p["cumplimiento_minimo_pct"]):
            continue
        nombre = info.get(r.nit, {}).get("nombre") or r.nit
        ventas_12m = float(compras.get(r.nit, 0.0)) or float(r.valor) * 365 / ventana
        if ventas_12m < minimo:
            continue
        hechos = ped[(ped["nit"] == r.nit) & ped["fecha_despacho"].notna()]
        hechos = hechos[hechos["fecha_despacho"] > corte - timedelta(days=ventana)]
        tarde = [{"pedido": x.pedido, "fecha_compromiso": x.fecha_compromiso.isoformat(),
                  "fecha_despacho": x.fecha_despacho.isoformat(), "dias_atraso": (x.fecha_despacho - x.fecha_compromiso).days,
                  "valor": round(x.valor, 0)}
                 for x in hechos.sort_values("fecha_despacho", ascending=False).itertuples(index=False)
                 if (x.fecha_despacho - x.fecha_compromiso).days > tol]
        es_clave = r.nit in clave
        ev = {**base_ev, "nit": r.nit, "cliente": nombre, "dias_cumplimiento": ventana,
              "pedidos_despachados": int(r.despachados), "a_tiempo": int(r.a_tiempo),
              "cumplimiento_pct": round(float(r.pct), 1), "cumplimiento_minimo_pct": float(p["cumplimiento_minimo_pct"]),
              "dias_atraso_prom": round(float(r.dias_atraso_prom), 1), "ventas_12m": round(ventas_12m, 0),
              "cliente_clave": es_clave, "tarde": tarde[:8], "cruces": []}
        mias = por_nit.get(r.nit, {})
        if "recuperar_cliente" in mias:
            ev["cruces"].append(f"Foco comercial ya lo marca como cliente que está comprando menos (decisión "
                                f"#{mias['recuperar_cliente']['id']}): los incumplimientos pueden ser la causa.")
        n_tarde = int(r.despachados - r.a_tiempo)
        accion = (f"Llamar a {nombre} con el gerente comercial: reconocer "
                  + ("el pedido entregado tarde" if n_tarde == 1 else f"los {n_tarde} pedidos entregados tarde") + ", "
                  f"fijarle un día de entrega y poner sus próximos pedidos primero en la ruta.")
        motivo = (f"En {ventana} días se le despacharon {int(r.despachados)} pedidos y solo {int(r.a_tiempo)} "
                  f"salieron a tiempo ({r.pct:.0f}%, mínimo {float(p['cumplimiento_minimo_pct']):.0f}%). Los tardíos "
                  f"salieron {r.dias_atraso_prom:.1f} días después de lo prometido. Compra {_pesos(ventas_12m)} al año.")
        severidad = min(1.0, (float(p["cumplimiento_minimo_pct"]) - r.pct) / float(p["cumplimiento_minimo_pct"]) * 2)
        nivel = "gerente" if es_clave or (limite and ventas_12m > 10 * limite) else "lider"
        candidatas.append(("recuperar_cumplimiento", r.nit, ventas_12m, severidad, accion, motivo, ev, nivel, es_clave))

    if not candidatas:
        return []
    maximos = {t: max(c[2] for c in candidatas if c[0] == t) or 1.0 for t in {c[0] for c in candidatas}}
    salida = []
    for tipo, nit, monto, sev, accion, motivo, ev, nivel, es_clave in candidatas:
        prioridad = round(100 * (0.4 * monto / maximos[tipo] + 0.4 * sev + 0.2 * es_clave), 1)
        salida.append(reglas.ResultadoRegla(nit, ev["cliente"], tipo, prioridad, accion, motivo,
                                            float(round(monto, 0)), ev, nivel))
    return sorted(salida, key=lambda x: x.prioridad, reverse=True)


def detectar(motor: Engine, carga_id: int, regla: dict) -> list[reglas.ResultadoRegla]:
    p = {**reglas.parametros_base(PAQUETE), **regla["parametros"]}
    _medir(motor, carga_id, p)            # antes de que la sincronización actualice la evidencia
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
        info = {r.nit: {"nombre": r.nombre, "cupo": r.cupo} for r in cx.execute(select(clientes))}
    compras = comercial.compras_desde_ventas(motor, corte, 12)
    return evaluar_despacho(pedidos_de_carga(motor, carga_id), corte, p, info, _otras_abiertas(motor),
                            _cartera_actual(motor), _existencias_actuales(motor), compras, regla["version"])


# --- Medición ---------------------------------------------------------------------------
def medir(motor: Engine, carga_id: int) -> int:
    """La medición de Despacho ya corrió dentro de `detectar` (antes de actualizar la evidencia)."""
    with motor.connect() as cx:
        return cx.execute(select(func.count()).select_from(resultados_despacho)
                          .where(resultados_despacho.c.carga_id == carga_id)).scalar()


def _medir(motor: Engine, carga_id: int, p: dict) -> int:
    """Con el siguiente reporte de pedidos: si los pedidos en alerta salieron y con cuántos días de atraso, y si el
    cumplimiento de los clientes alertados mejoró después de la decisión."""
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
    tol = int(p["dias_tolerancia"])
    hoy = pedidos_de_carga(motor, carga_id)
    por_pedido = {(r.nit, r.pedido): r for r in hoy.itertuples(index=False)}
    n = 0
    with motor.begin() as cx:
        objetivo = cx.execute(
            select(decisiones, cargas.c.fecha_corte.label("corte_origen"))
            .join(cargas, cargas.c.id == decisiones.c.carga_origen_id)
            .where(decisiones.c.paquete == PAQUETE, decisiones.c.carga_origen_id != carga_id,
                   decisiones.c.estado != "descartada", cargas.c.fecha_corte < corte)).mappings().all()
        for d in objetivo:
            if cx.execute(select(resultados_despacho.c.id).where(
                    resultados_despacho.c.decision_id == d["id"],
                    resultados_despacho.c.carga_id == carga_id)).first():
                continue
            ev = d["evidencia"]
            antes, despues = None, None
            if d["tipo"] == "despachar_pedidos":
                lista = ev["pedidos"]
                salieron = [(x, por_pedido[(d["nit"], x["pedido"])]) for x in lista
                            if (d["nit"], x["pedido"]) in por_pedido
                            and por_pedido[(d["nit"], x["pedido"])].fecha_despacho is not None]
                total, despachados = len(lista), len(salieron)
                valor_desp = sum(float(x["valor"]) for x, _ in salieron)
                atrasos = [max(0, (r.fecha_despacho - date.fromisoformat(x["fecha_compromiso"])).days)
                           for x, r in salieron]
            else:
                c = cumplimiento(hoy[hoy["nit"] == d["nit"]], d["corte_origen"], corte, tol)
                antes = float(ev["cumplimiento_pct"])
                total = int(c["despachados"].sum()) if len(c) else 0
                despachados = int(c["a_tiempo"].sum()) if len(c) else 0
                despues = float(c["pct"].iloc[0]) if len(c) else None
                valor_desp = float(c["valor"].sum()) if len(c) else 0.0
                atrasos = [float(c["dias_atraso_prom"].iloc[0])] if len(c) else []
            atendida = cx.execute(select(acciones.c.id).where(
                acciones.c.decision_id == d["id"],
                acciones.c.tipo.in_(("aprobar", "ajustar", "ejecutar")))).first() is not None
            cx.execute(insert(resultados_despacho).values(
                decision_id=d["id"], carga_id=carga_id, pedidos=total, despachados=despachados,
                valor_antes=float(d["saldo_vencido_inicial"]), valor_despachado=round(valor_desp, 0),
                dias_atraso=round(sum(atrasos) / len(atrasos), 1) if atrasos else None,
                cumplimiento_antes=antes, cumplimiento_despues=None if despues is None else round(despues, 1),
                atendida=atendida))
            n += 1
    return n


def metricas(motor: Engine, carga_id: int, p: dict) -> dict:
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
    ped = pedidos_de_carga(motor, carga_id)
    tol = int(p["dias_tolerancia"])
    c = cumplimiento(ped, corte - timedelta(days=30), corte, tol)
    pend = ped[ped["fecha_despacho"].isna()]
    atrasados = pend[pend["fecha_compromiso"] < corte]
    hechos = ped[ped["fecha_despacho"].notna()]
    hechos = hechos[hechos["fecha_despacho"] > corte - timedelta(days=30)]
    lead = [(a - b).days for a, b in zip(hechos["fecha_despacho"], hechos["fecha_pedido"])]
    desp = int(c["despachados"].sum()) if len(c) else 0
    return {"fecha": corte, "despachados_30d": desp,
            "pct_a_tiempo": round(100 * float(c["a_tiempo"].sum()) / desp, 1) if desp else None,
            "pendientes": int(len(pend)), "valor_pendiente": float(pend["valor_pendiente"].sum()),
            "atrasados": int(len(atrasados)), "valor_atrasado": float(atrasados["valor_pendiente"].sum()),
            "dias_entrega": round(sum(lead) / len(lead), 1) if lead else None}


def resumen(motor: Engine) -> dict:
    with motor.connect() as cx:
        cortes = cx.execute(select(cargas.c.id).where(cargas.c.tipo == "pedidos")
                            .order_by(cargas.c.fecha_corte, cargas.c.id)).scalars().all()
        sub = (select(resultados_despacho.c.decision_id, func.max(resultados_despacho.c.id).label("rid"))
               .group_by(resultados_despacho.c.decision_id).subquery())
        res = cx.execute(select(resultados_despacho, decisiones.c.tipo)
                         .join(sub, resultados_despacho.c.id == sub.c.rid)
                         .join(decisiones, decisiones.c.id == resultados_despacho.c.decision_id)).mappings().all()
        estados = dict(cx.execute(select(decisiones.c.estado, func.count()).where(decisiones.c.paquete == PAQUETE)
                                  .group_by(decisiones.c.estado)).all())
    if not cortes:
        return {"ultimo_reporte": None}
    p = reglas.regla_activa(motor, PAQUETE)["parametros"]
    base, hoy = metricas(motor, cortes[0], p), metricas(motor, cortes[-1], p)
    desp = [r for r in res if r["tipo"] == "despachar_pedidos"]
    cump = [r for r in res if r["tipo"] == "recuperar_cumplimiento" and r["cumplimiento_despues"] is not None]
    return {
        "ultimo_reporte": hoy["fecha"], "base": base, "hoy": hoy, "decisiones_por_estado": estados,
        "pedidos_medidos": sum(r["pedidos"] for r in desp), "pedidos_despachados": sum(r["despachados"] for r in desp),
        "valor_despachado_atendidas": float(sum(r["valor_despachado"] for r in desp if r["atendida"])),
        "valor_despachado_no_atendidas": float(sum(r["valor_despachado"] for r in desp if not r["atendida"])),
        "clientes_medidos": len(cump),
        "clientes_mejoraron": sum(1 for r in cump if r["cumplimiento_despues"] > r["cumplimiento_antes"]),
    }
