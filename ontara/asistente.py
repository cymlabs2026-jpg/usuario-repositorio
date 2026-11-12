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
import hmac
import json
import os
import re
import secrets
import unicodedata
from datetime import date, datetime
from typing import Any, Callable

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from . import caja, comercial, config, decisiones, despacho, impacto, inventario, margen
from .db import clientes, consultas_ia, cuentas_por_pagar, facturas, ventas

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
- Clientes, proveedores, vendedores y correos pueden aparecer con un alias como "Cliente-a1b2c3d4e5" o
  "Proveedor-…". Úsalo tal cual; el sistema lo traduce.
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
# Ley 1581: antes de salir hacia la API se reemplazan por alias los nombres y NIT de clientes y proveedores,
# los nombres de los vendedores y los correos. Los alias se calculan con una clave secreta (HMAC), así que no
# se pueden revertir probando NIT. La clave sale de ONTARA_SECRETO_ALIAS o, si no existe, se genera al
# arrancar: los alias solo tienen que ser estables mientras dura una conversación.
_SECRETO = (os.environ.get("ONTARA_SECRETO_ALIAS") or "").encode() or secrets.token_bytes(32)
_LARGO_ALIAS = 10                                   # 40 bits: sin choques prácticos aun con miles de NIT
_PATRON_ALIAS = re.compile(r"\b(?:Cliente|Proveedor|Vendedor|Correo)-[0-9a-f]{%d,}\b" % _LARGO_ALIAS)
_CORREO = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TELEFONO = re.compile(r"(?<!\d)(?:\+?57[\s-]?)?3\d{2}[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_EQUIVALENTES = {"a": "aáàäâ", "e": "eéèëê", "i": "iíìïî", "o": "oóòöô", "u": "uúùüû", "n": "nñ", "c": "cç"}
# Sufijos societarios que suelen escribirse o no ("Ferretería Central" = "Ferretería Central S.A.S.").
_SUFIJO = re.compile(r"(?:^|\s)(?:s a s|sas|s a|sa|ltda|limitada|e u|eu|s en c|y cia|cia|bic)$")


def _plano(texto: str) -> str:
    """Minúsculas, sin tildes y solo letras y números separados por un espacio."""
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _patron_nombre(nombre: str) -> str | None:
    """Patrón que reconoce el nombre con o sin tildes, mayúsculas, puntos o espacios extra."""
    fichas = _plano(nombre).split()
    if not fichas:
        return None
    partes = ["".join(f"[{_EQUIVALENTES[ch]}]" if ch in _EQUIVALENTES else re.escape(ch) for ch in f)
              for f in fichas]
    return r"(?<!\w)" + r"[\s.,&'-]*".join(partes) + r"\.?(?!\w)"


def _variantes_nombre(nombre: str) -> list[str]:
    """El nombre completo y, si es distintivo, el nombre sin el sufijo societario."""
    plano = _plano(nombre or "")
    if len(plano) < 3:
        return []
    salida = [nombre]
    base = plano
    while True:
        corto = _SUFIJO.sub("", base).strip()
        if corto == base:
            break
        base = corto
    if base != plano and (len(base.split()) >= 2 or len(base) >= 8):
        salida.append(base)
    return salida


def _patron_nit(nit: str) -> str | None:
    """El NIT con o sin puntos o espacios entre dígitos y con o sin dígito de verificación.
    No toca montos en pesos (precedidos de $ o con decimales, como en el JSON de las herramientas)."""
    digitos = re.sub(r"\D", "", nit or "")
    if len(digitos) < 5:
        return None
    return (r"(?<![\d$])(?<!\$\s)" + r"[.\s]?".join(digitos) + r"(?:\s*-\s*\d)?(?!\d)(?![.,]\d)")


def _alias(tipo: str, clave: str, usados: dict[str, str]) -> str:
    """Alias estable por clave; si dos claves distintas chocaran, se alarga hasta separarlas."""
    digest = hmac.new(_SECRETO, f"{tipo}|{clave}".encode(), hashlib.sha256).hexdigest()
    largo = _LARGO_ALIAS
    while True:
        alias = f"{tipo}-{digest[:largo]}"
        if usados.get(alias, clave) == clave:
            usados[alias] = clave
            return alias
        largo += 2


class Seudonimizador:
    def __init__(self, directorio: dict[str, str], activo: bool = True,
                 proveedores: dict[str, str] | None = None, vendedores: list[str] | None = None):
        """directorio: nit -> nombre del cliente; proveedores: nit -> nombre; vendedores: nombres."""
        self.activo = activo
        self.real_de: dict[str, str] = {}           # alias -> "Nombre (NIT ...)"
        self.nit_de: dict[str, str] = {}            # alias -> nit
        self.alias_de: dict[str, str] = {}          # nit o nombre -> alias (compatibilidad)
        self._usados: dict[str, str] = {}
        reglas_: list[tuple[str, str]] = []         # (patrón, alias)

        def agregar(tipo: str, nit: str | None, nombre: str | None):
            clave = nit or _plano(nombre or "")
            if not clave:
                return
            alias = _alias(tipo, clave, self._usados)
            if nit:
                self.alias_de[nit] = alias
                self.nit_de[alias] = nit
                if (pn := _patron_nit(nit)):
                    reglas_.append((pn, alias))
            for variante in _variantes_nombre(nombre or ""):
                self.alias_de.setdefault(variante, alias)
                if (pt := _patron_nombre(variante)):
                    reglas_.append((pt, alias))
            self.real_de[alias] = (f"{nombre} (NIT {nit})" if nombre and nit else nombre or f"NIT {nit}")

        for nit, nombre in directorio.items():
            agregar("Cliente", str(nit), nombre)
        for nit, nombre in (proveedores or {}).items():
            agregar("Proveedor", str(nit), nombre)
        for nombre in vendedores or []:
            agregar("Vendedor", None, nombre)
        # Primero los patrones más largos, para que "El Samán S.A.S." gane sobre "El Samán".
        reglas_.sort(key=lambda r: len(r[0]), reverse=True)
        self._grupos = {f"g{i}": alias for i, (_, alias) in enumerate(reglas_)}
        self._patron = (re.compile("|".join(f"(?P<g{i}>{pat})" for i, (pat, _) in enumerate(reglas_)),
                                   re.IGNORECASE) if reglas_ else None)

    def ocultar(self, texto: str) -> str:
        if not self.activo or not texto:
            return texto
        if self._patron:
            texto = self._patron.sub(lambda m: self._grupos[m.lastgroup], texto)
        return _CORREO.sub(self._alias_correo, texto)

    def _alias_correo(self, m: re.Match) -> str:
        correo = m.group(0)
        alias = _alias("Correo", correo.lower(), self._usados)
        self.real_de[alias] = correo
        return alias

    def mostrar(self, texto: str) -> str:
        if not self.activo:
            return texto
        return _PATRON_ALIAS.sub(lambda m: self.real_de.get(m.group(0), m.group(0)), texto)

    def nit_real(self, texto: str) -> str:
        return self.nit_de.get(texto.strip(), texto)


def texto_libre(texto: str | None) -> str | None:
    """Notas escritas a mano: se quitan los teléfonos celulares antes de enviarlas a la IA."""
    return _TELEFONO.sub("[teléfono]", texto) if texto else texto


def _directorio(motor: Engine) -> dict[str, str]:
    with motor.connect() as cx:
        return {r.nit: r.nombre for r in cx.execute(select(clientes.c.nit, clientes.c.nombre))}


def _seudonimizador(motor: Engine) -> Seudonimizador:
    """Clientes, proveedores de cuentas por pagar y vendedores de los reportes de ventas y cartera."""
    with motor.connect() as cx:
        proveedores = {r.nit: r.proveedor for r in cx.execute(
            select(cuentas_por_pagar.c.nit, cuentas_por_pagar.c.proveedor).distinct()) if r.nit}
        vendedores = set()
        for tabla in (ventas, facturas):
            vendedores |= {r[0] for r in cx.execute(select(tabla.c.vendedor).distinct()) if r[0]}
    return Seudonimizador(_directorio(motor), config.anonimizar_ia(), proveedores, sorted(vendedores))


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
            # Quién hizo cada acción no hace falta para explicar la decisión: no sale hacia la IA.
            "acciones": [{"tipo": a["tipo"], "quien": "Ontara" if a["usuario"] == "ontara" else "equipo",
                          "detalle": texto_libre(a["detalle"]), "fecha": a["fecha"]} for a in d["acciones"]],
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
    seud = _seudonimizador(motor)
    if cliente_api is None:
        import anthropic
        cliente_api = anthropic.Anthropic()
    mensajes: list[dict] = [{"role": t["role"], "content": seud.ocultar(texto_libre(t["content"]))}
                            for t in (historial or [])]
    mensajes.append({"role": "user", "content": seud.ocultar(texto_libre(pregunta))})
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
