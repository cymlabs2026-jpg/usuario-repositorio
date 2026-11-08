"""Piso 3: ontología. Traduce columnas sueltas al lenguaje del negocio.

Las plantillas por sector viven en /plantillas como YAML versionado en Git.
El cliente hereda una plantilla y no se escribe código nuevo por cliente.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

import yaml

from . import config


@dataclass(frozen=True)
class Campo:
    nombre: str
    tipo: str            # nit | texto | fecha | dinero
    requerido: bool
    sinonimos: tuple[str, ...]


@dataclass(frozen=True)
class Plantilla:
    nombre: str
    version: int
    campos_factura: tuple[Campo, ...]

    @property
    def requeridos(self) -> list[str]:
        return [c.nombre for c in self.campos_factura if c.requerido]

    def campo(self, nombre: str) -> Campo:
        return next(c for c in self.campos_factura if c.nombre == nombre)


@lru_cache(maxsize=16)
def cargar_plantilla(nombre: str | None = None, objeto: str = "Factura") -> Plantilla:
    """Campos estándar de un objeto de la ontología: Factura (reporte de cartera) o Venta (reporte de ventas)."""
    nombre = nombre or config.plantilla_sector()
    ruta = config.DIR_PLANTILLAS / f"{nombre}.yaml"
    datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    campos = tuple(
        Campo(n, d["tipo"], bool(d.get("requerido")), tuple(d.get("sinonimos", [])))
        for n, d in datos["objetos"][objeto]["campos"].items()
    )
    return Plantilla(datos["nombre"], int(datos["version"]), campos)


def normalizar(texto: str) -> str:
    """'Nombre del Tercero ' -> 'nombredeltercero' (sin tildes, espacios ni signos)."""
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", t.lower())


def sugerir_mapeo(columnas: list[str], plantilla: Plantilla | None = None) -> dict[str, str]:
    """Propone qué columna del archivo corresponde a cada campo estándar.

    Coincidencia exacta con un sinónimo vale más que una coincidencia parcial;
    cada columna se usa una sola vez. No usa IA: los nombres de columna bastan
    en la gran mayoría de exportes, y así no sale ningún dato del cliente.
    """
    plantilla = plantilla or cargar_plantilla()
    norm = {c: normalizar(c) for c in columnas}
    candidatos: list[tuple[int, int, str, str]] = []
    for campo in plantilla.campos_factura:
        for orden, sin in enumerate(campo.sinonimos):
            s = normalizar(sin)
            for col, n in norm.items():
                if n == s:
                    candidatos.append((2, -orden, campo.nombre, col))
                elif len(s) >= 4 and s in n:
                    candidatos.append((1, -orden, campo.nombre, col))
    candidatos.sort(reverse=True)
    mapeo: dict[str, str] = {}
    usadas: set[str] = set()
    for _, _, campo, col in candidatos:
        if campo not in mapeo and col not in usadas:
            mapeo[campo] = col
            usadas.add(col)
    return mapeo
