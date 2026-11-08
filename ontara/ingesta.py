"""Piso 2 (Ingesta): lee Excel/CSV y convierte cada columna a su tipo estándar.

Los exportes de ERP colombianos traen pesos como "$ 1.234.567,50", NIT con
dígito de verificación ("900.123.456-7") y fechas día/mes/año. Aquí se
normaliza todo eso; lo que no se pueda interpretar queda vacío y el
semáforo de calidad lo reporta.
"""
from __future__ import annotations

import io
import re
from datetime import date

import pandas as pd

from .ontologia import Plantilla, cargar_plantilla


def leer_archivo(contenido: bytes, nombre: str) -> pd.DataFrame:
    """Lee .xlsx/.xls o .csv (separador , o ; y codificación utf-8 o latin-1)."""
    if nombre.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(contenido), dtype=str)
    for codificacion in ("utf-8-sig", "latin-1"):
        try:
            texto = contenido.decode(codificacion)
            break
        except UnicodeDecodeError:
            continue
    primera = texto.splitlines()[0] if texto else ""
    sep = ";" if primera.count(";") > primera.count(",") else ","
    return pd.read_csv(io.StringIO(texto), sep=sep, dtype=str, keep_default_na=False)


def a_pesos(valor) -> float | None:
    """'$ 1.234.567,50' -> 1234567.5 · '1,234,567.50' -> 1234567.5 · '' -> None."""
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = re.sub(r"[\s$COPcop]", "", str(valor))
    negativo = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("-()")
    if not s:
        return None
    if "." in s and "," in s:
        if s.rfind(",") > s.rfind("."):          # 1.234.567,50
            s = s.replace(".", "").replace(",", ".")
        else:                                      # 1,234,567.50
            s = s.replace(",", "")
    elif "," in s:
        partes = s.split(",")
        s = s.replace(",", "") if all(len(p) == 3 for p in partes[1:]) else s.replace(",", ".")
    elif "." in s:
        partes = s.split(".")
        if len(partes) > 2 or (len(partes) == 2 and len(partes[1]) == 3):
            s = s.replace(".", "")                 # separador de miles
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if negativo else n


def a_nit(valor) -> str | None:
    """'900.123.456-7' -> '900123456'. Devuelve None si no parece un NIT o cédula."""
    if valor is None:
        return None
    s = str(valor).strip()
    if "-" in s:
        s = s.split("-")[0]
    digitos = re.sub(r"\D", "", s)
    return digitos if 5 <= len(digitos) <= 10 else None


def a_fecha(valor) -> date | None:
    if valor is None or str(valor).strip() == "":
        return None
    s = str(valor).strip()
    iso = bool(re.match(r"^\d{4}-\d{2}-\d{2}", s))
    f = pd.to_datetime(s, dayfirst=not iso, errors="coerce")
    if pd.isna(f):
        try:                                       # número de serie de Excel
            f = pd.Timestamp("1899-12-30") + pd.Timedelta(days=float(s))
        except ValueError:
            return None
    return f.date()


CONVERSORES = {"nit": a_nit, "fecha": a_fecha, "dinero": a_pesos, "numero": a_pesos,
               "texto": lambda v: (str(v).strip() or None) if v is not None else None}


def convertir(df: pd.DataFrame, mapeo: dict[str, str], plantilla: Plantilla | None = None) -> pd.DataFrame:
    """Devuelve un DataFrame con los campos estándar ya tipados.

    `_fila` guarda el número de fila del archivo original (con encabezado = 1)
    para que el cliente pueda encontrar y corregir el dato en su origen.
    """
    plantilla = plantilla or cargar_plantilla()
    salida = pd.DataFrame({"_fila": range(2, len(df) + 2)})
    for campo in plantilla.campos_factura:
        col = mapeo.get(campo.nombre)
        conv = CONVERSORES[campo.tipo]
        salida[campo.nombre] = [conv(v) for v in df[col]] if col in df.columns else None
    return salida
