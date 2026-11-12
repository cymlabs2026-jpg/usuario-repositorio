"""Configuración por variables de entorno.

Un cliente = un despliegue con su propia base de datos, así que toda la
configuración del cliente vive aquí y no en el código.
"""
from __future__ import annotations

import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DIR_PLANTILLAS = RAIZ / "plantillas"
DIR_REGLAS = RAIZ / "reglas"


def database_url() -> str:
    """PostgreSQL en producción (Cloud SQL); SQLite local para desarrollo y pruebas."""
    return os.environ.get("DATABASE_URL", f"sqlite:///{RAIZ / 'ontara.db'}")


def nombre_empresa() -> str:
    return os.environ.get("ONTARA_EMPRESA", "Empresa de demostración")


def plantilla_sector() -> str:
    return os.environ.get("ONTARA_PLANTILLA", "comercial")


def modo_demo() -> bool:
    """Entrar sin usuario (como gerente de demostración) solo si se pide de forma explícita con
    ONTARA_MODO_DEMO=1. Sin eso, una instalación sin usuarios configurados no deja entrar a nadie:
    un error de configuración nunca debe dejar los datos de un cliente abiertos."""
    return os.environ.get("ONTARA_MODO_DEMO", "").strip().lower() in ("1", "true", "si", "sí", "yes")


# --- Asistente de IA -------------------------------------------------------
def modelo_ia() -> str:
    return os.environ.get("ONTARA_MODELO", "claude-opus-5-5")


def esfuerzo_ia() -> str:
    return os.environ.get("ONTARA_ESFUERZO", "medium")


def anonimizar_ia() -> bool:
    """Ley 1581: por defecto no se envían nombres ni NIT reales a la API de IA."""
    return os.environ.get("ONTARA_ANONIMIZAR_IA", "1").lower() not in ("0", "false", "no")


def ia_disponible() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
