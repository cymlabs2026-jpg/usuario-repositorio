"""Ontara Cartera · aplicación web (Streamlit).

Ejecutar:  streamlit run app/streamlit_app.py
La lógica vive en el paquete `ontara`; esta capa solo muestra y captura.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ontara import asistente, calidad, config, decisiones, impacto, ingesta, ontologia, reglas  # noqa: E402
from ontara.db import cargas, crear_motor  # noqa: E402
from sqlalchemy import select  # noqa: E402

st.set_page_config(page_title="Ontara · Cartera", page_icon="◆", layout="wide")


@st.cache_resource
def motor():
    return crear_motor()


M = motor()
COLOR = {"verde": "🟢", "amarillo": "🟡", "rojo": "🔴"}
TIPO = {"cobrar": "Cobrar", "congelar_credito": "Frenar crédito"}
ETIQUETAS = {"nit": "NIT", "cliente": "Cliente", "numero": "Número de factura", "fecha_emision": "Fecha de emisión",
             "fecha_vencimiento": "Fecha de vencimiento", "saldo": "Saldo", "valor": "Valor", "vendedor": "Vendedor",
             "cupo": "Cupo de crédito"}
ESTADO = {"pendiente": "Pendiente", "aprobada": "Aprobada", "ajustada": "Ajustada",
          "ejecutada": "Ejecutada", "descartada": "Descartada", "cerrada": "Cerrada"}


def pesos(x) -> str:
    return "$" + f"{(x or 0):,.0f}".replace(",", ".")


# --- Acceso -----------------------------------------------------------------------
def usuarios() -> dict:
    """ONTARA_USUARIOS='{"maria": {"clave_sha256": "...", "rol": "lider"}}'. Sin variable = modo demo."""
    crudo = os.environ.get("ONTARA_USUARIOS")
    return json.loads(crudo) if crudo else {}


def login() -> tuple[str, str]:
    if "usuario" in st.session_state:
        return st.session_state.usuario, st.session_state.rol
    lista = usuarios()
    if not lista:
        st.session_state.usuario, st.session_state.rol = "demo", "gerente"
        return "demo", "gerente"
    st.title("Ontara · Cartera")
    with st.form("login"):
        u = st.text_input("Usuario")
        c = st.text_input("Clave", type="password")
        if st.form_submit_button("Entrar"):
            datos = lista.get(u)
            if datos and hashlib.sha256(c.encode()).hexdigest() == datos["clave_sha256"]:
                st.session_state.usuario, st.session_state.rol = u, datos.get("rol", "lider")
                st.rerun()
            st.error("Usuario o clave incorrectos.")
    st.stop()


USUARIO, ROL = login()

with st.sidebar:
    st.markdown(f"### ◆ Ontara\n**{config.nombre_empresa()}**")
    pagina = st.radio("Ir a", ["Bandeja de decisiones", "Cargar datos", "Impacto", "Asistente", "Reglas"],
                      label_visibility="collapsed")
    st.caption(f"Sesión: {USUARIO} · {ROL}")
    if not usuarios():
        st.warning("Modo demostración: sin login. Configura ONTARA_USUARIOS antes de usar datos reales.")


# --- Cargar datos ---------------------------------------------------------------------
def pagina_cargar():
    st.header("Cargar datos de cartera")
    st.write("Sube el reporte de cartera por edades o de facturas pendientes que exportas de tu ERP "
             "(Siigo, World Office, SAP) o tu Excel. Ontara reconoce las columnas y revisa la calidad "
             "antes de guardar nada.")
    corte = st.date_input("Fecha de corte del reporte", value=date.today(), format="DD/MM/YYYY")
    archivo = st.file_uploader("Archivo Excel o CSV", type=["xlsx", "xls", "csv"])
    if not archivo:
        st.info("También puedes probar con los archivos de `datos_ejemplo/`.")
        return
    crudo = ingesta.leer_archivo(archivo.getvalue(), archivo.name)
    st.caption(f"{len(crudo)} filas · {len(crudo.columns)} columnas")

    plantilla = ontologia.cargar_plantilla()
    sugerido = ontologia.sugerir_mapeo(list(crudo.columns), plantilla)
    st.subheader("1. Columnas")
    opciones = ["(no está)"] + list(crudo.columns)
    mapeo = {}
    cols = st.columns(3)
    for i, campo in enumerate(plantilla.campos_factura):
        etiqueta = ETIQUETAS.get(campo.nombre, campo.nombre) + (" *" if campo.requerido else "")
        actual = sugerido.get(campo.nombre, "(no está)")
        sel = cols[i % 3].selectbox(etiqueta, opciones, index=opciones.index(actual), key=f"map_{campo.nombre}")
        if sel != "(no está)":
            mapeo[campo.nombre] = sel

    datos = ingesta.convertir(crudo, mapeo, plantilla)
    informe, validas = calidad.evaluar(datos, mapeo, plantilla)
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
        repetida = cx.execute(select(cargas.c.id).where(cargas.c.fecha_corte == corte)).first()
    if repetida:
        st.warning("Ya hay una carga con esta fecha de corte. Si continúas se guardará otra versión del mismo corte.")

    st.subheader("3. Guardar y recalcular")
    if st.button("Cargar y generar decisiones", type="primary", disabled=not informe.puede_cargar):
        r = decisiones.registrar_carga(M, validas, informe, mapeo, corte, archivo.name, USUARIO)
        st.success(f"Carga #{r['carga_id']} guardada. Decisiones nuevas: {r['decisiones_nuevas']}, "
                   f"actualizadas: {r['decisiones_actualizadas']}, cerradas: {r['decisiones_cerradas']}. "
                   f"Resultados medidos: {r['resultados_medidos']}.")
        if r["linea_base_creada"]:
            st.info("Esta es la primera carga: quedó guardada como línea base para medir el impacto.")


# --- Bandeja -----------------------------------------------------------------------------
def pagina_bandeja():
    st.header("Bandeja de decisiones")
    c1, c2 = st.columns([2, 1])
    filtro = c1.segmented_control("Estado", ["Abiertas", "Pendientes", "Todas"], default="Abiertas")
    tipo = c2.selectbox("Tipo", ["Todos", "Cobrar", "Frenar crédito"])
    estados = {"Abiertas": decisiones.ABIERTAS, "Pendientes": ("pendiente",), "Todas": None}[filtro or "Abiertas"]
    tipo_k = {"Todos": None, "Cobrar": "cobrar", "Frenar crédito": "congelar_credito"}[tipo]
    lista = decisiones.listar(M, estados, tipo_k)
    if not lista:
        st.info("No hay decisiones con este filtro. Carga un corte de cartera para empezar.")
        return
    total = sum(d["evidencia"]["saldo_vencido"] for d in lista if d["tipo"] == "cobrar")
    st.caption(f"{len(lista)} decisiones · {pesos(total)} vencidos en las de cobro")
    for d in lista:
        ev = d["evidencia"]
        icono = "🔴" if d["prioridad"] >= 70 else "🟡" if d["prioridad"] >= 40 else "⚪"
        titulo = (f"{icono} {d['prioridad']:.0f} · {d['cliente']} · {TIPO[d['tipo']]} · "
                  f"{pesos(ev['saldo_vencido'])} vencidos · {ev['dias_mora_max']} días · {ESTADO[d['estado']]}")
        with st.expander(titulo):
            tarjeta(d)


def tarjeta(d: dict):
    ev = d["evidencia"]
    st.markdown(f"**Decisión #{d['id']}: {d['recomendacion']}**")
    st.write(d["motivo"])
    a, b, c, e = st.columns(4)
    a.metric("Saldo total", pesos(ev["saldo_total"]))
    b.metric("Saldo vencido", pesos(ev["saldo_vencido"]))
    c.metric("Mora máxima", f"{ev['dias_mora_max']} días")
    e.metric("Cupo", pesos(ev["cupo"]) if ev.get("cupo") else "Sin dato")
    if ev.get("facturas"):
        tabla = pd.DataFrame(ev["facturas"])
        tabla["saldo"] = tabla["saldo"].map(pesos)
        st.dataframe(tabla.rename(columns={
            "numero": "Factura", "vencimiento": "Vence", "dias_mora": "Días de mora", "saldo": "Saldo"}),
            hide_index=True, use_container_width=True)
    st.caption(f"NIT {d['nit']} · Regla cartera v{ev.get('regla_version')} · corte {ev['fecha_corte']} · "
               f"prioridad = monto {ev['componentes_prioridad']['monto']}, mora "
               f"{ev['componentes_prioridad']['mora']}, tendencia {ev['componentes_prioridad']['tendencia']}")

    det = decisiones.detalle(M, d["id"])
    if d["estado"] in decisiones.ABIERTAS:
        clave = f"txt_{d['id']}"
        texto = st.text_input("Comentario (qué hiciste, el ajuste o el motivo)", key=clave)
        b1, b2, b3, b4 = st.columns(4)
        acciones_ui = [(b1, "Aprobar", "aprobar"), (b2, "Ajustar", "ajustar"),
                       (b3, "Marcar ejecutada", "ejecutar"), (b4, "Descartar", "descartar")]
        for col, etiqueta, accion in acciones_ui:
            if d["estado"] in decisiones.TRANSICIONES[accion][0] and col.button(etiqueta, key=f"{accion}_{d['id']}"):
                try:
                    decisiones.registrar_accion(M, d["id"], accion, USUARIO, texto)
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
    st.header("Impacto frente a la línea base")
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
                                      "clientes_con_mora": "Clientes en mora", "clientes": "Clientes"}).drop(columns=["carga_id"]),
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
    st.header("Reglas de cartera")
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
        n["clientes_excepcion"] = st.multiselect(
            "Clientes estratégicos (no generan decisiones)", list(directorio),
            default=[c for c in p["clientes_excepcion"] if c in directorio],
            format_func=lambda nit: f"{directorio[nit]} ({nit})", placeholder="Elige clientes")
        nota = st.text_input("¿Por qué el cambio?")
        if st.form_submit_button("Guardar nueva versión y recalcular", disabled=not editable):
            try:
                reglas.guardar_parametros(M, n, USUARIO, nota)
                with M.connect() as cx:
                    ultima = cx.execute(select(cargas.c.id).order_by(cargas.c.fecha_corte.desc(),
                                                                     cargas.c.id.desc()).limit(1)).scalar()
                if ultima:
                    decisiones.recalcular(M, ultima)
                st.success("Reglas guardadas y bandeja recalculada.")
            except ValueError as e:
                st.error(str(e))


{"Cargar datos": pagina_cargar, "Bandeja de decisiones": pagina_bandeja, "Impacto": pagina_impacto,
 "Asistente": pagina_asistente, "Reglas": pagina_reglas}[pagina]()
