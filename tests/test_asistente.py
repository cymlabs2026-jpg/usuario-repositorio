import json
from types import SimpleNamespace as NS

from sqlalchemy import select

from ontara import asistente, decisiones
from ontara.db import consultas_ia

from conftest import CORTE_1


class ClienteFalso:
    """Imita client.beta.messages.create y guarda lo que se le envió."""

    def __init__(self, guion):
        self.guion = list(guion)
        self.enviado = []
        self.beta = NS(messages=NS(create=self._crear))

    def _crear(self, **kw):
        self.enviado.append(json.dumps(kw["messages"], default=str, ensure_ascii=False))
        return self.guion.pop(0)


def texto(t):
    return NS(type="text", text=t)


def uso(nombre, entrada, id_="t1"):
    return NS(type="tool_use", id=id_, name=nombre, input=entrada)


def preparar(motor, corte1):
    informe, validas, mapeo = corte1
    decisiones.registrar_carga(motor, validas, informe, mapeo, CORTE_1, "x.csv", "prueba")
    return decisiones.listar(motor)[0]


def test_seudonimizador_ida_y_vuelta():
    s = asistente.Seudonimizador({"900123456": "Distribuidora El Samán"})
    oculto = s.ocultar("¿Cuánto debe distribuidora el samán (900123456)?")
    assert "Samán" not in oculto and "900123456" not in oculto
    assert "Distribuidora El Samán (NIT 900123456)" in s.mostrar(oculto)


def test_no_salen_nombres_ni_nit_reales_y_la_respuesta_los_restaura(motor, corte1):
    d = preparar(motor, corte1)
    alias = asistente.Seudonimizador({d["nit"]: d["cliente"]}).alias_de[d["nit"]]
    falso = ClienteFalso([
        NS(stop_reason="tool_use", content=[uso("detalle_decision", {"decision_id": d["id"]})]),
        NS(stop_reason="end_turn", content=[texto(f"Llama primero a {alias} (decisión #{d['id']}).")]),
    ])
    r = asistente.preguntar(motor, f"¿Qué pasa con {d['cliente']}?", "lider", cliente_api=falso)
    enviado = " ".join(falso.enviado)
    assert d["cliente"] not in enviado and d["nit"] not in enviado
    assert d["cliente"] in r["respuesta"]
    assert r["herramientas"] == [{"herramienta": "detalle_decision", "entrada": {"decision_id": d["id"]}}]


def test_herramientas_son_de_solo_lectura_y_queda_registro(motor, corte1):
    preparar(motor, corte1)
    nombres = {h["name"] for h in asistente.HERRAMIENTAS}
    assert nombres == {"resumen_cartera", "listar_decisiones", "detalle_decision", "buscar_cliente"}
    antes = decisiones.listar(motor)
    falso = ClienteFalso([
        NS(stop_reason="tool_use", content=[
            uso("resumen_cartera", {}, "a"),
            uso("listar_decisiones", {"estado": "abiertas", "tipo": "todos", "limite": 5}, "b"),
            uso("buscar_cliente", {"texto": "constructora"}, "c")]),
        NS(stop_reason="end_turn", content=[texto("Listo.")]),
    ])
    asistente.preguntar(motor, "Resumen", "gerente", cliente_api=falso)
    assert decisiones.listar(motor) == antes
    with motor.connect() as cx:
        reg = cx.execute(select(consultas_ia)).mappings().one()
    assert reg["anonimizado"] and len(reg["herramientas"]) == 3


def test_error_de_herramienta_vuelve_a_claude(motor, corte1):
    preparar(motor, corte1)
    falso = ClienteFalso([
        NS(stop_reason="tool_use", content=[uso("detalle_decision", {"decision_id": 99999})]),
        NS(stop_reason="end_turn", content=[texto("No existe esa decisión.")]),
    ])
    r = asistente.preguntar(motor, "Decisión 99999", "lider", cliente_api=falso)
    assert "No existe" in falso.enviado[-1] and r["respuesta"] == "No existe esa decisión."


def test_rechazo_del_modelo(motor, corte1):
    preparar(motor, corte1)
    falso = ClienteFalso([NS(stop_reason="refusal", content=[])])
    assert "no pudo responder" in asistente.preguntar(motor, "x", "lider", cliente_api=falso)["respuesta"]
