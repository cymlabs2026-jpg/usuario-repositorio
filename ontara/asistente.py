"""Piso 5: asistente Claude con herramientas de SOLO LECTURA.

Regla de oro: Claude no calcula ni inventa cifras. Solo puede consultar los
resultados que ya calcularon las reglas (herramientas de abajo), explicarlos
y redactar textos. No tiene ninguna herramienta que escriba en la base.

Ley 1581: con ONTARA_ANONIMIZAR_IA=1 (por defecto) los nombres y NIT de los
clientes se reemplazan por alias antes de salir hacia la API, y se restauran
en la respuesta que ve el usuario.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Any, Callable

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from . import caja, comercial, config, decisiones, despacho, impacto, inventario, margen
from .db import clientes, consultas_ia

SISTEMA = """Eres el asistente de Ontara, una herramienta de decisiones de cartera para pymes colombianas.
Hablas con el líder de cartera o el gerente de la empresa, en español claro y directo.

Reglas que no puedes romper:
- Toda cifra que menciones (pesos, días, porcentajes, cantidades) debe venir de una herramienta. Nunca calcules
  cifras nuevas por tu cuenta ni estimes; si algo no está en las herramientas, dilo y sugiere qué dato falta.
- Cuando hables de una decisión, cita su número (por ejemplo, "decisión #12") para que el usuario la encuentre.
- No apruebas ni cambias decisiones: eso lo hace el usuario en la bandeja. Hay tres tipos: cobrar,
  congelar_credito (frenar crédito) y ampliar_cupo (más crédito a buenos pagadores para que sigan creciendo).
  El campo "aprueba" dice si la decide el líder o el gerente. El paquete Foco comercial agrega
  recuperar_cliente (clientes que dejaron de comprar o compran menos) y crecer_cliente (clientes que crecen).
  El paquete Margen agrega revisar_margen_cliente, revisar_margen_producto y descuento_fuera_politica; en las de
  producto el campo "cliente" es el nombre del producto. El paquete Caja a 13 semanas agrega caja_bajo_minimo
  (semanas en que la caja proyectada queda bajo el mínimo, con qué cobrar, qué aplazar y cuánto financiar),
  aplazar_pago_proveedor (el campo "cliente" es el proveedor) y excedente_de_caja. El paquete Inventario agrega
  reponer_producto y liberar_inventario; en ellas el campo "cliente" es el producto. El paquete Despacho agrega
  despachar_pedidos (pedidos pendientes que pasan o están por pasar la fecha prometida, con lo que los frena:
  crédito, cupo o inventario) y recuperar_cumplimiento (clientes a los que se les entrega tarde).
