"""Etapa 0 del aprendizaje autónomo: instrumentar.

Ontara todavía no ajusta reglas ni entrena modelos por su cuenta. Este módulo junta lo que hace falta
para que eso sea posible después, sin perder historia:

- `dataset`: una fila por decisión con la foto de variables al detectarla, lo que hizo la persona
  (motivo, canal, compromiso) y el resultado a 30, 60 y 90 días. Es la tabla con la que se calibran
  umbrales (etapa 1) y se entrenan modelos de pago y abandono (etapa 2).
- `recaudos_por_factura`: la fecha real de pago de cada factura, desde el reporte de recaudos.
- `preparacion`: cuánto falta para tener datos suficientes, contra los mínimos del diseño.
- `descartes_por_tipo`: qué recomendaciones descarta la gente y por qué (la primera señal de ruido).
- `guardar_texto`: el borrador de Claude y la versión que la persona envió.

Todo es lectura sobre la base del cliente. Nada sale a la IA.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine

from . import config, reglas
from .db import (acciones, cargas, decisiones, facturas, recaudos, reglas_version, resultados_comercial,
                 resultados_margen, textos_aprobados, ventas)

HORIZONTES = (30, 60, 90)
MORA_SERIA_DIAS = 30            # "cayó en mora" = alguna factura con más de estos días de mora


# --- Recaudos ---------------------------------------------------------------------------------------
def recaudos_por_factura(motor: Engine) -> pd.DataFrame:
    """Pagos recibidos: nit, factura, fecha_pago, valor. Si el mismo pago llega en dos reportes que se
    traslapan, cuenta una vez. Vacío si el cliente no ha cargado recaudos."""
    with motor.connect() as cx:
        filas = cx.execute(select(recaudos.c.nit, recaudos.c.factura, recaudos.c.fecha_pago, recaudos.c.valor,
                                  recaudos.c.recibo)).mappings().all()
    df = pd.DataFrame(filas, columns=["nit", "factura", "fecha_pago", "valor", "recibo"])
    if df.empty:
        return df.drop(columns="recibo")
    df["recibo"] = df["recibo"].fillna("")
    df = df.drop_duplicates(subset=["nit", "factura", "fecha_pago", "valor", "recibo"])
    return df.drop(columns="recibo").sort_values(["fecha_pago", "nit", "factura"]).reset_index(drop=True)


def _cobertura_recaudos(motor: Engine) -> date | None:
    """Hasta qué fecha llegan los recaudos cargados (la fecha de corte del último reporte)."""
    with motor.connect() as cx:
        return cx.execute(select(func.max(cargas.c.fecha_corte)).where(cargas.c.tipo == "recaudos")).scalar()


# --- Seudonimización --------------------------------------------------------------------------------
def codigo_cliente(nit: str) -> str:
    """Código estable e irreversible para el NIT, para sacar el dataset de la base del cliente.
    Usa la clave ONTARA_SAL_ANALITICA; configurarla en producción (sin ella, cualquiera que conozca el
    NIT puede recalcular el código)."""
    sal = os.environ.get("ONTARA_SAL_ANALITICA") or f"ontara|{config.nombre_empresa()}"
    return "C-" + hmac.new(sal.encode(), str(nit).encode(), hashlib.sha256).hexdigest()[:12]


# --- Dataset de entrenamiento -----------------------------------------------------------------------
def _decisiones(motor: Engine) -> pd.DataFrame:
    q = (select(decisiones.c.id.label("decision_id"), decisiones.c.paquete, decisiones.c.tipo, decisiones.c.nit,
                decisiones.c.estado, decisiones.c.grupo, decisiones.c.nivel_aprobacion, decisiones.c.prioridad,
                decisiones.c.saldo_vencido_inicial.label("monto_inicial"), decisiones.c.variables,
                decisiones.c.creada_en, reglas_version.c.version.label("regla_version"),
                cargas.c.fecha_corte.label("fecha_deteccion"))
         .join(cargas, cargas.c.id == decisiones.c.carga_origen_id)
         .join(reglas_version, reglas_version.c.id == decisiones.c.regla_version_id)
         .order_by(decisiones.c.id))
    with motor.connect() as cx:
        return pd.DataFrame(cx.execute(q).mappings().all())


def _acciones_por_decision(motor: Engine) -> pd.DataFrame:
    """Lo que hizo la persona con cada decisión, resumido en una fila."""
    with motor.connect() as cx:
        acc = pd.DataFrame(cx.execute(select(acciones).where(acciones.c.tipo != "sistema")
                                      .order_by(acciones.c.id)).mappings().all())
    cols = ["decision_id", "primera_accion", "fecha_primera_accion", "atendida", "motivo", "canal",
            "fecha_compromiso", "monto_compromiso", "notas"]
    if acc.empty:
        return pd.DataFrame(columns=cols)
    filas = []
    for did, g in acc.groupby("decision_id"):
        decisivas = g[g["tipo"] != "nota"]
        con_motivo = g[g["motivo"].notna()]
        con_canal = g[g["canal"].notna()]
        con_fecha = g[g["fecha_compromiso"].notna()]
        con_monto = g[g["monto_compromiso"].notna()]
        filas.append({
            "decision_id": did,
            "primera_accion": decisivas["tipo"].iloc[0] if len(decisivas) else None,
            "fecha_primera_accion": decisivas["fecha"].iloc[0] if len(decisivas) else None,
            "atendida": bool(g["tipo"].isin(["aprobar", "ajustar", "ejecutar"]).any()),
            "motivo": con_motivo["motivo"].iloc[-1] if len(con_motivo) else None,
            "canal": con_canal["canal"].iloc[-1] if len(con_canal) else None,
            "fecha_compromiso": con_fecha["fecha_compromiso"].iloc[-1] if len(con_fecha) else None,
            "monto_compromiso": con_monto["monto_compromiso"].iloc[-1] if len(con_monto) else None,
            "notas": int((g["tipo"] == "nota").sum()),
        })
    return pd.DataFrame(filas, columns=cols)


def _cortes(motor: Engine, tipo: str) -> list[tuple[date, int]]:
    """(fecha de corte, carga) por fecha; si un corte se cargó dos veces vale la última versión."""
    with motor.connect() as cx:
        filas = cx.execute(select(cargas.c.fecha_corte, func.max(cargas.c.id)).where(cargas.c.tipo == tipo)
                           .group_by(cargas.c.fecha_corte).order_by(cargas.c.fecha_corte)).all()
    return [(f, i) for f, i in filas]


def _primer_corte_desde(cortes: list[tuple[date, int]], objetivo: date) -> tuple[date, int] | None:
    return next(((f, i) for f, i in cortes if f >= objetivo), None)


def _etiquetas_cartera(motor: Engine, ds: pd.DataFrame, horizontes) -> pd.DataFrame:
    """Para toda decisión de cartera (cobrar, frenar, ampliar): saldo vencido y mora del cliente en el
    primer corte que cumple el horizonte. Sirve para medir cobros y para ver si un cupo ampliado terminó en mora."""
    cortes = _cortes(motor, "cartera")
    gracia = int(reglas.regla_activa(motor)["parametros"]["dias_gracia"])
    cache: dict[int, dict] = {}

    def resumen(carga_id: int, fecha: date) -> dict:
        if carga_id not in cache:
            with motor.connect() as cx:
                f = pd.DataFrame(cx.execute(select(facturas).where(facturas.c.carga_id == carga_id)).mappings().all())
            r = reglas.resumen_por_cliente(f, fecha, gracia) if not f.empty else pd.DataFrame(
                columns=["nit", "saldo_vencido", "dias_mora_max"])
            cache[carga_id] = {x.nit: (float(x.saldo_vencido), int(x.dias_mora_max)) for x in r.itertuples()}
        return cache[carga_id]

    filas = []
    for d in ds.itertuples():
        fila = {"decision_id": d.decision_id}
        for h in horizontes:
            corte = _primer_corte_desde(cortes, d.fecha_deteccion + timedelta(days=h))
            if corte is None:
                continue
            venc, mora = resumen(corte[1], corte[0]).get(d.nit, (0.0, 0))
            fila[f"dias_medidos_{h}d"] = (corte[0] - d.fecha_deteccion).days
            fila[f"saldo_vencido_{h}d"] = venc
            fila[f"mora_max_{h}d"] = mora
            fila[f"recuperado_{h}d"] = max(0.0, float(d.monto_inicial) - venc)
            fila[f"cayo_en_mora_{h}d"] = mora > MORA_SERIA_DIAS
        filas.append(fila)
    return pd.DataFrame(filas)


def _etiquetas_recaudos(motor: Engine, ds: pd.DataFrame, horizontes) -> pd.DataFrame:
    """Pesos realmente recibidos del cliente después de detectar la decisión, y días hasta el primer pago.
    Solo se llena hasta donde llegan los recaudos cargados."""
    rec = recaudos_por_factura(motor)
    hasta = _cobertura_recaudos(motor)
    if rec.empty or hasta is None:
        return pd.DataFrame({"decision_id": ds["decision_id"]})
    por_nit = {nit: g for nit, g in rec.groupby("nit")}
    filas = []
    vacio = rec.iloc[0:0]
    for d in ds.itertuples():
        g = por_nit.get(d.nit, vacio)
        despues = g[g["fecha_pago"] > d.fecha_deteccion]
        fila = {"decision_id": d.decision_id}
        primero = despues["fecha_pago"].min() if len(despues) else None
        if primero is not None:
            fila["dias_hasta_primer_pago"] = (primero - d.fecha_deteccion).days
        for h in horizontes:
            limite = d.fecha_deteccion + timedelta(days=h)
            if limite > hasta:
                continue
            fila[f"pagado_{h}d"] = float(despues.loc[despues["fecha_pago"] <= limite, "valor"].sum())
        filas.append(fila)
    return pd.DataFrame(filas)


def _etiquetas_tabla(motor: Engine, ds: pd.DataFrame, tabla, columnas: dict[str, str], horizontes) -> pd.DataFrame:
    """Etiquetas desde una tabla de resultados por corte (comercial, margen): la medición del primer
    corte que cumple el horizonte."""
    with motor.connect() as cx:
        med = pd.DataFrame(cx.execute(select(tabla, cargas.c.fecha_corte).join(cargas, cargas.c.id == tabla.c.carga_id)
                                      .where(tabla.c.decision_id.in_([int(x) for x in ds["decision_id"]]))
                                      .order_by(cargas.c.fecha_corte, tabla.c.id)).mappings().all())
    filas = []
    por_dec = {k: g for k, g in med.groupby("decision_id")} if not med.empty else {}
    for d in ds.itertuples():
        g = por_dec.get(d.decision_id)
        fila = {"decision_id": d.decision_id}
        if g is not None:
            for h in horizontes:
                ok = g[g["fecha_corte"] >= d.fecha_deteccion + timedelta(days=h)]
                if ok.empty:
                    continue
                m = ok.iloc[0]
                fila[f"dias_medidos_{h}d"] = (m["fecha_corte"] - d.fecha_deteccion).days
                for origen, destino in columnas.items():
                    fila[f"{destino}_{h}d"] = m[origen]
        filas.append(fila)
    return pd.DataFrame(filas)


def dataset(motor: Engine, horizontes: tuple[int, ...] = HORIZONTES, seudonimizar: bool = True) -> pd.DataFrame:
    """Una fila por decisión: qué se recomendó, con qué variables, qué hizo la persona y qué pasó después.

    Las columnas `v_*` son la foto de variables al detectar la decisión. Las columnas `*_30d`, `*_60d`,
    `*_90d` son el resultado en el primer corte que cumple ese plazo; vacías si aún no ha pasado.
    Con `seudonimizar` el NIT se reemplaza por un código y no hay nombres: así puede salir de la base del
    cliente para análisis (Ley 1581: sigue siendo dato del cliente; C&M LABS lo usa como encargado)."""
    ds = _decisiones(motor)
    if ds.empty:
        return ds
    ds["fecha_deteccion"] = pd.to_datetime(ds["fecha_deteccion"]).dt.date
    acc = _acciones_por_decision(motor)
    ds = ds.merge(acc, on="decision_id", how="left")
    ds["atendida"] = ds["atendida"].astype("boolean").fillna(False).astype(bool)
    ds["dias_hasta_accion"] = [
        (pd.Timestamp(f) - pd.Timestamp(c)).days if f is not None and not pd.isna(f) else None
        for f, c in zip(ds["fecha_primera_accion"], ds["creada_en"])]
    variables = pd.DataFrame([v or {} for v in ds["variables"]]).add_prefix("v_")
    ds = pd.concat([ds.drop(columns=["variables", "fecha_primera_accion"]), variables], axis=1)

    partes = []
    cartera = ds[ds["paquete"] == "cartera"]
    if not cartera.empty:
        partes.append(_etiquetas_cartera(motor, cartera, horizontes))
    partes.append(_etiquetas_recaudos(motor, ds, horizontes))
    comercial = ds[ds["paquete"] == "comercial"]
    if not comercial.empty:
        partes.append(_etiquetas_tabla(motor, comercial, resultados_comercial,
                                       {"volvio_a_comprar": "volvio_a_comprar", "ventas_despues": "ventas_despues"},
                                       horizontes))
    margen = ds[ds["paquete"] == "margen"]
    if not margen.empty:
        partes.append(_etiquetas_tabla(motor, margen, resultados_margen,
                                       {"margen_despues_pct": "margen_despues_pct",
                                        "utilidad_adicional": "utilidad_adicional"}, horizontes))
    etiquetas = pd.concat([p.set_index("decision_id") for p in partes if not p.empty], axis=0)
    etiquetas = etiquetas.groupby(level=0).first()        # una decisión aparece en cartera y en recaudos
    ds = ds.merge(etiquetas, left_on="decision_id", right_index=True, how="left")

    if seudonimizar:
        ds.insert(1, "cliente", [codigo_cliente(n) for n in ds["nit"]])
        ds = ds.drop(columns="nit")
    return ds.drop(columns="creada_en").reset_index(drop=True)


# --- Preparación: ¿hay datos suficientes para aprender? -------------------------------------------
def preparacion(motor: Engine) -> list[dict]:
    """Indicadores contra los mínimos orientativos del diseño de aprendizaje (estimaciones a validar en el
    primer piloto). `listo` dice si ya se alcanzó la meta."""
    with motor.connect() as cx:
        n_cortes = cx.execute(select(func.count(func.distinct(cargas.c.fecha_corte)))
                              .where(cargas.c.tipo == "cartera")).scalar() or 0
        acc = pd.DataFrame(cx.execute(select(acciones.c.tipo, acciones.c.motivo, acciones.c.canal)
                                      .where(acciones.c.tipo.in_(["descartar", "ajustar", "ejecutar"]))).mappings().all(),
                           columns=["tipo", "motivo", "canal"])
        rango_v = cx.execute(select(func.min(ventas.c.fecha), func.max(ventas.c.fecha))).one()
        rango_r = cx.execute(select(func.min(recaudos.c.fecha_pago), func.max(recaudos.c.fecha_pago))).one()
    ds = dataset(motor, seudonimizar=False)
    cobros = ds[ds["tipo"] == "cobrar"] if not ds.empty else ds
    medidos = int(cobros["saldo_vencido_30d"].notna().sum()) if "saldo_vencido_30d" in cobros else 0
    en_control = int((cobros["grupo"] == "control").sum()) if not cobros.empty else 0
    with motor.connect() as cx:
        con_variables = sum(1 for v in cx.execute(select(decisiones.c.variables)).scalars() if v)
    desc_aj = acc[acc["tipo"].isin(["descartar", "ajustar"])]
    ejec = acc[acc["tipo"] == "ejecutar"]
    pct_motivo = round(100 * desc_aj["motivo"].notna().mean()) if len(desc_aj) else None
    pct_canal = round(100 * ejec["canal"].notna().mean()) if len(ejec) else None
    n_rec = len(recaudos_por_factura(motor))

    def meses(rango) -> float:
        a, b = rango
        return round((b - a).days / 30.4, 1) if a and b else 0.0

    def fila(indicador, valor, meta, listo, para, unidad=""):
        return {"indicador": indicador, "valor": "Sin datos" if valor is None else f"{valor}{unidad}",
                "meta": f"{meta}{unidad}", "listo": bool(listo), "para": para}

    return [
        fila("Cortes de cartera cargados", n_cortes, 6, n_cortes >= 6, "Calibrar umbrales (etapa 1)"),
        fila("Decisiones con foto de variables", con_variables, 200, con_variables >= 200, "Calibrar umbrales (etapa 1)"),
        fila("Cobros con resultado a 30 días", medidos, 200, medidos >= 200, "Calibrar umbrales (etapa 1)"),
        fila("Descartes y ajustes con motivo", pct_motivo, 80, (pct_motivo or 0) >= 80,
             "Leer el ruido de las reglas (etapa 1)", " %"),
        fila("Gestiones con canal registrado", pct_canal, 80, (pct_canal or 0) >= 80,
             "Modelo de efecto del cobro (etapa 2)", " %"),
        fila("Cobros en grupo de control", en_control, 300, en_control >= 300, "Modelo de efecto del cobro (etapa 2)"),
        fila("Pagos con fecha real (recaudos)", n_rec, 1000, n_rec >= 1000, "Modelo de pago a 30 días (etapa 2)"),
        fila("Meses de historia de recaudos", meses(rango_r), 12, meses(rango_r) >= 12,
             "Modelo de pago a 30 días (etapa 2)"),
        fila("Meses de historia de ventas", meses(rango_v), 12, meses(rango_v) >= 12,
             "Modelo de abandono (etapa 2)"),
    ]


# --- Descartes: la primera señal de que una regla genera ruido --------------------------------------
def descartes_por_tipo(motor: Engine) -> pd.DataFrame:
    """Por paquete y tipo de decisión: cuántas se mostraron, cuántas se descartaron y el motivo más común."""
    from .decisiones import MOTIVOS
    with motor.connect() as cx:
        dec = pd.DataFrame(cx.execute(select(decisiones.c.id, decisiones.c.paquete, decisiones.c.tipo,
                                             decisiones.c.estado).where(decisiones.c.grupo != "control"))
                           .mappings().all(), columns=["id", "paquete", "tipo", "estado"])
        mot = pd.DataFrame(cx.execute(select(acciones.c.decision_id, acciones.c.motivo)
                                      .where(acciones.c.tipo == "descartar")).mappings().all(),
                           columns=["decision_id", "motivo"])
    if dec.empty:
        return pd.DataFrame(columns=["paquete", "tipo", "mostradas", "descartadas", "tasa_descarte_pct",
                                     "motivo_principal"])
    dec = dec.merge(mot, left_on="id", right_on="decision_id", how="left")
    filas = []
    for (paq, tipo), g in dec.groupby(["paquete", "tipo"]):
        desc = g[g["estado"] == "descartada"]
        motivos = desc["motivo"].dropna()
        principal = motivos.value_counts().idxmax() if len(motivos) else None
        filas.append({"paquete": paq, "tipo": tipo, "mostradas": int(g["id"].nunique()),
                      "descartadas": int(desc["id"].nunique()),
                      "tasa_descarte_pct": round(100 * desc["id"].nunique() / g["id"].nunique(), 1),
                      "motivo_principal": MOTIVOS["descartar"].get(principal) if principal else None})
    return pd.DataFrame(filas).sort_values("tasa_descarte_pct", ascending=False).reset_index(drop=True)


# --- Textos aprobados -------------------------------------------------------------------------------
def guardar_texto(motor: Engine, decision_id: int, tipo: str, borrador: str, final: str, usuario: str) -> int:
    """Guarda el borrador de Claude y la versión que la persona aprobó o envió. Más adelante, los mejores
    servirán de ejemplo para redactar en el tono de la empresa (siempre seudonimizados)."""
    if not (final or "").strip():
        raise ValueError("El texto final está vacío.")
    with motor.begin() as cx:
        return cx.execute(insert(textos_aprobados).values(
            decision_id=decision_id, tipo=tipo, borrador=borrador or "", final=final.strip(),
            editado=(borrador or "").strip() != final.strip(), usuario=usuario)).inserted_primary_key[0]


def textos(motor: Engine, tipo: str | None = None) -> list[dict]:
    q = select(textos_aprobados).order_by(textos_aprobados.c.id.desc())
    if tipo:
        q = q.where(textos_aprobados.c.tipo == tipo)
    with motor.connect() as cx:
        return [dict(r) for r in cx.execute(q).mappings()]
