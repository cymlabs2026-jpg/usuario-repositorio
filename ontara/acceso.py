"""Acceso a la app: usuarios, roles, claves y bitácora de accesos.

Dos formas de entrar:
- Con la cuenta de Google o Microsoft de la empresa (recomendado). La identidad la verifica el proveedor;
  Ontara solo deja pasar los correos registrados en ONTARA_USUARIOS.
- Con usuario y clave, para pilotos sin cuenta corporativa. Las claves se guardan con PBKDF2-SHA256,
  sal aleatoria y 600.000 iteraciones; nunca en texto plano ni con un hash simple.

ONTARA_USUARIOS = '{"maria@empresa.com": {"rol": "lider"},
                    "gerente": {"rol": "gerente", "clave": "pbkdf2_sha256$600000$<sal>$<hash>"}}'

Generar el hash de una clave:  python -m ontara.acceso "LaClave"
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
from datetime import datetime, timedelta

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine

from . import config
from .db import accesos, ahora

ITERACIONES = 600_000
ROLES_VALIDOS = ("lider", "gerente")


def hash_clave(clave: str, iteraciones: int = ITERACIONES, sal: bytes | None = None) -> str:
    sal = sal or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), sal, iteraciones)
    return "pbkdf2_sha256${}${}${}".format(
        iteraciones, base64.b64encode(sal).decode(), base64.b64encode(dk).decode())


def verificar_clave(clave: str, guardado: str | None) -> bool:
    """Compara en tiempo constante. Cualquier formato distinto de pbkdf2_sha256 se rechaza."""
    try:
        algoritmo, iteraciones, sal, esperado = (guardado or "").split("$")
    except ValueError:
        return False
    if algoritmo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), base64.b64decode(sal), int(iteraciones))
    return hmac.compare_digest(dk, base64.b64decode(esperado))


def usuarios() -> dict[str, dict]:
    """Usuarios autorizados, con la llave en minúsculas (los correos no distinguen mayúsculas)."""
    crudo = os.environ.get("ONTARA_USUARIOS")
    if not crudo:
        return {}
    datos = json.loads(crudo)
    return {str(k).strip().lower(): dict(v) for k, v in datos.items()}


def rol_de(identidad: str | None, lista: dict[str, dict] | None = None) -> str | None:
    """Rol del usuario o None si no está autorizado."""
    if not identidad:
        return None
    lista = usuarios() if lista is None else lista
    datos = lista.get(identidad.strip().lower())
    if not datos:
        return None
    rol = datos.get("rol", "lider")
    return rol if rol in ROLES_VALIDOS else "lider"


def autenticar_con_clave(usuario: str, clave: str, lista: dict[str, dict] | None = None) -> str | None:
    """Rol si usuario y clave son correctos; None si no."""
    lista = usuarios() if lista is None else lista
    datos = lista.get((usuario or "").strip().lower())
    if not datos or not verificar_clave(clave, datos.get("clave")):
        return None
    return rol_de(usuario, lista)


def modo_acceso(lista: dict[str, dict] | None = None, demo: bool | None = None) -> str:
    """"login" si hay usuarios configurados, "demo" solo si se activó ONTARA_MODO_DEMO, y
    "bloqueado" en cualquier otro caso. Nunca se entra sin clave por accidente."""
    lista = usuarios() if lista is None else lista
    if lista:
        return "login"
    demo = config.modo_demo() if demo is None else demo
    return "demo" if demo else "bloqueado"


# --- Intentos fallidos ---------------------------------------------------------------
MAX_FALLIDOS = 5            # intentos fallidos permitidos por usuario...
VENTANA_MINUTOS = 15        # ...dentro de esta ventana; después se bloquea hasta que pase


def bloqueado(motor: Engine, usuario: str, max_fallidos: int = MAX_FALLIDOS,
              ventana_minutos: int = VENTANA_MINUTOS, ahora_: datetime | None = None) -> bool:
    """True si el usuario acumula `max_fallidos` intentos fallidos en los últimos `ventana_minutos`.
    Mientras está bloqueado no se verifica la clave (tampoco se gasta CPU en el PBKDF2)."""
    desde = (ahora_ or ahora()) - timedelta(minutes=ventana_minutos)
    with motor.connect() as cx:
        n = cx.execute(select(func.count()).select_from(accesos).where(
            accesos.c.usuario == (usuario or "(vacío)").strip().lower()[:255],
            accesos.c.evento == "fallido", accesos.c.fecha >= desde)).scalar_one()
    return n >= max_fallidos


def registrar(motor: Engine, usuario: str, evento: str, metodo: str, detalle: str | None = None) -> None:
    """Bitácora de accesos: ingreso, fallido, denegado o salida."""
    with motor.begin() as cx:
        cx.execute(insert(accesos).values(usuario=(usuario or "(vacío)").strip().lower()[:255], evento=evento,
                                          metodo=metodo, detalle=detalle))


def bitacora(motor: Engine, limite: int = 200) -> list[dict]:
    with motor.connect() as cx:
        return [dict(r) for r in cx.execute(
            select(accesos).order_by(accesos.c.id.desc()).limit(limite)).mappings()]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('Uso: python -m ontara.acceso "LaClave"')
    print(hash_clave(sys.argv[1]))
