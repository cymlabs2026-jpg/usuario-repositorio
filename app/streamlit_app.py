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

from ontara import acceso, asistente, calidad, comercial, paquetes, config, decisiones, impacto, ingesta, ontologia, reglas  # noqa: E402
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
ETIQUETAS = {"fecha": "Fecha de la factura", "nit": "NIT", "cliente": "Cliente", "numero": "Número de factura", "fecha_emision": "Fecha de emisión",
             "fecha_vencimiento": "Fecha de vencimiento", "saldo": "Saldo", "valor": "Valor", "vendedor": "Vendedor",
             "cupo": "Cupo de crédito"}
ESTADO = {"pendiente": "Pendiente", "aprobada": "Aprobada", "ajustada": "Ajustada",
          "ejecutada": "Ejecutada", "descartada": "Descartada", "cerrada": "Cerrada"}


def pesos(x) -> str:
    return "$" + f"{(x or 0):,.0f}".replace(",", ".")


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
    paginas = ["Bandeja de decisiones", "Cargar datos", "Impacto", "Asistente", "Reglas"]
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
    reportes = list(dict.fromkeys(p.reporte for p in ACTIVOS))
    tipo = st.radio("¿Qué reporte vas a cargar?", reportes, format_func=lambda t: paquetes.REPORTES[t]["nombre"],
                    horizontal=True)
    usa = ", ".join(p.nombre for p in paquetes.de_reporte(tipo))
    st.write(("Sube el reporte de cartera por edades o de facturas pendientes" if tipo == "cartera" else
              "Sube el reporte de ventas de los últimos 12 meses (todas las facturas, incluidas las pagadas)")
             + " que exportas de tu ERP (Siigo, World Office, SAP) o tu Excel. Ontara reconoce las columnas y "
             f"revisa la calidad antes de guardar nada. Lo usa: {usa}.")
    corte = st.date_input("Fecha de corte del reporte", value=date.today(), format="DD/MM/YYYY")
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
    evaluar = calidad.evaluar if tipo == "cartera" else calidad.evaluar_ventas
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
    if st.button("Cargar y generar decisiones", type="primary", disabled=not informe.puede_cargar):
        r = decisiones.registrar_carga(M, validas, informe, mapeo, corte, archivo.name, USUARIO, tipo)
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
    st.caption(f"{len(lista)} decisiones · {pesos(total)} vencidos en las de cobro · "
               f"{pesos(cupo)} de cupo adicional sugerido · {pesos(riesgo)} de ventas trimestrales en riesgo "
               f"en clientes que se están enfriando")
    for d in lista:
        ev = d["evidencia"]
        icono = "🟢" if d["tipo"] in ("ampliar_cupo", "crecer_cliente") else "🔴" if d["prioridad"] >= 70 else \
            "🟡" if d["prioridad"] >= 40 else "⚪"
        clave = " ⭐" if ev.get("cliente_clave") else ""
        gerente = " · aprueba gerente" if d["nivel_aprobacion"] == "gerente" else ""
        if d["tipo"] == "ampliar_cupo":
            detalle_tipo = f"{ev.get('uso_cupo_pct', 0):.0f}% del cupo usado"
        elif d["tipo"] == "recuperar_cliente":
            detalle_tipo = f"{ev['dias_sin_comprar']} días sin comprar · 90 días {-ev['caida_pct']:+.0f}%"
        elif d["tipo"] == "crecer_cliente":
            detalle_tipo = f"90 días {pesos(ev['ventas_90d'])}"
        else:
            detalle_tipo = f"{pesos(ev['saldo_vencido'])} vencidos · {ev['dias_mora_max']} días"
        titulo = (f"{icono} {d['prioridad']:.0f} · {d['cliente']}{clave} · {TIPO[d['tipo']]} · "
                  f"{detalle_tipo} · {ESTADO[d['estado']]}{gerente}")
        with st.expander(titulo):
            tarjeta(d)


def tarjeta(d: dict):
    ev = d["evidencia"]
    st.markdown(f"**Decisión #{d['id']}: {d['recomendacion']}**")
    st.write(d["motivo"])
    if d["paquete"] == "comercial":
        tarjeta_comercial(d, ev)
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
        b1, b2, b3, b4 = st.columns(4)
        acciones_ui = [(b1, "Aprobar", "aprobar"), (b2, "Ajustar", "ajustar"),
                       (b3, "Marcar ejecutada", "ejecutar"), (b4, "Descartar", "descartar")]
        autorizado = reglas.puede_aprobar(ROL, d["nivel_aprobacion"])
        if not autorizado:
            acciones_ui = [(b1, "Dejar nota", "nota"), (b3, "Marcar ejecutada", "ejecutar")]
        for col, etiqueta, accion in acciones_ui:
            if d["estado"] in decisiones.TRANSICIONES[accion][0] and col.button(etiqueta, key=f"{accion}_{d['id']}"):
                try:
                    decisiones.registrar_accion(M, d["id"], accion, USUARIO, texto, rol=ROL)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
    if config.ia_disponible():
        x1, x2 = st.columns(2)
        if x1.button("Explicar con Claude", key=f"exp_{d['id']}"):
            with st.spinner("Consultando la evidencia..."):
                st.info(asistente.explicar_decision(M, d["id"], USUARIO)["respuesta"])
        if d["tipo"] == "cobrar" and x2.button("Redactar mensaje de cobro", key=f"red_{d['id']}"):
            with st.spinner("Redactando..."):
                st.text_area("Borrador", asistente.redactar_cobro(M, d["id"], USUARIO)["respuesta"], height=260)
    if det["acciones"] or det["resultados"]:
        st.markdown("**Historial**")
        for a in det["acciones"]:
            st.write(f"- {a['fecha']:%d/%m/%Y %H:%M} · {a['usuario']} · {a['tipo']}: {a['detalle'] or ''}")
        for r in det["resultados"]:
            st.write(f"- Resultado medido: saldo vencido pasó de {pesos(r['saldo_vencido_antes'])} a "
                     f"{pesos(r['saldo_vencido_despues'])} ({pesos(r['pesos_recuperados'])} recuperados).")


# --- Impacto -------------------------------------------------------------------------------
def pagina_impacto():
    st.header("Impacto")
    pestanas = st.tabs([p.nombre for p in ACTIVOS])
    for tab, paq in zip(pestanas, ACTIVOS):
        with tab:
            impacto_cartera() if paq.clave == "cartera" else impacto_comercial()


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


{"Cargar datos": pagina_cargar, "Accesos": pagina_accesos, "Bandeja de decisiones": pagina_bandeja, "Impacto": pagina_impacto,
 "Asistente": pagina_asistente, "Reglas": pagina_reglas}[pagina]()
