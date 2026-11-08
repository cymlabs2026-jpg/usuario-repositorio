"""Semáforo de calidad de datos.

El riesgo número uno del proyecto son los datos sucios. El semáforo hace
visible el problema para el cliente, con número de fila, para que lo corrija
en su ERP en vez de que C&M LABS lo limpie a mano cada mes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd

from .ontologia import Plantilla, cargar_plantilla

ORDEN = {"verde": 0, "amarillo": 1, "rojo": 2}
UMBRAL_ROJO = 0.10          # más del 10 % de filas afectadas = rojo


@dataclass
class Validacion:
    nombre: str
    descripcion: str
    filas_afectadas: int
    porcentaje: float
    nivel: str
    excluye_filas: bool
    ejemplos: list[int] = field(default_factory=list)   # números de fila del archivo


@dataclass
class Informe:
    semaforo: str
    filas_total: int
    filas_validas: int
    puede_cargar: bool
    faltan_campos: list[str]
    validaciones: list[Validacion]

    def a_dict(self) -> dict:
        d = asdict(self)
        return d


def _validacion(nombre, descripcion, mascara: pd.Series, datos: pd.DataFrame, excluye: bool,
                nivel_max: str = "rojo") -> Validacion:
    n = int(mascara.sum())
    total = max(len(datos), 1)
    pct = n / total
    if n == 0:
        nivel = "verde"
    elif pct > UMBRAL_ROJO and nivel_max == "rojo":
        nivel = "rojo"
    else:
        nivel = "amarillo"
    return Validacion(nombre, descripcion, n, round(pct * 100, 1), nivel, excluye,
                      datos.loc[mascara, "_fila"].head(10).tolist())


def evaluar(datos: pd.DataFrame, mapeo: dict[str, str],
            plantilla: Plantilla | None = None) -> tuple[Informe, pd.DataFrame]:
    """Evalúa el archivo ya convertido. Devuelve el informe y las filas válidas."""
    plantilla = plantilla or cargar_plantilla()
    faltan = [c for c in plantilla.requeridos if c not in mapeo]
    if faltan or datos.empty:
        return Informe("rojo", len(datos), 0, False, faltan, []), datos.iloc[0:0]

    v: list[Validacion] = []
    sin_nit = datos["nit"].isna()
    v.append(_validacion("NIT válido", "Filas sin NIT o cédula reconocible", sin_nit, datos, True))
    sin_fechas = datos["fecha_emision"].isna() | datos["fecha_vencimiento"].isna()
    v.append(_validacion("Fechas legibles", "Fecha de emisión o vencimiento vacía o ilegible",
                         sin_fechas, datos, True))
    sin_saldo = datos["saldo"].isna()
    v.append(_validacion("Saldo numérico", "Saldo vacío o que no es un número", sin_saldo, datos, True))
    negativo = datos["saldo"].fillna(0) < 0
    v.append(_validacion("Saldo no negativo", "Saldos negativos (notas crédito o anticipos sin cruzar)",
                         negativo, datos, True))
    dup = datos.duplicated(subset=["nit", "numero"], keep="first") & datos["nit"].notna()
    v.append(_validacion("Facturas únicas", "Factura repetida para el mismo cliente (se deja la primera)",
                         dup, datos, True))

    fe = pd.to_datetime(datos["fecha_emision"], errors="coerce")
    fv = pd.to_datetime(datos["fecha_vencimiento"], errors="coerce")
    v.append(_validacion("Vencimiento coherente", "Vence antes de haber sido emitida",
                         (fv < fe).fillna(False), datos, False, "amarillo"))
    if datos["valor"].notna().any():
        v.append(_validacion("Saldo ≤ valor", "El saldo es mayor que el valor de la factura",
                             (datos["saldo"] > datos["valor"]).fillna(False), datos, False, "amarillo"))
    nombres = datos.dropna(subset=["nit"]).groupby("nit")["cliente"].nunique()
    varios = datos["nit"].isin(nombres[nombres > 1].index)
    v.append(_validacion("Un nombre por NIT", "El mismo NIT aparece con nombres distintos",
                         varios, datos, False, "amarillo"))

    excluir = sin_nit | sin_fechas | sin_saldo | negativo | dup
    validas = datos.loc[~excluir].copy()
    semaforo = max((x.nivel for x in v), key=ORDEN.get, default="verde")
    informe = Informe(semaforo, len(datos), len(validas), len(validas) > 0, [], v)
    return informe, validas


def evaluar_ventas(datos: pd.DataFrame, mapeo: dict[str, str],
                   plantilla: Plantilla | None = None) -> tuple[Informe, pd.DataFrame]:
    """Semáforo para el reporte de ventas (todas las facturas emitidas, incluidas las pagadas)."""
    plantilla = plantilla or cargar_plantilla(objeto="Venta")
    faltan = [c for c in plantilla.requeridos if c not in mapeo]
    if faltan or datos.empty:
        return Informe("rojo", len(datos), 0, False, faltan, []), datos.iloc[0:0]
    v: list[Validacion] = []
    sin_nit = datos["nit"].isna()
    v.append(_validacion("NIT válido", "Filas sin NIT o cédula reconocible", sin_nit, datos, True))
    sin_fecha = datos["fecha"].isna()
    v.append(_validacion("Fecha legible", "Fecha de la factura vacía o ilegible", sin_fecha, datos, True))
    sin_valor = datos["valor"].isna()
    v.append(_validacion("Valor numérico", "Valor vacío o que no es un número", sin_valor, datos, True))
    dup = datos.duplicated(subset=["nit", "numero"], keep="first") & datos["nit"].notna()
    v.append(_validacion("Facturas únicas", "Factura repetida para el mismo cliente (se deja la primera)",
                         dup, datos, True))
    negativo = datos["valor"].fillna(0) < 0
    v.append(_validacion("Notas crédito", "Valores negativos: se restan de las ventas del cliente",
                         negativo, datos, False, "amarillo"))
    nombres = datos.dropna(subset=["nit"]).groupby("nit")["cliente"].nunique()
    varios = datos["nit"].isin(nombres[nombres > 1].index)
    v.append(_validacion("Un nombre por NIT", "El mismo NIT aparece con nombres distintos",
                         varios, datos, False, "amarillo"))
    excluir = sin_nit | sin_fecha | sin_valor | dup
    validas = datos.loc[~excluir].copy()
    semaforo = max((x.nivel for x in v), key=ORDEN.get, default="verde")
    return Informe(semaforo, len(datos), len(validas), len(validas) > 0, [], v), validas
