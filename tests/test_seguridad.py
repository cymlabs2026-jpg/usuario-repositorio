"""Acceso que no se abre por error, seudonimización sin fugas y lectura honesta del grupo de control."""
from datetime import timedelta
from types import SimpleNamespace as NS

from sqlalchemy import insert

from ontara import acceso, asistente, decisiones, estadistica
from ontara.db import accesos, ahora

from tests.test_asistente import ClienteFalso, preparar, texto, uso


# --- Acceso -----------------------------------------------------------------------------
def test_sin_usuarios_no_entra_nadie_salvo_demo_explicito(monkeypatch):
    monkeypatch.delenv("ONTARA_MODO_DEMO", raising=False)
    assert acceso.modo_acceso({}) == "bloqueado"
    monkeypatch.setenv("ONTARA_MODO_DEMO", "1")
    assert acceso.modo_acceso({}) == "demo"
    assert acceso.modo_acceso({"maria@empresa.com": {"rol": "lider"}}) == "login"
    monkeypatch.setenv("ONTARA_MODO_DEMO", "0")
    assert acceso.modo_acceso({}) == "bloqueado"


def test_bloqueo_por_intentos_fallidos(motor):
    for _ in range(acceso.MAX_FALLIDOS - 1):
        acceso.registrar(motor, "Gerente", "fallido", "clave")
    assert not acceso.bloqueado(motor, "gerente")
    acceso.registrar(motor, "GERENTE ", "fallido", "clave")          # mayúsculas y espacios cuentan igual
    assert acceso.bloqueado(motor, "gerente")
    assert not acceso.bloqueado(motor, "maria")                       # el bloqueo es por usuario
    # Pasada la ventana, se puede volver a intentar.
    despues = ahora() + timedelta(minutes=acceso.VENTANA_MINUTOS + 1)
    assert not acceso.bloqueado(motor, "gerente", ahora_=despues)


def test_intentos_viejos_no_bloquean(motor):
    with motor.begin() as cx:
        for _ in range(10):
            cx.execute(insert(accesos).values(usuario="gerente", evento="fallido", metodo="clave",
                                              fecha=ahora() - timedelta(hours=2)))
    assert not acceso.bloqueado(motor, "gerente")


# --- Seudonimización ------------------------------------------------------------------------
def seud():
    return asistente.Seudonimizador(
        {"900123456": "Ferretería Central S.A.S.", "16789012": "Juan Pérez"},
        proveedores={"800555111": "Transportes Llanogrande Ltda."},
        vendedores=["Carlos Gómez"])


def test_variantes_de_nombre_y_nit_no_salen():
    s = seud()
    for txt in ["Ferretería Central S.A.S. debe", "FERRETERIA CENTRAL SAS debe", "ferreteria  central debe",
                "NIT 900.123.456-7", "NIT 900123456-7", "nit 900 123 456", "juan perez llamó",
                "pago a Transportes Llanogrande", "visita de carlos gomez", "escribir a cartera@ferreteria.co"]:
        oculto = s.ocultar(txt)
        plano = asistente._plano(oculto)
        for prohibido in ("ferreteria central", "900123456", "900 123 456", "juan perez", "llanogrande",
                          "carlos gomez", "cartera ferreteria"):
            assert prohibido not in plano, (txt, oculto)


def test_alias_se_restauran_y_no_tocan_montos():
    s = seud()
    oculto = s.ocultar("Ferretería Central debe $16.789.012 y escribió desde cartera@ferreteria.co")
    assert "$16.789.012" in oculto                       # un monto igual a una cédula no se toca
    real = s.mostrar(oculto)
    assert "Ferretería Central S.A.S. (NIT 900123456)" in real and "cartera@ferreteria.co" in real
    assert s.mostrar(s.ocultar("Proveedor: Transportes Llanogrande Ltda.")).count("NIT 800555111") == 1


def test_alias_largos_secretos_y_sin_choques():
    s = seud()
    alias = s.alias_de["900123456"]
    assert alias.startswith("Cliente-") and len(alias.split("-")[1]) >= 10
    import hashlib
    assert hashlib.sha256(b"900123456").hexdigest()[:10] not in alias      # no se revierte probando NIT
    # Choque forzado: dos claves distintas nunca comparten alias.
    usados = {}
    a = asistente._alias("Cliente", "1", usados)
    usados[f"Cliente-{asistente.hmac.new(asistente._SECRETO, b'Cliente|2', asistente.hashlib.sha256).hexdigest()[:10]}"] = "otro"
    b = asistente._alias("Cliente", "2", usados)
    assert a != b and usados[b] == "2"
    # Miles de NIT sin choques.
    s2 = asistente.Seudonimizador({str(800000000 + i): f"Empresa {i}" for i in range(5000)})
    assert len(set(s2.nit_de)) == 5000


def test_telefonos_en_notas():
    assert asistente.texto_libre("Llamar al 310 555 1234 o +57 315-555-9876") == \
        "Llamar al [teléfono] o [teléfono]"
    assert asistente.texto_libre("Saldo 3105551234000") == "Saldo 3105551234000"


def test_quien_hizo_la_accion_no_sale_hacia_la_ia(motor, corte1):
    d = preparar(motor, corte1)
    decisiones.registrar_accion(motor, d["id"], "nota", "maria@empresa.com", "Le escribí al 3105551234",
                                rol="lider")
    falso = ClienteFalso([
        NS(stop_reason="tool_use", content=[uso("detalle_decision", {"decision_id": d["id"]})]),
        NS(stop_reason="end_turn", content=[texto("Listo.")]),
    ])
    asistente.preguntar(motor, "¿Qué se ha hecho?", "maria@empresa.com", cliente_api=falso)
    enviado = " ".join(falso.enviado)
    assert "maria@empresa.com" not in enviado and "3105551234" not in enviado and "[teléfono]" in enviado


# --- Grupo de control ---------------------------------------------------------------------
def test_rango_del_efecto():
    efecto = lambda t, c: sum(t) / len(t) - sum(c) / len(c)  # noqa: E731
    pocos = estadistica.rango_efecto([10, 12], [9, 11], efecto)
    assert pocos == estadistica.rango_efecto([10, 12], [9, 11], efecto)        # reproducible
    assert estadistica.rango_efecto([1], [1, 2], efecto) is None
    claro = estadistica.rango_efecto([100 + i % 7 for i in range(40)], [50 + i % 7 for i in range(40)], efecto)
    lectura = estadistica.lectura(40, 40, claro)
    assert lectura["muestra_suficiente"] and lectura["efecto_concluyente"] and claro[0] > 0
    ruido = estadistica.rango_efecto([i % 10 for i in range(40)], [(i + 3) % 10 for i in range(40)], efecto)
    assert not estadistica.lectura(40, 40, ruido)["efecto_concluyente"]
    assert not estadistica.lectura(12, 12, claro)["muestra_suficiente"]
