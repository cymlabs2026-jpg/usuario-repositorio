"""Cuánto creerle a la comparación contra el grupo de control.

Con pocas decisiones por grupo, la diferencia entre "con Ontara" y "sin Ontara" puede ser puro azar.
Aquí se calcula un rango del efecto por remuestreo (bootstrap): se repite la comparación miles de veces
con muestras tomadas al azar de las mismas decisiones y se toma el 90 % central de los resultados.
Si el rango completo queda por encima de cero, el efecto es concluyente; si cruza el cero, todavía no.
"""
from __future__ import annotations

import random
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")

# Por debajo de este número de decisiones por grupo la app avisa que la diferencia puede ser azar.
MUESTRA_MINIMA = 30
REPETICIONES = 2000
NIVEL = 0.90


def rango_efecto(tratamiento: Sequence[T], control: Sequence[T],
                 efecto: Callable[[Sequence[T], Sequence[T]], float | None],
                 repeticiones: int = REPETICIONES, nivel: float = NIVEL,
                 semilla: int = 7) -> tuple[float, float] | None:
    """Rango (bajo, alto) del efecto al `nivel` de confianza. None si no hay al menos 2 casos por grupo.
    La semilla es fija para que la misma información siempre muestre el mismo rango."""
    if len(tratamiento) < 2 or len(control) < 2:
        return None
    azar = random.Random(semilla)
    valores = []
    for _ in range(repeticiones):
        t = [azar.choice(tratamiento) for _ in tratamiento]
        c = [azar.choice(control) for _ in control]
        v = efecto(t, c)
        if v is not None:
            valores.append(v)
    if len(valores) < repeticiones // 2:
        return None
    valores.sort()
    corte = (1 - nivel) / 2
    bajo = valores[int(corte * (len(valores) - 1))]
    alto = valores[int((1 - corte) * (len(valores) - 1))]
    return round(bajo, 0), round(alto, 0)


def lectura(n_tratamiento: int, n_control: int, rango: tuple[float, float] | None) -> dict:
    """Campos comunes que la app usa para decidir qué tan fuerte puede afirmar el resultado."""
    suficiente = n_tratamiento >= MUESTRA_MINIMA and n_control >= MUESTRA_MINIMA
    concluyente = bool(suficiente and rango and (rango[0] > 0 or rango[1] < 0))
    return {"efecto_rango": list(rango) if rango else None, "muestra_suficiente": suficiente,
            "muestra_minima": MUESTRA_MINIMA, "efecto_concluyente": concluyente}
