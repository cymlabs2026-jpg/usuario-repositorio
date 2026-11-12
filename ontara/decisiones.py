"""Piso 4: memoria de decisiones.

Ciclo de vida de una decisión:
    pendiente -> aprobada | ajustada | descartada
    aprobada/ajustada -> ejecutada
    cualquiera abierta -> cerrada (el sistema la cierra cuando la condición desaparece)

Cada cambio queda en `acciones` con usuario y fecha; cada carga nueva mide el
resultado en pesos contra el saldo vencido que tenía el cliente al detectarla.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date

import pandas as pd
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from . import comercial, impacto, paquetes, reglas
from .calidad import Informe
from .db import (acciones, ahora, cargas, clientes, cuentas_por_pagar, decisiones, existencias, facturas,
                 pedidos,
                 linea_base, lineas_venta, recaudos, resultados, saldos_caja, ventas)

ABIERTAS = ("pendiente", "aprobada", "ajustada", "ejecutada")
TRANSICIONES = {
    "aprobar": ({"pendiente", "ajustada"}, "aprobada"),
    "ajustar": ({"pendiente", "aprobada"}, "ajustada"),
    "descartar": ({"pendiente", "aprobada", "ajustada"}, "descartada"),
    "ejecutar": ({"aprobada", "ajustada"}, "ejecutada"),
    "nota": (set(ABIERTAS) | {"descartada", "cerrada"}, None),
}
REQUIEREN_DETALLE = {"ajustar", "descartar", "ejecutar", "nota"}
NECESITAN_NIVEL = {"aprobar", "ajustar", "descartar"}

# Motivos estructurados: convierten el "por qué" de cada descarte o ajuste en un dato que se puede contar.
# Con un motivo de la lista (distinto de "otro") el comentario es opcional.
MOTIVOS = {
    "descartar": {
        "ya_pago": "Ya pagó o hay un pago en camino",
        "acuerdo_vigente": "Hay un acuerdo de pago vigente",
        "cliente_estrategico": "Cliente estratégico o relación de la gerencia",
        "dato_incorrecto": "El dato del ERP está mal",
        "no_es_momento": "No es el momento (temporada, negociación en curso)",
        "no_aplica": "La regla no aplica a este caso",
        "otro": "Otro (explicar en el comentario)",
    },
    "ajustar": {
        "monto": "Otro monto",
        "plazo": "Otro plazo o fecha",
        "canal": "Otro canal o responsable",
        "condicion": "Otra condición",
        "otro": "Otro (explicar en el comentario)",
    },
}
# Cómo se gestionó la decisión al marcarla ejecutada.
CANALES = {"llamada": "Llamada", "whatsapp": "WhatsApp", "correo": "Correo", "visita": "Visita",
           "juridico": "Cobro jurídico", "otro": "Otro"}


# --- Carga de datos -----------------------------------------------------------
def registrar_carga(motor: Engine, validas: pd.DataFrame, informe: Informe, mapeo: dict,
                    fecha_corte: date, archivo: str, usuario: str, tipo: str = "cartera",
                    saldo_bancos: float | None = None) -> dict:
    """Guarda un reporte (cartera, ventas, líneas, cuentas por pagar, existencias, pedidos o recaudos), actualiza clientes, fija la
    línea base de cartera (si es la primera) y recalcula los paquetes que usan ese reporte.
    El reporte de cuentas por pagar exige el saldo en bancos a la fecha de corte."""
    if not informe.puede_cargar:
        raise ValueError("El semáforo de calidad no permite cargar este archivo.")
    if tipo not in ("cartera", "ventas", "lineas", "pagos", "inventario", "pedidos", "recaudos"):
        raise ValueError(f"Tipo de reporte desconocido: {tipo}")
    if tipo == "pagos" and saldo_bancos is None:
        raise ValueError("Para proyectar la caja falta el saldo en bancos a la fecha de corte.")
    with motor.begin() as cx:
        carga_id = cx.execute(insert(cargas).values(
            tipo=tipo, fecha_corte=fecha_corte, archivo=archivo, usuario=usuario,
            filas_total=informe.filas_total, filas_validas=informe.filas_validas,
            semaforo=informe.semaforo, calidad=_json_seguro(informe.a_dict()), mapeo=mapeo,
        )).inserted_primary_key[0]
        if tipo == "cartera":
            filas = [{
                "carga_id": carga_id, "nit": r.nit, "numero": str(r.numero),
                "fecha_emision": r.fecha_emision, "fecha_vencimiento": r.fecha_vencimiento,
                "valor": None if pd.isna(r.valor) else float(r.valor), "saldo": float(r.saldo),
                "vendedor": r.vendedor if isinstance(r.vendedor, str) else None,
            } for r in validas.itertuples(index=False)]
            if filas:
                cx.execute(insert(facturas), filas)
        elif tipo == "ventas":
            filas = [{
                "carga_id": carga_id, "nit": r.nit, "numero": str(r.numero), "fecha": r.fecha,
                "valor": float(r.valor), "vendedor": r.vendedor if isinstance(r.vendedor, str) else None,
            } for r in validas.itertuples(index=False)]
            if filas:
                cx.execute(insert(ventas), filas)
        elif tipo == "recaudos":
            filas = [{
                "carga_id": carga_id, "nit": r.nit, "factura": str(r.factura), "fecha_pago": r.fecha_pago,
                "valor": float(r.valor), "recibo": r.recibo if isinstance(r.recibo, str) else None,
            } for r in validas.itertuples(index=False)]
            if filas:
                cx.execute(insert(recaudos), filas)
        elif tipo == "inventario":
            filas = [{
                "carga_id": carga_id, "producto": str(r.producto).strip(),
                "bodega": r.bodega if isinstance(r.bodega, str) else None,
                "cantidad": float(r.cantidad), "costo_total": float(r.costo_total),
            } for r in validas.itertuples(index=False)]
            if filas:
                cx.execute(insert(existencias), filas)
        elif tipo == "pedidos":
            def opc(x):
                return None if x is None or (not isinstance(x, str) and pd.isna(x)) else x
            filas = [{
                "carga_id": carga_id, "nit": r.nit, "pedido": str(r.pedido),
                "producto": str(r.producto).strip() if isinstance(r.producto, str) else None,
                "cantidad": None if opc(r.cantidad) is None else float(r.cantidad), "valor": float(r.valor),
                "fecha_pedido": r.fecha_pedido, "fecha_compromiso": r.fecha_compromiso,
                "fecha_despacho": opc(r.fecha_despacho), "ciudad": r.ciudad if isinstance(r.ciudad, str) else None,
            } for r in validas.itertuples(index=False)]
            if filas:
                cx.execute(insert(pedidos), filas)
        elif tipo == "pagos":
            filas = [{
                "carga_id": carga_id, "nit": r.nit, "proveedor": str(r.proveedor or r.nit), "numero": str(r.numero),
                "fecha_emision": None if r.fecha_emision is None or pd.isna(r.fecha_emision) else r.fecha_emision,
                "fecha_vencimiento": r.fecha_vencimiento, "saldo": float(r.saldo),
                "categoria": r.categoria if isinstance(r.categoria, str) else None,
            } for r in validas.itertuples(index=False)]
            if filas:
                cx.execute(insert(cuentas_por_pagar), filas)
            cx.execute(insert(saldos_caja).values(carga_id=carga_id, saldo_bancos=float(saldo_bancos)))
        else:
            def num(x):
                return None if x is None or pd.isna(x) else float(x)
            filas = [{
                "carga_id": carga_id, "nit": r.nit, "numero": str(r.numero), "fecha": r.fecha,
                "producto": str(r.producto).strip(), "valor": float(r.valor), "costo": float(r.costo),
                "descuento": num(r.descuento), "cantidad": num(r.cantidad),
                "vendedor": r.vendedor if isinstance(r.vendedor, str) else None,
            } for r in validas.itertuples(index=False)]
            if filas:
                cx.execute(insert(lineas_venta), filas)

        existentes = set(cx.execute(select(clientes.c.nit)).scalars())
        # Los recaudos no crean ni actualizan clientes (se cruzan con los que ya existen) y los proveedores
        # del reporte de cuentas por pagar no son clientes; las existencias no traen clientes.
        sin_clientes = tipo in ("recaudos", "pagos", "inventario")
        for nit, grupo in ([] if sin_clientes else validas.groupby("nit")):
            nombre = Counter(grupo["cliente"].dropna()).most_common(1)
            vend = grupo["vendedor"].dropna()
            datos = {"nombre": nombre[0][0] if nombre else nit,
                     "vendedor": vend.iloc[-1] if len(vend) else None, "actualizado_en": ahora()}
            if tipo == "cartera":                    # el cupo solo viene en el reporte de cartera
                cupo = grupo["cupo"].dropna()
                datos["cupo"] = float(cupo.max()) if len(cupo) else None
            if nit in existentes:
                cx.execute(update(clientes).where(clientes.c.nit == nit).values(**datos))
            else:
                cx.execute(insert(clientes).values(nit=nit, **datos))

        primera = tipo == "cartera" and cx.execute(select(linea_base.c.id).limit(1)).first() is None
    if primera:
        with motor.begin() as cx:
            cx.execute(insert(linea_base).values(carga_id=carga_id,
                                                 metricas=impacto.metricas_carga(motor, carga_id)))
    resumen = recalcular(motor, carga_id)
    if tipo == "ventas":
        # Las ventas cambian quiénes son los clientes clave: se recalcula también el último corte de cartera.
        ult = ultima_carga(motor, "cartera")
        if ult and any(p.clave == "cartera" for p in paquetes.activos()):
            recalcular(motor, ult)
    if tipo in ("lineas", "pagos"):
        # Inventario usa la demanda de las ventas por producto y cruza con Margen y Caja.
        ult = ultima_carga(motor, "inventario")
        if ult and any(p.clave == "inventario" for p in paquetes.activos()):
            recalcular(motor, ult)
    if tipo in ("cartera", "ventas", "inventario"):
        # Despacho cruza con el crédito, el cupo, el valor del cliente y las existencias.
        ult = ultima_carga(motor, "pedidos")
        if ult and any(p.clave == "despacho" for p in paquetes.activos()):
            recalcular(motor, ult)
    if tipo != "pagos":
        # La caja proyecta con la última cartera, las ventas, el margen y los recaudos: se recalcula con cada
        # reporte nuevo.
        ult = ultima_carga(motor, "pagos")
        if ult and any(p.clave == "caja" for p in paquetes.activos()):
            recalcular(motor, ult)
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
def ultima_carga(motor: Engine, tipo: str) -> int | None:
    with motor.connect() as cx:
        return cx.execute(select(cargas.c.id).where(cargas.c.tipo == tipo)
                          .order_by(cargas.c.fecha_corte.desc(), cargas.c.id.desc()).limit(1)).scalar()


def _carga_anterior(motor: Engine, carga_id: int) -> int | None:
    """Corte anterior del mismo tipo de reporte."""
    with motor.connect() as cx:
        corte, tipo = cx.execute(select(cargas.c.fecha_corte, cargas.c.tipo)
                                 .where(cargas.c.id == carga_id)).one()
        return cx.execute(select(cargas.c.id).where(
            cargas.c.tipo == tipo,
            (cargas.c.fecha_corte < corte) | ((cargas.c.fecha_corte == corte) & (cargas.c.id < carga_id)))
            .order_by(cargas.c.fecha_corte.desc(), cargas.c.id.desc()).limit(1)).scalar()


def recalcular(motor: Engine, carga_id: int) -> dict:
    """Aplica la regla activa de cada paquete que usa este reporte y sincroniza la bandeja."""
    with motor.connect() as cx:
        tipo = cx.execute(select(cargas.c.tipo).where(cargas.c.id == carga_id)).scalar_one()
    total = {"decisiones_nuevas": 0, "decisiones_actualizadas": 0, "decisiones_cerradas": 0,
             "resultados_medidos": 0, "regla_version": None, "paquetes": []}
    for paq in paquetes.de_reporte(tipo):
        regla = reglas.regla_activa(motor, paq.clave)
        detectadas = paq.detectar(motor, carga_id, regla)
        r = sincronizar(motor, paq, carga_id, detectadas, regla)
        r["resultados_medidos"] = paq.medir(motor, carga_id)
        for k in ("decisiones_nuevas", "decisiones_actualizadas", "decisiones_cerradas", "resultados_medidos"):
            total[k] += r[k]
        total["paquetes"].append(paq.clave)
        if total["regla_version"] is None:
            total["regla_version"] = regla["version"]
    return total


def detectar_cartera(motor: Engine, carga_id: int, regla: dict) -> list[reglas.ResultadoRegla]:
    p = regla["parametros"]
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
        info = {r.nit: {"nombre": r.nombre, "cupo": r.cupo} for r in cx.execute(select(clientes))}
    fact = impacto.facturas_de_carga(motor, carga_id)
    # Valor del cliente: con el reporte de ventas si existe (incluye lo ya pagado); si no, con la cartera.
    compras = comercial.compras_desde_ventas(motor, corte, int(p["meses_valor_cliente"]))
    if compras is None:
        compras = reglas.compras_por_cliente(impacto.facturas_historicas(motor, corte), corte,
                                             int(p["meses_valor_cliente"]))
    anterior = _carga_anterior(motor, carga_id)
    previo = None
    if anterior:
        fa = impacto.facturas_de_carga(motor, anterior)
        with motor.connect() as cx:
            corte_ant = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == anterior)).scalar_one()
        previo = dict(reglas.resumen_por_cliente(fa, corte_ant, p["dias_gracia"])
                      [["nit", "saldo_vencido"]].values)
    return reglas.evaluar_cartera(fact, corte, p, info, previo, regla["version"], compras)


def sincronizar(motor: Engine, paq, carga_id: int, detectadas: list[reglas.ResultadoRegla],
                regla: dict) -> dict:
    """Crea, actualiza o cierra decisiones de un paquete según lo que detectaron sus reglas."""
    p = regla["parametros"]
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
    creadas = actualizadas = cerradas = 0
    tope_control = float(p.get("control_monto_maximo") or float("inf"))
    with motor.begin() as cx:
        abiertas = {(d.nit, d.tipo): d for d in cx.execute(
            select(decisiones).where(decisiones.c.estado.in_(ABIERTAS),
                                     decisiones.c.paquete == paq.clave)).mappings()}
        descartadas_aqui = {(d.nit, d.tipo) for d in cx.execute(
            select(decisiones.c.nit, decisiones.c.tipo).where(
                decisiones.c.paquete == paq.clave, decisiones.c.estado == "descartada",
                decisiones.c.carga_actual_id == carga_id))}
        vistas = set()
        for r in detectadas:
            clave = (r.nit, r.tipo)
            vistas.add(clave)
            if clave in descartadas_aqui:
                continue
            valores = dict(prioridad=r.prioridad, recomendacion=r.recomendacion, motivo=r.motivo,
                           evidencia=_json_seguro(r.evidencia), regla_version_id=regla["id"],
                           carga_actual_id=carga_id, nivel_aprobacion=r.nivel_aprobacion,
                           actualizada_en=ahora())
            if clave in abiertas:
                d = abiertas[clave]
                if d["grupo"] == "control" and r.saldo_vencido > tope_control:
                    # Seguridad: una deuda que creció por encima del tope sale del grupo de control.
                    valores["grupo"] = "fuera"
                    cx.execute(insert(acciones).values(
                        decision_id=d["id"], tipo="sistema", usuario="ontara",
                        detalle="Sale del grupo de control: el monto superó el tope y pasa a la bandeja."))
                cx.execute(update(decisiones).where(decisiones.c.id == d["id"]).values(**valores))
                actualizadas += 1
            else:
                cx.execute(insert(decisiones).values(
                    paquete=paq.clave, tipo=r.tipo, nit=r.nit, estado="pendiente",
                    carga_origen_id=carga_id, saldo_vencido_inicial=r.saldo_vencido,
                    grupo=asignar_grupo(r, carga_id, p, paq.tipos_control),
                    variables=variables_de(r), **valores))
                creadas += 1
        for clave, d in abiertas.items():
            if clave not in vistas:
                cx.execute(update(decisiones).where(decisiones.c.id == d["id"]).values(
                    estado="cerrada", carga_actual_id=carga_id, actualizada_en=ahora()))
                cx.execute(insert(acciones).values(
                    decision_id=d["id"], tipo="sistema", usuario="ontara",
                    detalle=f"Cerrada automáticamente en el corte {corte.isoformat()}: ya no cumple la "
                            f"condición de la regla."))
                cerradas += 1
    return {"decisiones_nuevas": creadas, "decisiones_actualizadas": actualizadas,
            "decisiones_cerradas": cerradas}


def variables_de(r: reglas.ResultadoRegla) -> dict:
    """Foto numérica de la decisión cuando se detectó: los valores simples de la evidencia (montos, días,
    porcentajes, banderas) y un nivel de los diccionarios anidados. Las listas (facturas, productos) y los
    textos se dejan fuera: son para leer, no para entrenar. No se actualiza en los cortes siguientes."""
    def simple(v):
        return isinstance(v, (bool, int, float)) and not (isinstance(v, float) and v != v)

    salida = {"prioridad": float(r.prioridad), "monto_en_juego": float(r.saldo_vencido),
              "requiere_gerente": r.nivel_aprobacion == "gerente"}
    for k, v in (_json_seguro(r.evidencia) or {}).items():
        if k == "regla_version":
            continue
        if simple(v):
            salida[k] = v
        elif isinstance(v, dict):
            salida.update({f"{k}_{k2}": v2 for k2, v2 in v.items() if simple(v2)})
    return salida


def asignar_grupo(r: reglas.ResultadoRegla, carga_id: int, p: dict,
                  tipos_control: frozenset[str] | set[str] = frozenset({"cobrar"})) -> str:
    """Grupo de control: una fracción de las decisiones elegibles no se muestra en la bandeja,
    para comparar el resultado con y sin Ontara. No son elegibles los montos por encima del tope ni,
    si el paquete lo pide, los clientes clave.

    La asignación es fija por cliente y tipo de decisión: depende del NIT, del tipo y del número de
    experimento (`experimento_control`, 1 por defecto), no del corte. Así un cliente que cae en control
    sigue en control en los cortes siguientes y nunca pasa a ser gestionado a mitad de la medición, lo
    que contaminaría la comparación. Cambiar `experimento_control` vuelve a sortear a todos (un
    experimento nuevo). `carga_id` se conserva en la firma por compatibilidad y no se usa."""
    pct = float(p.get("pct_grupo_control") or 0)
    if r.tipo not in tipos_control or pct <= 0:
        return "fuera"
    if r.saldo_vencido > float(p.get("control_monto_maximo") or float("inf")):
        return "fuera"
    if r.evidencia.get("cliente_clave") and r.tipo != "cobrar":
        return "fuera"
    experimento = p.get("experimento_control", 1)
    sorteo = int(hashlib.sha256(f"{r.nit}|{r.tipo}|{experimento}".encode()).hexdigest()[:8], 16) % 100
    return "control" if sorteo < pct else "tratamiento"


def medir_cartera(motor: Engine, carga_id: int) -> int:
    """Para cada decisión de cobro detectada en un corte anterior, mide cuánto bajó su saldo vencido."""
    p = reglas.regla_activa(motor)["parametros"]
    with motor.connect() as cx:
        corte = cx.execute(select(cargas.c.fecha_corte).where(cargas.c.id == carga_id)).scalar_one()
    vencido_hoy = dict(reglas.resumen_por_cliente(impacto.facturas_de_carga(motor, carga_id), corte,
                                                  p["dias_gracia"])[["nit", "saldo_vencido"]].values)
    n = 0
    with motor.begin() as cx:
        objetivo = cx.execute(select(decisiones).where(
            decisiones.c.paquete == reglas.PAQUETE, decisiones.c.tipo == "cobrar",
            decisiones.c.carga_origen_id != carga_id, decisiones.c.estado != "descartada")).mappings().all()
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
                     detalle: str | None = None, rol: str = "gerente", motivo: str | None = None,
                     canal: str | None = None, fecha_compromiso: date | None = None,
                     monto_compromiso: float | None = None) -> str:
    """Aprobar, ajustar, descartar, ejecutar o dejar una nota. Devuelve el nuevo estado.

    Matriz de delegación: aprobar, ajustar o descartar una decisión de nivel 'gerente'
    exige ese rol; el líder puede dejar notas y ejecutar lo que ya se aprobó.

    Para aprender: `motivo` (descartar, ajustar) sale de MOTIVOS; `canal` (ejecutar) de CANALES;
    `fecha_compromiso` y `monto_compromiso` guardan lo que el cliente prometió pagar."""
    if tipo not in TRANSICIONES:
        raise ValueError(f"Acción desconocida: {tipo}")
    motivo = motivo or None
    if motivo and motivo not in MOTIVOS.get(tipo, {}):
        raise ValueError(f"Motivo desconocido para '{tipo}': {motivo}")
    canal = canal or None
    if canal and canal not in CANALES:
        raise ValueError(f"Canal desconocido: {canal}")
    if monto_compromiso is not None and monto_compromiso < 0:
        raise ValueError("El monto prometido no puede ser negativo.")
    motivo_basta = (motivo is not None and motivo != "otro") or (tipo == "ejecutar" and canal not in (None, "otro"))
    if tipo in REQUIEREN_DETALLE and not (detalle and detalle.strip()) and not motivo_basta:
        raise ValueError("Esta acción necesita un comentario (qué se hizo, el ajuste o el motivo).")
    desde, hacia = TRANSICIONES[tipo]
    with motor.begin() as cx:
        fila = cx.execute(select(decisiones.c.estado, decisiones.c.nivel_aprobacion, decisiones.c.grupo)
                          .where(decisiones.c.id == decision_id)).one()
        estado = fila.estado
        if fila.grupo == "control":
            raise ValueError("Esta decisión está en el grupo de control y no se gestiona desde la bandeja.")
        if tipo in NECESITAN_NIVEL and not reglas.puede_aprobar(rol, fila.nivel_aprobacion):
            raise ValueError(f"Esta decisión la aprueba el {fila.nivel_aprobacion}: supera el monto que puede "
                             f"decidir el rol {rol}. Deja una nota para escalarla.")
        if estado not in desde:
            raise ValueError(f"No se puede '{tipo}' una decisión en estado '{estado}'.")
        cx.execute(insert(acciones).values(decision_id=decision_id, tipo=tipo, usuario=usuario,
                                           detalle=(detalle or "").strip() or None, motivo=motivo,
                                           canal=canal, fecha_compromiso=fecha_compromiso,
                                           monto_compromiso=monto_compromiso))
        if hacia:
            cx.execute(update(decisiones).where(decisiones.c.id == decision_id)
                       .values(estado=hacia, actualizada_en=ahora()))
            return hacia
        return estado


# --- Consultas ------------------------------------------------------------------
def listar(motor: Engine, estados: tuple[str, ...] | None = ABIERTAS, tipo: str | None = None,
           limite: int = 200, incluir_control: bool = False, nivel: str | None = None,
           paquete: str | None = None) -> list[dict]:
    """Decisiones por prioridad. Las del grupo de control no se muestran salvo que se pidan."""
    q = (select(decisiones, clientes.c.nombre.label("cliente"), clientes.c.vendedor.label("vendedor"))
         .join(clientes, clientes.c.nit == decisiones.c.nit, isouter=True)
         .order_by(decisiones.c.prioridad.desc()).limit(limite))
    if not incluir_control:
        q = q.where(decisiones.c.grupo != "control")
    if nivel:
        q = q.where(decisiones.c.nivel_aprobacion == nivel)
    if paquete:
        q = q.where(decisiones.c.paquete == paquete)
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