- Los clientes pueden aparecer con un alias como "Cliente-a1b2c3". Úsalo tal cual; el sistema lo traduce.
- Escribe pesos como $12.345.678. Sé breve: respuestas de pocas líneas, viñetas solo si hay varios ítems.
- Si te piden redactar un mensaje de cobro, que sea cordial, firme y con los datos exactos de la evidencia."""

HERRAMIENTAS = [
    {
        "name": "resumen_cartera",
        "description": "Indicadores de cartera del último corte frente a la línea base, pesos recuperados, "
                       "caja liberada, decisiones por estado, historial de cortes y resultados de Foco comercial, Margen, Caja a 13 semanas, Inventario y Despacho.",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "listar_decisiones",
        "description": "Lista decisiones de la bandeja ordenadas por prioridad (0-100).",
        "input_schema": {
            "type": "object",
            "properties": {
                "estado": {"type": "string", "enum": ["abiertas", "todas", "pendiente", "aprobada", "ajustada",
                                                      "ejecutada", "descartada", "cerrada"]},
                "tipo": {"type": "string", "enum": ["todos", "cobrar", "congelar_credito",
                                                    "ampliar_cupo", "recuperar_cliente",
                                                    "crecer_cliente", "revisar_margen_cliente",
                                                    "revisar_margen_producto",
                                                    "descuento_fuera_politica", "caja_bajo_minimo",
                                                    "aplazar_pago_proveedor", "excedente_de_caja",
                                                    "reponer_producto", "liberar_inventario",
                                                    "despachar_pedidos", "recuperar_cumplimiento"]},
                "limite": {"type": "integer", "description": "Máximo 50"},
            },
            "required": ["estado", "tipo", "limite"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "detalle_decision",
        "description": "Evidencia completa de una decisión: facturas vencidas, motivo, regla que la generó, "
                       "acciones del equipo y resultados medidos.",
        "input_schema": {"type": "object", "properties": {"decision_id": {"type": "integer"}},
                         "required": ["decision_id"], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "buscar_cliente",
        "description": "Busca un cliente por nombre, NIT o alias y devuelve su saldo, saldo vencido, mora "
                       "y decisiones abiertas en el último corte.",
        "input_schema": {"type": "object", "properties": {"texto": {"type": "string"}},
                         "required": ["texto"], "additionalProperties": False},
        "strict": True,
    },
]


# --- Seudonimización ------------------------------------------------------------
class Seudonimizador:
    def __init__(self, directorio: dict[str, str], activo: bool = True):
        """directorio: nit -> nombre del cliente."""
        self.activo = activo
        self.alias_de: dict[str, str] = {}          # texto real -> alias
        self.real_de: dict[str, str] = {}           # alias -> "Nombre (NIT ...)"
        self.nit_de: dict[str, str] = {}            # alias -> nit
        for nit, nombre in directorio.items():
            alias = "Cliente-" + hashlib.sha256(nit.encode()).hexdigest()[:6]
            self.alias_de[nit] = alias
            if nombre and len(nombre) >= 3:
                self.alias_de[nombre] = alias
            self.real_de[alias] = f"{nombre} (NIT {nit})" if nombre else f"NIT {nit}"
            self.nit_de[alias] = nit
        claves = sorted(self.alias_de, key=len, reverse=True)
        self._patron = re.compile("|".join(rf"(?<!\w){re.escape(k)}(?!\w)" for k in claves),
                                  re.IGNORECASE) if claves else None
        self._minus = {k.lower(): v for k, v in self.alias_de.items()}

    def ocultar(self, texto: str) -> str:
        if not self.activo or not self._patron:
            return texto
        return self._patron.sub(lambda m: self._minus[m.group(0).lower()], texto)

    def mostrar(self, texto: str) -> str:
        if not self.activo:
            return texto
        return re.sub(r"Cliente-[0-9a-f]{6}", lambda m: self.real_de.get(m.group(0), m.group(0)), texto)

    def nit_real(self, texto: str) -> str:
        return self.nit_de.get(texto.strip(), texto)


def _directorio(motor: Engine) -> dict[str, str]:
    with motor.connect() as cx:
        return {r.nit: r.nombre for r in cx.execute(select(clientes.c.nit, clientes.c.nombre))}


def _json(obj: Any) -> str:
    def conv(o):
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        if hasattr(o, "item"):
            return o.item()
        return str(o)
    return json.dumps(obj, ensure_ascii=False, default=conv)


# --- Herramientas (solo lectura) ----------------------------------------------
def _resumen_cartera(motor: Engine, _: dict, __: Seudonimizador) -> dict:
    hist = impacto.historial(motor)
    return {**impacto.resumen(motor), "historial_cortes": hist.tail(6).to_dict("records"),
            "foco_comercial": comercial.resumen(motor), "margen": margen.resumen(motor), "caja": _resumen_caja(motor),
            "inventario": inventario.resumen(motor), "despacho": despacho.resumen(motor)}


def _resumen_caja(motor: Engine) -> dict:
    r = caja.resumen(motor)
    if r.get("proyeccion") is not None:
        r["proyeccion"] = [{"semana": int(x.semana), "desde": x.inicio, "saldo_final": round(float(x.saldo_final))}
                           for x in r["proyeccion"].itertuples(index=False)]
    return r


def _compacta(d: dict) -> dict:
    ev = d["evidencia"]
    return {"decision_id": d["id"], "cliente": d.get("cliente") or ev.get("producto") or ev.get("nombre_mostrar") or d["nit"], "nit": d["nit"],
            "tipo": d["tipo"], "estado": d["estado"], "prioridad": d["prioridad"],
            "saldo_vencido": ev.get("saldo_vencido"), "dias_mora_max": ev.get("dias_mora_max"),
            "recomendacion": d["recomendacion"], "aprueba": d.get("nivel_aprobacion"),
            "cliente_clave": ev.get("cliente_clave")}


def _listar_decisiones(motor: Engine, args: dict, _: Seudonimizador) -> dict:
    estado = args.get("estado", "abiertas")
    estados = decisiones.ABIERTAS if estado == "abiertas" else None if estado == "todas" else (estado,)
    tipo = None if args.get("tipo") in (None, "todos") else args["tipo"]
    filas = decisiones.listar(motor, estados, tipo, min(int(args.get("limite") or 20), 50))
    return {"total": len(filas), "decisiones": [_compacta(d) for d in filas]}


def _detalle_decision(motor: Engine, args: dict, _: Seudonimizador) -> dict:
    d = decisiones.detalle(motor, int(args["decision_id"]))
    if not d:
        return {"error": f"No existe la decisión #{args['decision_id']}."}
    return {**_compacta(d), "motivo": d["motivo"], "evidencia": d["evidencia"],
            "acciones": [{k: a[k] for k in ("tipo", "usuario", "detalle", "fecha")} for a in d["acciones"]],
            "resultados": [{k: r[k] for k in ("saldo_vencido_antes", "saldo_vencido_despues",
                                               "pesos_recuperados", "atendida", "medido_en")}
                           for r in d["resultados"]]}


def _buscar_cliente(motor: Engine, args: dict, seud: Seudonimizador) -> dict:
    texto = seud.nit_real(args["texto"]).lower()
    directorio = _directorio(motor)
    nits = [n for n, nom in directorio.items() if texto in n or texto in (nom or "").lower()][:5]
    if not nits:
        return {"encontrados": 0}
    from .db import cargas
    with motor.connect() as cx:
        ultima = cx.execute(select(cargas.c.id, cargas.c.fecha_corte)
                            .order_by(cargas.c.fecha_corte.desc(), cargas.c.id.desc()).limit(1)).first()
    fact = impacto.facturas_de_carga(motor, ultima.id) if ultima else None
    salida = []
    for nit in nits:
        info = {"cliente": directorio[nit], "nit": nit}
        if fact is not None:
            f = fact[(fact["nit"] == nit) & (fact["saldo"] > 0)]
            mora = [(ultima.fecha_corte - v).days for v in f["fecha_vencimiento"]]
            venc = [s for s, m in zip(f["saldo"], mora) if m > 0]
            info.update(fecha_corte=ultima.fecha_corte, saldo_total=float(f["saldo"].sum()),
                        saldo_vencido=float(sum(venc)), dias_mora_max=max([m for m in mora if m > 0], default=0),
                        facturas_con_saldo=len(f))
        info["decisiones_abiertas"] = [_compacta(d) for d in decisiones.listar(motor)
                                       if d["nit"] == nit]
        salida.append(info)
    return {"encontrados": len(salida), "clientes": salida}


EJECUTORES: dict[str, Callable[[Engine, dict, Seudonimizador], dict]] = {
    "resumen_cartera": _resumen_cartera,
    "listar_decisiones": _listar_decisiones,
    "detalle_decision": _detalle_decision,
    "buscar_cliente": _buscar_cliente,
}


# --- Conversación -----------------------------------------------------------------
def preguntar(motor: Engine, pregunta: str, usuario: str, historial: list[dict] | None = None,
              cliente_api=None, max_vueltas: int = 8) -> dict:
    """Responde una pregunta usando solo las herramientas de lectura.

    historial: turnos anteriores [{"role": "user"|"assistant", "content": str}] en texto real.
    Devuelve {"respuesta": str, "herramientas": [...]}.
    """
    seud = Seudonimizador(_directorio(motor), config.anonimizar_ia())
    if cliente_api is None:
        import anthropic
        cliente_api = anthropic.Anthropic()
    mensajes: list[dict] = [{"role": t["role"], "content": seud.ocultar(t["content"])}
                            for t in (historial or [])]
    mensajes.append({"role": "user", "content": seud.ocultar(pregunta)})
    usadas: list[dict] = []
    respuesta = ""

    for _ in range(max_vueltas):
        r = cliente_api.beta.messages.create(
            model=config.modelo_ia(),
            max_tokens=16000,
            system=SISTEMA,
            tools=HERRAMIENTAS,
            messages=mensajes,
            thinking={"type": "adaptive"},
            output_config={"effort": config.esfuerzo_ia()},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if r.stop_reason == "refusal":
            respuesta = "El asistente no pudo responder esta consulta. Intenta formularla de otra manera."
            break
        mensajes.append({"role": "assistant", "content": r.content})
        if r.stop_reason == "pause_turn":
            continue
        llamadas = [b for b in r.content if getattr(b, "type", None) == "tool_use"]
        if r.stop_reason != "tool_use" or not llamadas:
            respuesta = "".join(b.text for b in r.content if getattr(b, "type", None) == "text").strip()
            break
        bloques = []
        for b in llamadas:
            entrada = b.input if isinstance(b.input, dict) else json.loads(b.input)
            usadas.append({"herramienta": b.name, "entrada": entrada})
            try:
                salida = EJECUTORES[b.name](motor, entrada, seud)
                bloques.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": seud.ocultar(_json(salida))})
            except Exception as e:  # el error vuelve a Claude, no rompe la app
                bloques.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                "content": f"Error al consultar: {e}"})
        mensajes.append({"role": "user", "content": bloques})
    else:
        respuesta = "La consulta necesitó demasiados pasos. Intenta una pregunta más concreta."

    respuesta_real = seud.mostrar(respuesta)
    with motor.begin() as cx:
        cx.execute(insert(consultas_ia).values(
            usuario=usuario, pregunta=pregunta, herramientas=usadas, respuesta=respuesta_real,
            anonimizado=seud.activo, modelo=config.modelo_ia()))
    return {"respuesta": respuesta_real, "herramientas": usadas}


def explicar_decision(motor: Engine, decision_id: int, usuario: str, cliente_api=None) -> dict:
    return preguntar(motor, f"Explícame en 3 o 4 líneas por qué el sistema recomienda la decisión #{decision_id} "
                            "y qué debería hacer primero. Usa solo la evidencia de la decisión.",
                     usuario, cliente_api=cliente_api)


def redactar_cobro(motor: Engine, decision_id: int, usuario: str, cliente_api=None) -> dict:
    return preguntar(motor, f"Redacta un mensaje de cobro por correo para el cliente de la decisión "
                            f"#{decision_id}, con las facturas vencidas, sus saldos y días de mora tal como "
                            "aparecen en la evidencia. Firma como 'Área de Cartera'.",
                     usuario, cliente_api=cliente_api)
