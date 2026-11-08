"""Ontara Cartera · aplicación web (Streamlit).

Ejecutar:  streamlit run app/streamlit_app.py
La lógica vive en el paquete `ontara`; esta capa solo muestra y captura.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ontara import acceso, aprendizaje, asistente, caja, calidad, comercial, margen, paquetes, config, decisiones, impacto, ingesta, ontologia, reglas  # noqa: E402
from ontara.db import cargas, crear_motor  # noqa: E402
from sqlalchemy import select  # noqa: E402

st.set_page_config(page_title="Ontara · Cartera", page_icon="◆", layout="wide")


@st.cache_resource
def motor():
    return crear_motor()


M = motor()
COLOR = {"verde": "🟢", "amarillo": "🟡", "rojo": "🔴"}
TIPO = paquetes.todos_los_tipos()
ACTIVOS = paquetes.activos()
NIVEL = {"lider": "Líder", "gerente": "Gerente"}
ETIQUETAS = {"fecha": "Fecha de la factura", "producto": "Producto", "costo": "Costo total de la línea",
             "descuento": "Descuento (pesos)", "cantidad": "Cantidad", "nit": "NIT", "cliente": "Cliente", "numero": "Número de factura", "fecha_emision": "Fecha de emisión",
             "fecha_vencimiento": "Fecha de vencimiento", "saldo": "Saldo", "valor": "Valor", "vendedor": "Vendedor",
             "cupo": "Cupo de crédito", "factura": "Factura que se paga", "fecha_pago": "Fecha de pago",
             "recibo": "Recibo de caja", "proveedor": "Proveedor", "categoria": "Concepto o categoría"}
ESTADO = {"pendiente": "Pendiente", "aprobada": "Aprobada", "ajustada": "Ajustada",
          "ejecutada": "Ejecutada", "descartada": "Descartada", "cerrada": "Cerrada"}


def pesos(x) -> str:
    x = x or 0
    return ("-$" if x < 0 else "$") + f"{abs(x):,.0f}".replace(",", ".")


# --- Acceso -----------------------------------------------------------------------
PROVEEDORES = {"google": "Google", "microsoft": "Microsoft"}


def proveedores_oidc() -> list[str]:
    """Proveedores configurados en .streamlit/secrets.toml, sección [auth.google] o [auth.microsoft]."""
    try:
        auth = st.secrets.get("auth", {})
    except Exception:            # sin archivo de secretos
        return []
    return [k for k in PROVEEDORES if k in auth]


def _entrar(usuario: str, rol: str, metodo: str):
    st.session_state.usuario, st.session_state.rol, st.session_state.metodo = usuario, rol, metodo
    acceso.registrar(M, usuario, "ingreso", metodo)


def login() -> tuple[str, str]:
    if "usuario" in st.session_state:
        return st.session_state.usuario, st.session_state.rol
    lista = acceso.usuarios()
    if not lista:
        st.session_state.usuario, st.session_state.rol, st.session_state.metodo = "demo", "gerente", "demo"
        return "demo", "gerente"
    oidc = proveedores_oidc()
    # Volvió del proveedor de identidad: solo entra si su correo está autorizado.
    if oidc and getattr(st.user, "is_logged_in", False):
        correo = st.user.get("email") or ""
        rol = acceso.rol_de(correo, lista)
        if rol:
            _entrar(correo.lower(), rol, "oidc")
            st.rerun()
        if not st.session_state.get("_denegado_registrado"):
            acceso.registrar(M, correo, "denegado", "oidc", "Correo no autorizado en ONTARA_USUARIOS")
            st.session_state._denegado_registrado = True
        st.error(f"La cuenta {correo} no tiene acceso a Ontara. Pide al gerente que la autorice.")
        if st.button("Salir"):
            st.logout()
        st.stop()

    st.title("Ontara · Cartera")
    for prov in oidc:
        if st.button(f"Entrar con {PROVEEDORES[prov]}", type="primary", key=f"login_{prov}"):
            st.login(prov)
    hay_claves = any(u.get("clave") for u in lista.values())
    if hay_claves:
        if oidc:
            st.caption("O con usuario y clave:")
        with st.form("login"):
            u = st.text_input("Usuario")
            c = st.text_input("Clave", type="password")
            if st.form_submit_button("Entrar"):
                rol = acceso.autenticar_con_clave(u, c, lista)
                if rol:
                    _entrar(u.strip().lower(), rol, "clave")
                    st.rerun()
                acceso.registrar(M, u, "fallido", "clave")
                st.error("Usuario o clave incorrectos.")
    if not oidc and not hay_claves:
        st.error("No hay forma de entrar configurada: agrega [auth] en secrets.toml o claves en ONTARA_USUARIOS.")
    st.stop()


def salir():
    acceso.registrar(M, USUARIO, "salida", st.session_state.get("metodo", "clave"))
    metodo = st.session_state.get("metodo")
    for k in ("usuario", "rol", "metodo"):
        st.session_state.pop(k, None)
    if metodo == "oidc":
        st.logout()
    st.rerun()


USUARIO, ROL = login()

with st.sidebar:
    st.markdown(f"### ◆ Ontara\n**{config.nombre_empresa()}**")
    paginas = ["Bandeja de decisiones", "Cargar datos", "Impacto", "Asistente", "Reglas", "Aprendizaje"]
    if ROL == "gerente":
        paginas.append("Accesos")
    pagina = st.radio("Ir a", paginas, label_visibility="collapsed")
    st.caption(f"Sesión: {USUARIO} · {ROL}")
    if not acceso.usuarios():
        st.warning("Modo demostración: sin login. Configura ONTARA_USUARIOS antes de usar datos reales.")
    elif st.button("Salir"):
        salir()


# --- Cargar datos ---------------------------------------------------------------------
def pagina_cargar():
    st.header("Cargar datos")
    reportes = list(dict.fromkeys([p.reporte for p in ACTIVOS] + ["recaudos"]))
    tipo = st.radio("¿Qué reporte vas a cargar?", reportes, format_func=lambda t: paquetes.REPORTES[t]["nombre"],
                    horizontal=True)
    usa = ", ".join(p.nombre for p in paquetes.de_reporte(tipo)) or "el aprendizaje de Ontara (fecha real de pago)"
    st.write({"cartera": "Sube el reporte de cartera por edades o de facturas pendientes",
              "ventas": "Sube el reporte de ventas de los últimos 12 meses (todas las facturas, incluidas las pagadas)",
              "lineas": "Sube el reporte de ventas por producto de los últimos 6 meses (una fila por producto de cada "
                        "factura, con venta neta y costo)",
              "pagos": "Sube el reporte de cuentas por pagar (facturas de proveedores pendientes) con las obligaciones "
                       "programadas que quieras incluir: nómina, impuestos, cuotas de crédito y arriendo, con su fecha "
                       "de pago",
              "recaudos": "Sube el reporte de recaudos o recibos de caja (cada pago con la factura que cruza). La "
                          "primera vez, trae 12 a 24 meses de historia; después, el periodo desde el último corte"}[tipo]
             + " que exportas de tu ERP (Siigo, World Office, SAP) o tu Excel. Ontara reconoce las columnas y "
             f"revisa la calidad antes de guardar nada. Lo usa: {usa}.")
    corte = st.date_input("Fecha de corte del reporte", value=date.today(), format="DD/MM/YYYY")
    saldo_bancos = None
    if tipo == "pagos":
        saldo_bancos = st.number_input("Saldo en bancos a la fecha de corte (COP): suma de todas las cuentas",
                                       min_value=0, value=0, step=1_000_000)
    archivo = st.file_uploader("Archivo Excel o CSV", type=["xlsx", "xls", "csv"], key=f"archivo_{tipo}")
    if not archivo:
        st.info("También puedes probar con los archivos de `datos_ejemplo/`.")
        return
    crudo = ingesta.leer_archivo(archivo.getvalue(), archivo.name)
    st.caption(f"{len(crudo)} filas · {len(crudo.columns)} columnas")

    plantilla = ontologia.cargar_plantilla(objeto=paquetes.REPORTES[tipo]["objeto"])
    sugerido = ontologia.sugerir_mapeo(list(crudo.columns), plantilla)
    st.subheader("1. Columnas")
    opciones = ["(no está)"] + list(crudo.columns)
    mapeo = {}
    cols = st.columns(3)
    for i, campo in enumerate(plantilla.campos_factura):
        etiqueta = ETIQUETAS.get(campo.nombre, campo.nombre) + (" *" if campo.requerido else "")
        actual = sugerido.get(campo.nombre, "(no está)")
        sel = cols[i % 3].selectbox(etiqueta, opciones, index=opciones.index(actual), key=f"map_{tipo}_{campo.nombre}")
        if sel != "(no está)":
            mapeo[campo.nombre] = sel

    datos = ingesta.convertir(crudo, mapeo, plantilla)
    evaluar = {"cartera": calidad.evaluar, "ventas": calidad.evaluar_ventas, "lineas": calidad.evaluar_lineas,
               "pagos": calidad.evaluar_pagos, "recaudos": calidad.evaluar_recaudos}[tipo]
    informe, validas = evaluar(datos, mapeo, plantilla)
    st.subheader(f"2. Semáforo de calidad: {COLOR[informe.semaforo]} {informe.semaforo.capitalize()}")
    if informe.faltan_campos:
        st.error("Faltan columnas obligatorias: " + ", ".join(informe.faltan_campos))
        return
    st.write(f"{informe.filas_validas} de {informe.filas_total} filas se pueden usar.")
    st.dataframe(pd.DataFrame([{
        "": COLOR[v.nivel], "Validación": v.nombre, "Qué revisa": v.descripcion,
        "Filas": v.filas_afectadas, "%": v.porcentaje,
        "Se excluyen": "Sí" if v.excluye_filas and v.filas_afectadas else "",
        "Ejemplos (fila del archivo)": ", ".join(map(str, v.ejemplos)),
    } for v in informe.validaciones]), hide_index=True, use_container_width=True)

    with motor().connect() as cx:
        repetida = cx.execute(select(cargas.c.id).where(cargas.c.fecha_corte == corte,
                                                         cargas.c.tipo == tipo)).first()
    if repetida:
        st.warning("Ya hay una carga con esta fecha de corte. Si continúas se guardará otra versión del mismo corte.")

    st.subheader("3. Guardar y recalcular")
    if tipo == "pagos" and not saldo_bancos:
        st.info("Digita el saldo en bancos a la fecha de corte: es el punto de partida de la proyección.")
    if st.button("Cargar y generar decisiones", type="primary",
                 disabled=not informe.puede_cargar or (tipo == "pagos" and not saldo_bancos)):
        r = decisiones.registrar_carga(M, validas, informe, mapeo, corte, archivo.name, USUARIO, tipo,
                                       float(saldo_bancos) if tipo == "pagos" else None)
        st.success(f"Carga #{r['carga_id']} guardada. Decisiones nuevas: {r['decisiones_nuevas']}, "
                   f"actualizadas: {r['decisiones_actualizadas']}, cerradas: {r['decisiones_cerradas']}. "
                   f"Resultados medidos: {r['resultados_medidos']}.")
        if r["linea_base_creada"]:
            st.info("Esta es la primera carga: quedó guardada como línea base para medir el impacto.")


# --- Bandeja -----------------------------------------------------------------------------
def pagina_bandeja():
    st.header("Bandeja de decisiones")
    c0, c1 = st.columns([1, 1])
    paq = c0.segmented_control("Paquete", ["Todos", *(p.nombre for p in ACTIVOS)], default="Todos")
    filtro = c1.segmented_control("Estado", ["Abiertas", "Pendientes", "Todas"], default="Abiertas")
    paq_k = next((p.clave for p in ACTIVOS if p.nombre == paq), None)
    tipos_vis = paquetes.obtener(paq_k).tipos if paq_k else TIPO
    c2, c3, c4 = st.columns(3)
    tipo = c2.selectbox("Tipo", ["Todos", *tipos_vis.values()])
    quien = c3.selectbox("Quién aprueba", ["Todos", "Líder", "Gerente"],
                         index=2 if ROL == "gerente" and paq_k != "comercial" else 0)
    estados = {"Abiertas": decisiones.ABIERTAS, "Pendientes": ("pendiente",), "Todas": None}[filtro or "Abiertas"]
    tipo_k = {v: k for k, v in TIPO.items()}.get(tipo)
    nivel_k = {"Líder": "lider", "Gerente": "gerente"}.get(quien)
    lista = decisiones.listar(M, estados, tipo_k, nivel=nivel_k, paquete=paq_k)
    vendedores = sorted({d["evidencia"].get("vendedor") or d.get("vendedor") for d in lista} - {None})
    vend = c4.selectbox("Vendedor", ["Todos", *vendedores])
    if vend != "Todos":
        lista = [d for d in lista if (d["evidencia"].get("vendedor") or d.get("vendedor")) == vend]
    if not lista:
        st.info("No hay decisiones con este filtro. Carga un reporte para empezar.")
        return
    total = sum(d["evidencia"]["saldo_vencido"] for d in lista if d["tipo"] == "cobrar")
    cupo = sum(d["evidencia"].get("aumento_sugerido", 0) for d in lista if d["tipo"] == "ampliar_cupo")
    riesgo = sum(d["saldo_vencido_inicial"] for d in lista if d["tipo"] == "recuperar_cliente")
    utilidad = sum(d["saldo_vencido_inicial"] for d in lista if d["paquete"] == "margen")
    deficit = max((d["saldo_vencido_inicial"] for d in lista if d["tipo"] == "caja_bajo_minimo"), default=0)
    st.caption(f"{len(lista)} decisiones · {pesos(total)} vencidos en las de cobro · "
               f"{pesos(cupo)} de cupo adicional sugerido · {pesos(riesgo)} de ventas trimestrales en riesgo "
               f"en clientes que se están enfriando · {pesos(utilidad)} de utilidad en juego en margen"
               + (f" · {pesos(deficit)} de caja por cubrir en la peor semana" if deficit else ""))
    for d in lista:
        ev = d["evidencia"]
        icono = "🟢" if d["tipo"] in ("ampliar_cupo", "crecer_cliente", "excedente_de_caja") else "🔴" if d["prioridad"] >= 70 else \
            "🟡" if d["prioridad"] >= 40 else "⚪"
        clave = " ⭐" if ev.get("cliente_clave") else ""
        gerente = " · aprueba gerente" if d["nivel_aprobacion"] == "gerente" else ""
        if d["tipo"] == "ampliar_cupo":
            detalle_tipo = f"{ev.get('uso_cupo_pct', 0):.0f}% del cupo usado"
        elif d["tipo"] == "recuperar_cliente":
            detalle_tipo = f"{ev['dias_sin_comprar']} días sin comprar · 90 días {-ev['caida_pct']:+.0f}%"
        elif d["tipo"] == "crecer_cliente":
            detalle_tipo = f"90 días {pesos(ev['ventas_90d'])}"
        elif d["tipo"] == "caja_bajo_minimo":
            detalle_tipo = (f"semanas {ev['semana_desde']} a {ev['semana_hasta']} · faltan {pesos(ev['deficit'])}"
                            if ev["semana_desde"] != ev["semana_hasta"]
                            else f"semana {ev['semana_desde']} · faltan {pesos(ev['deficit'])}")
        elif d["tipo"] == "aplazar_pago_proveedor":
            detalle_tipo = f"{pesos(ev['monto'])} a correr {ev['dias_aplazamiento']} días"
        elif d["tipo"] == "excedente_de_caja":
            detalle_tipo = f"{pesos(ev['excedente'])} sin uso en 13 semanas"
        elif d["paquete"] == "margen":
            detalle_tipo = (f"margen {ev['margen_pct']:.1f}% · descuento {ev['descuento_pct']:.1f}% · "
                            f"{pesos(ev['utilidad_en_juego'])} en juego")
        else:
            detalle_tipo = f"{pesos(ev['saldo_vencido'])} vencidos · {ev['dias_mora_max']} días"
        quien_es = d["cliente"] or ev.get("producto") or ev.get("cliente") or ev.get("nombre_mostrar") or d["nit"]
        titulo = (f"{icono} {d['prioridad']:.0f} · {quien_es}{clave} · {TIPO[d['tipo']]} · "
                  f"{detalle_tipo} · {ESTADO[d['estado']]}{gerente}")
        with st.expander(titulo):
            tarjeta(d)


def tarjeta(d: dict):
    ev = d["evidencia"]
    st.markdown(f"**Decisión #{d['id']}: {d['recomendacion']}**")
    st.write(d["motivo"])
    if d["paquete"] == "comercial":
        tarjeta_comercial(d, ev)
    elif d["paquete"] == "margen":
        tarjeta_margen(d, ev)
    elif d["paquete"] == "caja":
        tarjeta_caja(d, ev)
    else:
        tarjeta_cartera(d, ev)
    acciones_y_historial(d)


def tarjeta_comercial(d: dict, ev: dict):
    a, b, c, e = st.columns(4)
    a.metric("Ventas 12 meses", pesos(ev["ventas_12m"]))
    b.metric("Últimos 90 días", pesos(ev["ventas_90d"]),
             delta=f"{-ev['caida_pct']:+.0f}% vs. su trimestre promedio")
    c.metric("Días sin comprar", ev["dias_sin_comprar"],
             help=f"Suele comprar cada {ev['frecuencia_dias']:.0f} días" if ev.get("frecuencia_dias") else None)
    e.metric("Vendedor", ev.get("vendedor") or "Sin dato")
    st.caption(f"{ev['compras_12m']} compras en 12 meses · cada {ev['frecuencia_dias'] or 0:.0f} días en promedio · "
               f"{ev['participacion_ventas_pct']}% de las ventas" + (" · ⭐ cliente clave" if ev.get("cliente_clave") else ""))
    for cruce in ev.get("cruces_cartera", []):
        st.info("Cruce con Cartera: " + cruce)
    if ev.get("ultimas_compras"):
        tabla = pd.DataFrame(ev["ultimas_compras"])
        tabla["valor"] = tabla["valor"].map(pesos)
        st.dataframe(tabla.rename(columns={"fecha": "Fecha", "numero": "Factura", "valor": "Valor"}),
                     hide_index=True, use_container_width=True)
    st.caption(f"NIT {d['nit']} · Regla comercial v{ev.get('regla_version')} · reporte {ev['fecha_corte']}")


def tarjeta_margen(d: dict, ev: dict):
    a, b, c, e = st.columns(4)
    a.metric(f"Ventas {ev['meses_analisis']} meses", pesos(ev["ventas"]))
    b.metric("Margen bruto", f"{ev['margen_pct']:.1f}%",
             delta=f"{ev['margen_pct'] - ev['margen_minimo_pct']:+.1f} pts vs. mínimo", delta_color="normal")
    c.metric("Descuento promedio", f"{ev['descuento_pct']:.1f}%")
    e.metric("Utilidad en juego", pesos(ev["utilidad_en_juego"]))
    st.caption(f"Margen de la empresa en el periodo: {ev['margen_empresa_pct']:.1f}%"
               + (f" · {ev['lineas_bajo_costo']} líneas vendidas bajo el costo ({pesos(ev['perdida_bajo_costo'])} "
                  f"de pérdida)" if ev.get("lineas_bajo_costo") else "")
               + (" · ⭐ cliente clave" if ev.get("cliente_clave") else ""))
    for cruce in ev.get("cruces", []):
        st.info("Cruce: " + cruce)
    detalle = ev.get("productos") or ev.get("clientes")
    if detalle:
        tabla = pd.DataFrame(detalle).drop(columns=["nit"], errors="ignore")
        tabla["ventas"] = tabla["ventas"].map(pesos)
        st.markdown("**Productos de este cliente, de menor a mayor margen**" if ev.get("productos")
                    else "**Clientes que compran este producto, de menor a mayor margen**")
        st.dataframe(tabla.rename(columns={"producto": "Producto", "cliente": "Cliente", "ventas": "Ventas",
                                           "margen_pct": "Margen %", "descuento_pct": "Descuento %"}),
                     hide_index=True, use_container_width=True)
    st.caption(f"{'NIT ' + d['nit'] if 'nit' in ev else 'Producto'} · Regla margen v{ev.get('regla_version')} · "
               f"reporte {ev['fecha_corte']}")


def grafico_caja(proy: pd.DataFrame, minimo: float, alto: int = 320):
    """Saldo proyectado semana a semana frente a la caja mínima."""
    df = proy.copy()
    df["Semana"] = [f"S{s} · {pd.Timestamp(i):%d/%m}" for s, i in zip(df["semana"], df["inicio"])]
    df["Estado"] = ["Bajo el mínimo" if v < minimo else "Sobre el mínimo" for v in df["saldo_final"]]
    fig = px.bar(df, x="Semana", y="saldo_final", color="Estado", height=alto,
                 color_discrete_map={"Bajo el mínimo": "#c0392b", "Sobre el mínimo": "#2e86c1"},
                 labels={"saldo_final": "Saldo en bancos al final de la semana"})
    fig.add_hline(y=minimo, line_dash="dash", annotation_text="Caja mínima")
    fig.update_layout(legend_title_text="", margin=dict(t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)


def tarjeta_caja(d: dict, ev: dict):
    sup = ev.get("supuestos") or {}
    if d["tipo"] == "aplazar_pago_proveedor":
        a, b, c = st.columns(3)
        a.metric("Monto a correr", pesos(ev["monto"]))
        b.metric("Días", ev["dias_aplazamiento"])
        c.metric("Déficit de la racha", pesos(ev["deficit"]))
        tabla = pd.DataFrame(ev["facturas"])[["numero", "vencimiento", "nueva_fecha", "saldo"]]
        tabla["saldo"] = tabla["saldo"].map(pesos)
        st.dataframe(tabla.rename(columns={"numero": "Documento", "vencimiento": "Vence", "nueva_fecha": "Propuesta",
                                           "saldo": "Monto"}), hide_index=True, use_container_width=True)
        st.caption(f"NIT {d['nit']} · Regla caja v{ev.get('regla_version')} · reporte {ev['fecha_corte']}")
        return
    a, b, c, e = st.columns(4)
    a.metric("Saldo en bancos al corte", pesos(sup.get("saldo_bancos") or 0))
    if d["tipo"] == "caja_bajo_minimo":
        b.metric("Punto más bajo", pesos(ev["saldo_minimo"]))
        c.metric("Falta para el mínimo", pesos(ev["deficit"]))
        e.metric("Semanas para actuar", ev["semana_desde"] - 1)
    else:
        b.metric("Excedente sobre el mínimo", pesos(ev["excedente"]))
        c.metric("Semana más baja", ev["semana_mas_baja"])
        e.metric("Caja mínima", pesos(ev["caja_minima"]))
    proy = pd.DataFrame(ev["proyeccion"])
    if len(proy):
        grafico_caja(proy, ev["caja_minima"], 260)
    for cruce in ev.get("cruces", []):
        st.info("Cruce: " + cruce)
    if ev.get("cobrar"):
        st.markdown("**1. Cobrar antes de la semana en rojo**")
        t = pd.DataFrame(ev["cobrar"])
        t["decision_cartera"] = ["#" + str(int(x)) if x == x and x is not None else "" for x in t["decision_cartera"]]
        t["monto"] = t["monto"].map(pesos)
        st.dataframe(t[["cliente", "monto", "decision_cartera"]].rename(columns={
            "cliente": "Cliente", "monto": "Monto", "decision_cartera": "Decisión de Cartera"}),
            hide_index=True, use_container_width=True)
    if ev.get("aplazar"):
        st.markdown("**2. Negociar con proveedores** (nunca nómina, impuestos ni créditos)")
        t = pd.DataFrame(ev["aplazar"])
        t["saldo"] = t["saldo"].map(pesos)
        st.dataframe(t[["proveedor", "numero", "vencimiento", "nueva_fecha", "saldo"]].rename(columns={
            "proveedor": "Proveedor", "numero": "Documento", "vencimiento": "Vence", "nueva_fecha": "Propuesta",
            "saldo": "Monto"}), hide_index=True, use_container_width=True)
    if ev.get("financiar"):
        st.markdown(f"**3. Financiar lo que falte:** {pesos(ev['financiar'])} con crédito de tesorería o factoring.")
    if sup:
        st.caption(f"Supuestos: ventas de {pesos(sup['venta_semanal'])} por semana "
                   f"({sup['crecimiento_ventas_pct']:+.0f}% en el escenario), clientes pagan {sup['plazo_clientes_dias']:.0f} "
                   f"días + {sup['atraso_medio_dias']:.1f} de atraso promedio ({sup['clientes_con_historia_de_pago']} con "
                   f"historia de pagos), compras = {sup['compras_pct_ventas']:.0f}% de las ventas pagadas a "
                   f"{sup['plazo_proveedores_dias']:.0f} días · cartera del {sup['corte_cartera']}"
                   + (f" · {pesos(sup['cartera_no_proyectada'])} de cartera incobrable fuera de la proyección"
                      if sup.get("cartera_no_proyectada") else ""))
    st.caption(f"Regla caja v{ev.get('regla_version')} · reporte {ev['fecha_corte']}")


def tarjeta_cartera(d: dict, ev: dict):
    a, b, c, e = st.columns(4)
    a.metric("Saldo total", pesos(ev["saldo_total"]))
    if d["tipo"] == "ampliar_cupo":
        b.metric("Uso del cupo", f"{ev['uso_cupo_pct']:.0f}%")
        c.metric("Cupo sugerido", pesos(ev["cupo_sugerido"]), delta=pesos(ev["aumento_sugerido"]))
    else:
        b.metric("Saldo vencido", pesos(ev["saldo_vencido"]))
        c.metric("Mora máxima", f"{ev['dias_mora_max']} días")
    e.metric("Cupo", pesos(ev["cupo"]) if ev.get("cupo") else "Sin dato")
    if "compras_ventana" in ev:
        st.caption(f"Compras de los últimos {ev['meses_valor_cliente']} meses: {pesos(ev['compras_ventana'])} "
                   f"({ev['participacion_compras_pct']}% del total)"
                   + (" · ⭐ cliente clave" if ev.get("cliente_clave") else ""))
    if ev.get("facturas"):
        tabla = pd.DataFrame(ev["facturas"])
        tabla["saldo"] = tabla["saldo"].map(pesos)
        st.dataframe(tabla.rename(columns={
            "numero": "Factura", "vencimiento": "Vence", "dias_mora": "Días de mora", "saldo": "Saldo"}),
            hide_index=True, use_container_width=True)
    st.caption(f"NIT {d['nit']} · Regla cartera v{ev.get('regla_version')} · corte {ev['fecha_corte']} · "
               f"prioridad = monto {ev['componentes_prioridad']['monto']}, mora "
               f"{ev['componentes_prioridad']['mora']}, tendencia {ev['componentes_prioridad']['tendencia']}")


def acciones_y_historial(d: dict):
    if d["nivel_aprobacion"] == "gerente":
        st.warning("Esta decisión la aprueba el gerente (matriz de delegación). "
                   + ("" if ROL == "gerente" else "Puedes dejar una nota para escalarla."))
    det = decisiones.detalle(M, d["id"])
    if d["estado"] in decisiones.ABIERTAS:
        clave = f"txt_{d['id']}"
        texto = st.text_input("Comentario (qué hiciste, el ajuste o el motivo)", key=clave)
        # Datos estructurados para que Ontara aprenda: por qué se descarta o ajusta, y cómo se gestionó.
        m1, m2, m3, m4 = st.columns(4)
        motivos = {"": "(elige)"} | decisiones.MOTIVOS["descartar"] | {
            f"aj:{k}": f"Ajuste: {v}" for k, v in decisiones.MOTIVOS["ajustar"].items()}
        motivo_sel = m1.selectbox("Motivo si descartas o ajustas", list(motivos), format_func=motivos.get,
                                  key=f"mot_{d['id']}")
        canales = {"": "(elige)"} | decisiones.CANALES
        canal = m2.selectbox("Canal si la marcas ejecutada", list(canales), format_func=canales.get,
                             key=f"can_{d['id']}") or None
        fecha_comp = monto_comp = None
        if d["paquete"] == "cartera":
            fecha_comp = m3.date_input("Fecha prometida de pago", value=None, format="DD/MM/YYYY",
                                       key=f"fco_{d['id']}")
            monto_comp = m4.number_input("Monto prometido (COP)", min_value=0, value=None, step=100_000,
                                         key=f"mco_{d['id']}")
        b1, b2, b3, b4 = st.columns(4)
        acciones_ui = [(b1, "Aprobar", "aprobar"), (b2, "Ajustar", "ajustar"),
                       (b3, "Marcar ejecutada", "ejecutar"), (b4, "Descartar", "descartar")]
        autorizado = reglas.puede_aprobar(ROL, d["nivel_aprobacion"])
        if not autorizado:
            acciones_ui = [(b1, "Dejar nota", "nota"), (b3, "Marcar ejecutada", "ejecutar")]
        for col, etiqueta, accion in acciones_ui:
            if d["estado"] in decisiones.TRANSICIONES[accion][0] and col.button(etiqueta, key=f"{accion}_{d['id']}"):
                extra = {}
                if accion == "descartar" and motivo_sel and not motivo_sel.startswith("aj:"):
                    extra["motivo"] = motivo_sel
                elif accion == "ajustar" and motivo_sel.startswith("aj:"):
                    extra["motivo"] = motivo_sel[3:]
                elif accion == "ejecutar":
                    extra = {"canal": canal, "fecha_compromiso": fecha_comp,
                             "monto_compromiso": float(monto_comp) if monto_comp is not None else None}
                try:
                    decisiones.registrar_accion(M, d["id"], accion, USUARIO, texto, rol=ROL, **extra)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
    if config.ia_disponible():
        x1, x2 = st.columns(2)
        if x1.button("Explicar con Claude", key=f"exp_{d['id']}"):
            with st.spinner("Consultando la evidencia..."):
                st.info(asistente.explicar_decision(M, d["id"], USUARIO)["respuesta"])
        clave_borr = f"borr_{d['id']}"
        if d["tipo"] == "cobrar" and x2.button("Redactar mensaje de cobro", key=f"red_{d['id']}"):
            with st.spinner("Redactando..."):
                st.session_state[clave_borr] = asistente.redactar_cobro(M, d["id"], USUARIO)["respuesta"]
        if clave_borr in st.session_state:
            final = st.text_area("Borrador (edítalo antes de enviarlo)", st.session_state[clave_borr], height=260,
                                 key=f"fin_{d['id']}")
            if st.button("Guardar el texto que envié", key=f"gtx_{d['id']}"):
                try:
                    aprendizaje.guardar_texto(M, d["id"], "mensaje_cobro", st.session_state[clave_borr], final, USUARIO)
                    st.session_state.pop(clave_borr)
                    st.success("Guardado. Ontara usará tus correcciones para aprender el tono de la empresa.")
                except ValueError as e:
                    st.error(str(e))
    if det["acciones"] or det["resultados"]:
        st.markdown("**Historial**")
        for a in det["acciones"]:
            extra = [decisiones.MOTIVOS.get(a["tipo"], {}).get(a.get("motivo") or "", ""),
                     decisiones.CANALES.get(a.get("canal") or "", ""),
                     f"promete {pesos(a['monto_compromiso'])}" if a.get("monto_compromiso") else "",
                     f"para el {a['fecha_compromiso']:%d/%m/%Y}" if a.get("fecha_compromiso") else ""]
            extra = " · ".join(x for x in extra if x)
            st.write(f"- {a['fecha']:%d/%m/%Y %H:%M} · {a['usuario']} · {a['tipo']}: {a['detalle'] or ''}"
                     + (f" ({extra})" if extra else ""))
        for r in det["resultados"]:
            st.write(f"- Resultado medido: saldo vencido pasó de {pesos(r['saldo_vencido_antes'])} a "
                     f"{pesos(r['saldo_vencido_despues'])} ({pesos(r['pesos_recuperados'])} recuperados).")


# --- Impacto -------------------------------------------------------------------------------
def pagina_impacto():
    st.header("Impacto")
    pestanas = st.tabs([p.nombre for p in ACTIVOS])
    for tab, paq in zip(pestanas, ACTIVOS):
        with tab:
            {"cartera": impacto_cartera, "comercial": impacto_comercial, "margen": impacto_margen,
             "caja": impacto_caja}[paq.clave]()


def impacto_caja():
    r = caja.resumen(M)
    if not r["ultimo_reporte"]:
        st.info("Carga el reporte de cuentas por pagar (con el saldo en bancos) para activar Caja a 13 semanas.")
        return
    st.caption(f"Último reporte: {r['ultimo_reporte']:%d/%m/%Y}. Proyección de las próximas 13 semanas con la última "
               "cartera, el ritmo de ventas y los pagos comprometidos.")
    a, b, c = st.columns(3)
    a.metric("Saldo en bancos", pesos(r["saldo_bancos"] or 0))
    a.caption("Al corte del último reporte")
    b.metric("Semanas bajo el mínimo", r["semanas_rojo_hoy"],
             delta=f"{r['semanas_rojo_hoy'] - r['semanas_rojo_base']:+d} vs. primera proyección", delta_color="inverse")
    c.metric("Punto más bajo", pesos(r["saldo_minimo_hoy"]))
    grafico_caja(r["proyeccion"], r["caja_minima"])
    x, y, z = st.columns(3)
    x.metric("Déficit cubierto en decisiones atendidas", pesos(r["deficit_cubierto_atendidas"]))
    y.metric("Pagos a proveedores aplazados", pesos(r["pagos_aplazados"]))
    pre = r["precision"]
    if pre:
        z.metric("Precisión de la proyección", f"{100 - pre['error_pct']:.0f}%" if pre["error_pct"] is not None else "-",
                 help=f"El reporte anterior proyectaba {pesos(pre['proyectado'])} para el {pre['fecha']:%d/%m/%Y}; "
                      f"el saldo real fue {pesos(pre['real'])}.")
    else:
        z.metric("Precisión de la proyección", "Desde el 2.º reporte")
    with st.expander("Detalle semana a semana"):
        t = r["proyeccion"].copy()
        for col in ["cobros_cartera", "cobros_ventas_nuevas", "pagos_proveedores", "compras_nuevas", "gastos_fijos",
                    "saldo_final"]:
            t[col] = t[col].map(pesos)
        st.dataframe(t.rename(columns={
            "semana": "Semana", "inicio": "Desde", "fin": "Hasta", "cobros_cartera": "Cobros de cartera",
            "cobros_ventas_nuevas": "Cobros de ventas nuevas", "pagos_proveedores": "Cuentas por pagar",
            "compras_nuevas": "Compras nuevas", "gastos_fijos": "Gastos fijos", "saldo_final": "Saldo final"}),
            hide_index=True, use_container_width=True)


def impacto_margen():
    r = margen.resumen(M)
    if not r["ultimo_reporte"]:
        st.info("Carga el reporte de ventas por producto para activar Margen.")
        return
    st.caption(f"Último reporte: {r['ultimo_reporte']:%d/%m/%Y}. Margen bruto de los últimos 90 días frente al del "
               "primer reporte cargado (línea base).")
    a, b, c = st.columns(3)
    a.metric("Margen bruto (90 días)", f"{r['margen_hoy_pct']:.1f}%",
             delta=f"{r['puntos']:+.1f} pts vs. línea base ({r['margen_base_pct']:.1f}%)")
    b.metric("Utilidad adicional en 90 días", pesos(r["utilidad_adicional_90d"]))
    c.metric("Utilidad en juego en decisiones abiertas", pesos(r["utilidad_en_juego_abiertas"]))
    st.caption(f"Decisiones medidas: {r['decisiones_medidas']} · utilidad adicional en decisiones atendidas "
               f"{pesos(r['utilidad_adicional_atendidas'])} · no atendidas {pesos(r['utilidad_adicional_no_atendidas'])}")
    mensual = margen.margen_mensual(M)
    if len(mensual) > 1:
        fig = px.line(mensual, x="mes", y="margen_pct", markers=True,
                      labels={"mes": "Mes", "margen_pct": "Margen bruto (%)"})
        minimo = reglas.regla_activa(M, "margen")["parametros"]["margen_minimo_pct"]
        fig.add_hline(y=minimo, line_dash="dash", annotation_text="Margen mínimo")
        st.plotly_chart(fig, use_container_width=True)


def impacto_comercial():
    r = comercial.resumen(M)
    if not r["ultimo_reporte"]:
        st.info("Carga el reporte de ventas para activar Foco comercial.")
        return
    st.caption(f"Último reporte de ventas: {r['ultimo_reporte']:%d/%m/%Y}. Los resultados se miden con el siguiente "
               "reporte: si el cliente alertado volvió a comprar y cuánto.")
    a, b, c = st.columns(3)
    a.metric("Alertas medidas", r["alertas_medidas"])
    b.metric("Clientes atendidos que volvieron", r["volvieron_atendidas"])
    c.metric("Ventas de clientes atendidos desde la alerta", pesos(r["ventas_recuperadas_atendidas"]))
    st.subheader("Efecto frente al grupo de control")
    if not r["n_control"] or r["efecto_estimado"] is None:
        st.info("Aún no hay resultados del grupo de control. Se miden desde el segundo reporte de ventas.")
        return
    x, y, z = st.columns(3)
    x.metric("Volvieron a comprar con la bandeja", f"{r['pct_volvio_tratamiento']}%", help=f"{r['n_tratamiento']} alertas")
    y.metric("Volvieron sin gestión (control)", f"{r['pct_volvio_control']}%", help=f"{r['n_control']} alertas")
    z.metric("Ventas adicionales atribuibles", pesos(r["efecto_estimado"]))
    if not r["muestra_suficiente"]:
        st.caption("Todavía hay pocos casos (menos de 10 por grupo): la diferencia puede ser azar.")


def impacto_cartera():
    r = impacto.resumen(M)
    if not r["linea_base"]:
        st.info("Todavía no hay línea base. Se crea automáticamente con la primera carga.")
        return
    base, hoy = r["linea_base"], r["actual"]
    a, b, c, d = st.columns(4)
    a.metric("Cartera vencida", pesos(hoy["cartera_vencida"]),
             delta=pesos(hoy["cartera_vencida"] - base["cartera_vencida"]), delta_color="inverse")
    b.metric("% vencida", f"{hoy['pct_vencida']}%", delta=f"{hoy['pct_vencida'] - base['pct_vencida']:.1f} pts",
             delta_color="inverse")
    c.metric("Mora promedio (ponderada)", f"{hoy['mora_promedio_dias']} días",
             delta=f"{hoy['mora_promedio_dias'] - base['mora_promedio_dias']:.1f}", delta_color="inverse")
    d.metric("Recuperado en decisiones atendidas", pesos(r["pesos_recuperados_atendidas"]))
    st.caption(f"Recuperado en decisiones no atendidas: {pesos(r['pesos_recuperados_no_atendidas'])} · "
               f"decisiones medidas: {r['decisiones_medidas']} · acciones registradas: {r['acciones_registradas']}")

    st.subheader("Caja para crecer")
    caja = r["caja"]
    if caja["pesos"] is None:
        st.info("Faltan datos de ventas para calcular los días de cartera. Registra las ventas mensuales en Reglas.")
    else:
        x, y, z = st.columns(3)
        x.metric("Días de cartera", f"{caja['dias_hoy']:.0f}",
                 delta=f"{-caja['dias_ganados']:+.1f} vs. línea base ({caja['dias_base']:.0f})", delta_color="inverse")
        y.metric("Caja liberada vs. línea base", pesos(caja["pesos"]))
        z.metric("Ventas diarias usadas", "Contabilidad" if caja["fuente_ventas"] == "contabilidad" else "Estimadas")
        if caja["fuente_ventas"] != "contabilidad":
            st.caption("Las ventas se estiman con las facturas emitidas en los últimos 30 días de cada corte. "
                       "Para una cifra más estable, registra las ventas mensuales de contabilidad en Reglas.")

    st.subheader("Efecto de Ontara frente al grupo de control")
    ctl = r["control"]
    if not ctl["n_control"] or ctl["efecto_estimado"] is None:
        st.info("Aún no hay resultados del grupo de control. Se miden desde el segundo corte.")
    else:
        x, y, z = st.columns(3)
        x.metric("Recuperación con la bandeja", f"{ctl['tasa_tratamiento_pct']}%",
                 help=f"{ctl['n_tratamiento']} decisiones")
        y.metric("Recuperación del grupo de control", f"{ctl['tasa_control_pct']}%",
                 help=f"{ctl['n_control']} decisiones que no se mostraron")
        z.metric("Recuperación adicional atribuible", pesos(ctl["efecto_estimado"]))
        if not ctl["muestra_suficiente"]:
            st.caption("Todavía hay pocos casos (menos de 10 por grupo): la diferencia puede ser azar.")
    hist = impacto.historial(M)
    if len(hist) > 1:
        fig = px.line(hist, x="fecha_corte", y="cartera_vencida", markers=True,
                      labels={"fecha_corte": "Corte", "cartera_vencida": "Cartera vencida (COP)"})
        fig.add_hline(y=base["cartera_vencida"], line_dash="dash", annotation_text="Línea base")
        st.plotly_chart(fig, use_container_width=True)
    for col in ("cartera_total", "cartera_vencida"):
        hist[col] = hist[col].map(pesos)
    st.dataframe(hist.rename(columns={"fecha_corte": "Corte", "cartera_total": "Cartera total",
                                      "cartera_vencida": "Vencida", "pct_vencida": "% vencida",
                                      "mora_promedio_dias": "Mora prom. (días)",
                                      "clientes_con_mora": "Clientes en mora", "clientes": "Clientes",
                                      "dias_cartera": "Días de cartera"})
                 .drop(columns=["carga_id", "ventas_diarias_estimadas"], errors="ignore"),
                 hide_index=True, use_container_width=True)


# --- Asistente -------------------------------------------------------------------------------
def pagina_asistente():
    st.header("Pregúntale a Ontara")
    st.caption("Claude responde solo con cifras que ya calcularon las reglas. "
               + ("Los nombres y NIT de clientes se envían anonimizados." if config.anonimizar_ia()
                  else "⚠ La anonimización está desactivada."))
    if not config.ia_disponible():
        st.warning("Configura ANTHROPIC_API_KEY para activar el asistente.")
        return
    hist = st.session_state.setdefault("chat", [])
    for t in hist:
        st.chat_message(t["role"]).write(t["content"])
    if pregunta := st.chat_input("Ej.: ¿A quién debo llamar primero hoy?"):
        st.chat_message("user").write(pregunta)
        with st.chat_message("assistant"), st.spinner("Consultando..."):
            r = asistente.preguntar(M, pregunta, USUARIO, historial=hist[-10:])
            st.write(r["respuesta"])
            if r["herramientas"]:
                st.caption("Consultó: " + ", ".join(h["herramienta"] for h in r["herramientas"]))
        hist += [{"role": "user", "content": pregunta}, {"role": "assistant", "content": r["respuesta"]}]


# --- Reglas -----------------------------------------------------------------------------------
def pagina_reglas():
    st.header("Reglas")
    paq = st.segmented_control("Paquete", [p.nombre for p in ACTIVOS], default=ACTIVOS[0].nombre) or ACTIVOS[0].nombre
    paq = next(p for p in ACTIVOS if p.nombre == paq)
    if paq.clave == "cartera":
        reglas_cartera()
    else:
        reglas_genericas(paq)


def _guardar_y_recalcular(n: dict, nota: str, paq):
    try:
        reglas.guardar_parametros(M, n, USUARIO, nota, paquete=paq.clave)
        ultima = decisiones.ultima_carga(M, paq.reporte)
        if ultima:
            decisiones.recalcular(M, ultima)
        st.success("Reglas guardadas y bandeja recalculada.")
    except ValueError as e:
        st.error(str(e))


def reglas_genericas(paq):
    """Formulario a partir del YAML del paquete: sirve para cualquier paquete nuevo sin escribir pantalla."""
    activa = reglas.regla_activa(M, paq.clave)
    p = activa["parametros"]
    etiquetas = reglas.descripciones(paq.clave)
    st.caption(f"{paq.nombre} · versión activa v{activa['version']}. Cada cambio guarda una versión nueva.")
    editable = ROL == "gerente"
    if not editable:
        st.info("Solo el rol gerente puede cambiar los parámetros.")
    with st.form(f"reglas_{paq.clave}"):
        n, cols = {}, st.columns(2)
        editables = [k for k, v in p.items() if isinstance(v, (int, float))]
        for i, k in enumerate(editables):
            v, etiqueta = p[k], etiquetas.get(k, k)
            if isinstance(v, bool):
                n[k] = cols[i % 2].checkbox(etiqueta, v)
            elif isinstance(v, int):
                n[k] = cols[i % 2].number_input(etiqueta, 0, value=v, step=max(1, 10 ** (len(str(v)) - 2)))
            else:
                n[k] = cols[i % 2].number_input(etiqueta, 0.0, value=float(v), step=0.1)
        nota = st.text_input("¿Por qué el cambio?")
        if st.form_submit_button("Guardar nueva versión y recalcular", disabled=not editable):
            _guardar_y_recalcular(n, nota, paq)


def reglas_cartera():
    activa = reglas.regla_activa(M)
    p = activa["parametros"]
    st.caption(f"Versión activa: v{activa['version']}. Cada cambio guarda una versión nueva y las decisiones "
               "registran con qué versión se generaron.")
    editable = ROL == "gerente"
    if not editable:
        st.info("Solo el rol gerente puede cambiar los parámetros.")
    with st.form("reglas"):
        a, b = st.columns(2)
        n = {}
        n["dias_gracia"] = a.number_input("Días de gracia después del vencimiento", 0, 60, int(p["dias_gracia"]))
        n["umbral_cobro_minimo"] = b.number_input("Saldo vencido mínimo para generar cobro (COP)", 0,
                                                  value=int(p["umbral_cobro_minimo"]), step=100_000)
        n["umbral_congelar_dias"] = a.number_input("Días de mora para frenar el crédito", 1, 365,
                                                   int(p["umbral_congelar_dias"]))
        n["dias_compra_reciente"] = b.number_input("'Sigue comprando' = facturó en los últimos (días)", 1, 120,
                                                   int(p["dias_compra_reciente"]))
        n["mora_maxima_dias"] = a.number_input("Mora a partir de la cual pesa al máximo (días)", 30, 720,
                                               int(p["mora_maxima_dias"]))
        n["congelar_por_sobrecupo"] = b.checkbox("Frenar crédito si el saldo supera el cupo",
                                                 bool(p["congelar_por_sobrecupo"]))
        st.markdown("**Pesos de la prioridad** (deben sumar 1)")
        x, y, z = st.columns(3)
        n["peso_monto"] = x.number_input("Monto vencido", 0.0, 1.0, float(p["peso_monto"]), 0.05)
        n["peso_mora"] = y.number_input("Días de mora", 0.0, 1.0, float(p["peso_mora"]), 0.05)
        n["peso_tendencia"] = z.number_input("Tendencia", 0.0, 1.0, float(p["peso_tendencia"]), 0.05)
        from ontara.db import clientes
        with M.connect() as cx:
            directorio = {r.nit: r.nombre for r in cx.execute(select(clientes.c.nit, clientes.c.nombre))}
        st.markdown("**Ampliar cupo a buenos pagadores**")
        a2, b2 = st.columns(2)
        n["ampliar_activo"] = a2.checkbox("Recomendar ampliar cupo", bool(p["ampliar_activo"]))
        n["ampliar_uso_minimo"] = b2.number_input("Uso del cupo a partir del cual recomendar (0,7 = 70%)",
                                                  0.1, 2.0, float(p["ampliar_uso_minimo"]), 0.05)
        n["ampliar_mora_maxima_dias"] = a2.number_input("Mora máxima para ser buen pagador (días)", 0, 60,
                                                        int(p["ampliar_mora_maxima_dias"]))
        n["ampliar_pct_sugerido"] = b2.number_input("Aumento sugerido (fracción del cupo)", 0.05, 2.0,
                                                    float(p["ampliar_pct_sugerido"]), 0.05)
        n["ampliar_maximo_cop"] = a2.number_input("Tope del aumento (COP)", 0, value=int(p["ampliar_maximo_cop"]),
                                                  step=1_000_000)
        st.markdown("**Valor del cliente y delegación**")
        a3, b3 = st.columns(2)
        n["pct_clientes_clave"] = a3.number_input("Clientes clave = los que suman este % de las compras", 1, 100,
                                                  int(p["pct_clientes_clave"]))
        n["meses_valor_cliente"] = b3.number_input("Meses de compras para el valor del cliente", 1, 36,
                                                   int(p["meses_valor_cliente"]))
        n["aprobacion_lider_max_cop"] = a3.number_input("El líder aprueba hasta (COP)", 0,
                                                        value=int(p["aprobacion_lider_max_cop"]), step=1_000_000)
        n["frenar_clave_requiere_gerente"] = b3.checkbox("Frenar a un cliente clave lo aprueba el gerente",
                                                         bool(p["frenar_clave_requiere_gerente"]))
        st.markdown("**Medición**")
        a4, b4 = st.columns(2)
        n["pct_grupo_control"] = a4.number_input("Grupo de control (% de cobros elegibles que no se muestran)",
                                                 0, 50, int(p["pct_grupo_control"]))
        n["control_monto_maximo"] = b4.number_input("Solo van a control saldos vencidos hasta (COP)", 0,
                                                    value=int(p["control_monto_maximo"]), step=1_000_000)
        n["ventas_mensuales_cop"] = a4.number_input("Ventas a crédito del mes según contabilidad (0 = estimar)", 0,
                                                    value=int(p["ventas_mensuales_cop"]), step=10_000_000)
        n["clientes_excepcion"] = st.multiselect(
            "Clientes estratégicos (no generan decisiones)", list(directorio),
            default=[c for c in p["clientes_excepcion"] if c in directorio],
            format_func=lambda nit: f"{directorio[nit]} ({nit})", placeholder="Elige clientes")
        nota = st.text_input("¿Por qué el cambio?")
        if st.form_submit_button("Guardar nueva versión y recalcular", disabled=not editable):
            _guardar_y_recalcular(n, nota, paquetes.obtener("cartera"))


# --- Aprendizaje -----------------------------------------------------------------------------------
def pagina_aprendizaje():
    st.header("Aprendizaje")
    st.write("Ontara guarda en cada decisión las variables con que se detectó, lo que hizo el equipo y lo que "
             "pasó después. Con eso podrá proponer mejores umbrales (etapa 1) y entrenar modelos de pago y "
             "abandono (etapa 2). Todavía no cambia nada por su cuenta: aquí se ve cuánto falta.")
    st.subheader("¿Hay datos suficientes para aprender?")
    prep = pd.DataFrame(aprendizaje.preparacion(M))
    prep.insert(0, "", prep.pop("listo").map({True: "🟢", False: "⚪"}))
    st.dataframe(prep.rename(columns={"indicador": "Indicador", "valor": "Hoy", "meta": "Meta orientativa",
                                      "para": "Sirve para"}), hide_index=True, use_container_width=True)
    st.caption("Las metas son puntos de partida del diseño, no medidas de esta empresa; se ajustan en el piloto. "
               "Lo que más acelera: cargar el reporte de recaudos con 12 a 24 meses de historia.")

    st.subheader("Qué recomendaciones descarta el equipo")
    desc = aprendizaje.descartes_por_tipo(M)
    if desc.empty:
        st.info("Todavía no hay decisiones.")
    else:
        desc["tipo"] = desc["tipo"].map(lambda t: TIPO.get(t, t))
        st.dataframe(desc.drop(columns="paquete").rename(columns={
            "tipo": "Decisión", "mostradas": "Mostradas", "descartadas": "Descartadas",
            "tasa_descarte_pct": "% descartadas", "motivo_principal": "Motivo más común"}),
            hide_index=True, use_container_width=True)
        st.caption("Una tasa alta de descarte con un mismo motivo indica que la regla genera ruido para ese caso.")

    st.subheader("Datos de entrenamiento")
    ds = aprendizaje.dataset(M)
    if ds.empty:
        st.info("Se llenará a medida que se carguen cortes y se gestionen decisiones.")
        return
    st.write(f"{len(ds)} decisiones · {sum(c.startswith('v_') for c in ds.columns)} variables · resultados a 30, "
             "60 y 90 días cuando ya pasó el plazo. Los clientes van con código, sin NIT ni nombre.")
    st.dataframe(ds.head(50), hide_index=True, use_container_width=True)
    if ROL == "gerente":
        st.download_button("Descargar CSV", ds.to_csv(index=False).encode("utf-8"),
                           file_name=f"ontara_aprendizaje_{date.today():%Y%m%d}.csv", mime="text/csv")


# --- Accesos (solo gerente) ---------------------------------------------------------------------
def pagina_accesos():
    st.header("Bitácora de accesos")
    st.caption("Quién entró, cuándo y cómo. Incluye intentos fallidos y cuentas no autorizadas.")
    filas = acceso.bitacora(M)
    if not filas:
        st.info("Todavía no hay accesos registrados.")
        return
    st.dataframe(pd.DataFrame(filas)[["fecha", "usuario", "evento", "metodo", "detalle"]].rename(columns={
        "fecha": "Fecha (UTC)", "usuario": "Usuario", "evento": "Evento", "metodo": "Método", "detalle": "Detalle"}),
        hide_index=True, use_container_width=True)


{"Aprendizaje": pagina_aprendizaje, "Cargar datos": pagina_cargar, "Accesos": pagina_accesos, "Bandeja de decisiones": pagina_bandeja, "Impacto": pagina_impacto,
 "Asistente": pagina_asistente, "Reglas": pagina_reglas}[pagina]()
