"""Piso 3 (Reglas) del paquete de decisión Cartera.

Todo cálculo es determinístico: mismos datos + misma versión de reglas =
mismas decisiones. Cada decisión guarda la versión de regla que la produjo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
import yaml
from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine

from . import config
from .db import reglas_version

PAQUETE = "cartera"


def parametros_base(paquete: str = PAQUETE) -> dict:
    datos = yaml.safe_load((config.DIR_REGLAS / f"{paquete}.yaml").read_text(encoding="utf-8"))
    return dict(datos["parametros"])


def regla_activa(motor: Engine, paquete: str = PAQUETE) -> dict:
    """Última versión de parámetros. Si no hay ninguna, registra la versión base del YAML."""
    with motor.begin() as cx:
        fila = cx.execute(
            select(reglas_version).where(reglas_version.c.paquete == paquete)
            .order_by(reglas_version.c.version.desc()).limit(1)
        ).mappings().first()
        if fila is None:
            res = cx.execute(insert(reglas_version).values(
                paquete=paquete, version=1, parametros=parametros_base(paquete),
                creada_por="sistema", nota="Versión base de la plantilla"))
            return {"id": res.inserted_primary_key[0], "version": 1, "parametros": parametros_base(paquete)}
        # Versiones guardadas antes de que existiera un parámetro lo heredan de la plantilla base.
        return {"id": fila["id"], "version": fila["version"],
                "parametros": {**parametros_base(paquete), **dict(fila["parametros"])}}


def guardar_parametros(motor: Engine, parametros: dict, usuario: str, nota: str = "",
                       paquete: str = PAQUETE) -> dict:
    """Guarda una versión nueva; las anteriores se conservan para auditoría."""
    base = parametros_base(paquete)
    desconocidos = set(parametros) - set(base)
    if desconocidos:
        raise ValueError(f"Parámetros no reconocidos: {sorted(desconocidos)}")
    nuevos = {**base, **regla_activa(motor, paquete)["parametros"], **parametros}
    pesos = nuevos["peso_monto"] + nuevos["peso_mora"] + nuevos["peso_tendencia"]
    if abs(pesos - 1) > 0.01:
        raise ValueError(f"Los tres pesos deben sumar 1 (suman {pesos:.2f}).")
    if not 0 <= float(nuevos["pct_grupo_control"]) <= 50:
        raise ValueError("El grupo de control debe estar entre 0% y 50% de las decisiones.")
    if not 0 < float(nuevos["pct_clientes_clave"]) <= 100:
        raise ValueError("El porcentaje para clientes clave debe estar entre 1 y 100.")
    if not 0 < float(nuevos["ampliar_uso_minimo"]) <= 2:
        raise ValueError("El uso mínimo del cupo para ampliar debe estar entre 0 y 2 (200%).")
    negativos = [k for k in ("aprobacion_lider_max_cop", "ampliar_maximo_cop", "control_monto_maximo",
                             "ventas_mensuales_cop", "ampliar_pct_sugerido") if float(nuevos[k]) < 0]
    if negativos:
        raise ValueError(f"Estos parámetros no pueden ser negativos: {negativos}")
    with motor.begin() as cx:
        ultima = cx.execute(select(func.max(reglas_version.c.version))
                            .where(reglas_version.c.paquete == paquete)).scalar() or 0
        res = cx.execute(insert(reglas_version).values(
            paquete=paquete, version=ultima + 1, parametros=nuevos, creada_por=usuario, nota=nota))
    return {"id": res.inserted_primary_key[0], "version": ultima + 1, "parametros": nuevos}


@dataclass
class ResultadoRegla:
    nit: str
    nombre: str
    tipo: str                    # cobrar | congelar_credito | ampliar_cupo
    prioridad: float             # 0 a 100
    recomendacion: str
    motivo: str
    saldo_vencido: float
    evidencia: dict = field(default_factory=dict)
    nivel_aprobacion: str = "lider"   # lider | gerente (matriz de delegación)


TIPOS = {"cobrar": "Cobrar", "congelar_credito": "Frenar crédito", "ampliar_cupo": "Ampliar cupo"}
ROLES = {"lider": 1, "gerente": 2}


def compras_por_cliente(historico: pd.DataFrame, corte: date, meses: int) -> dict[str, float]:
    """Compras de cada cliente en la ventana, sumando cada factura una sola vez aunque aparezca
    en varios cortes. Usa el valor de la factura y, si no viene, el saldo."""
    if historico.empty:
        return {}
    desde = corte - timedelta(days=int(round(meses * 30.4)))
    h = historico[(historico["fecha_emision"] > desde) & (historico["fecha_emision"] <= corte)].copy()
    if h.empty:
        return {}
    valor = h["valor"] if "valor" in h else pd.Series(index=h.index, dtype=float)
    h["monto"] = valor.where(valor.notna() & (valor > 0), h["saldo"])
    por_factura = h.groupby(["nit", "numero"])["monto"].max().reset_index()
    return {str(k): float(v) for k, v in por_factura.groupby("nit")["monto"].sum().items()}


def clientes_clave(compras: dict[str, float], pct: float) -> set[str]:
    """Curva ABC: los clientes que, ordenados de mayor a menor, suman el `pct` % de las compras."""
    total = sum(compras.values())
    if total <= 0:
        return set()
    clave, acumulado = set(), 0.0
    for nit, monto in sorted(compras.items(), key=lambda x: (-x[1], x[0])):
        if acumulado >= total * pct / 100:
            break
        clave.add(nit)
        acumulado += monto
    return clave


def nivel_aprobacion(tipo: str, monto: float, es_clave: bool, p: dict) -> str:
    """Matriz de delegación: quién puede aprobar la decisión."""
    limite = float(p.get("aprobacion_lider_max_cop") or 0)
    if tipo == "congelar_credito" and es_clave and p.get("frenar_clave_requiere_gerente", True):
        return "gerente"
    if limite and monto > limite:
        return "gerente"
    return "lider"


def puede_aprobar(rol: str, nivel: str | None) -> bool:
    return ROLES.get(rol, 0) >= ROLES.get(nivel or "lider", 1)


def resumen_por_cliente(facturas: pd.DataFrame, corte: date, dias_gracia: int) -> pd.DataFrame:
    """Una fila por cliente con saldo total, saldo vencido, mora máxima y última compra."""
    f = facturas[facturas["saldo"] > 0].copy()
    if f.empty:
        return pd.DataFrame(columns=["nit", "saldo_total", "saldo_vencido", "dias_mora_max",
                                     "facturas_vencidas", "ultima_emision"])
    f["dias_mora"] = [(corte - v).days for v in f["fecha_vencimiento"]]
    f["vencida"] = f["dias_mora"] > dias_gracia
    f["saldo_venc"] = f["saldo"].where(f["vencida"], 0.0)
    f["mora_venc"] = f["dias_mora"].where(f["vencida"], 0)
    return f.groupby("nit").agg(
        saldo_total=("saldo", "sum"),
        saldo_vencido=("saldo_venc", "sum"),
        dias_mora_max=("mora_venc", "max"),
        facturas_vencidas=("vencida", "sum"),
        ultima_emision=("fecha_emision", "max"),
    ).reset_index()


def _pesos(x: float) -> str:
    return "$" + f"{x:,.0f}".replace(",", ".")


def evaluar_cartera(facturas: pd.DataFrame, corte: date, parametros: dict,
                    clientes: dict[str, dict], vencido_previo: dict[str, float] | None = None,
                    regla_version: int | None = None,
                    compras: dict[str, float] | None = None) -> list[ResultadoRegla]:
    """Aplica las reglas de cartera y devuelve las decisiones recomendadas, sin tocar la base.

    facturas: columnas nit, numero, fecha_emision, fecha_vencimiento, saldo
    clientes: nit -> {"nombre": str, "cupo": float | None}
    vencido_previo: saldo vencido por NIT en el corte anterior (para la tendencia)
    compras: compras por NIT en los últimos meses (valor del cliente); si no se da, se calcula
             con las facturas del corte
    """
    p = {**parametros_base(), **parametros}
    vencido_previo = vencido_previo or {}
    if compras is None:
        compras = compras_por_cliente(facturas, corte, int(p["meses_valor_cliente"]))
    total_compras = sum(compras.values())
    clave = clientes_clave(compras, float(p["pct_clientes_clave"]))
    max_compras = max(compras.values(), default=0.0) or 1.0
    excepciones = {str(n) for n in p.get("clientes_excepcion", [])}
    res = resumen_por_cliente(facturas, corte, int(p["dias_gracia"]))
    if res.empty:
        return []
    max_vencido = max(res["saldo_vencido"].max(), 1.0)
    limite_compra = corte - timedelta(days=int(p["dias_compra_reciente"]))
    salida: list[ResultadoRegla] = []

    for r in res.itertuples(index=False):
        if r.nit in excepciones:
            continue
        info = clientes.get(r.nit, {})
        nombre = info.get("nombre") or r.nit
        cupo = info.get("cupo")
        previo = vencido_previo.get(r.nit)
        if previo is None:
            tendencia, txt_tend = 0.5, "sin corte anterior para comparar"
        elif r.saldo_vencido > previo * 1.02:
            tendencia, txt_tend = 1.0, f"subió desde {_pesos(previo)} en el corte anterior"
        elif r.saldo_vencido < previo * 0.98:
            tendencia, txt_tend = 0.0, f"bajó desde {_pesos(previo)} en el corte anterior"
        else:
            tendencia, txt_tend = 0.5, "igual que en el corte anterior"
        monto_n = r.saldo_vencido / max_vencido
        mora_n = min(r.dias_mora_max, p["mora_maxima_dias"]) / p["mora_maxima_dias"]
        prioridad = round(100 * (p["peso_monto"] * monto_n + p["peso_mora"] * mora_n
                                 + p["peso_tendencia"] * tendencia), 1)

        compras_cli = float(compras.get(r.nit, 0.0))
        es_clave = r.nit in clave
        participacion = round(100 * compras_cli / total_compras, 1) if total_compras else 0.0
        meses = int(p["meses_valor_cliente"])
        txt_valor = (f"Es cliente clave: {participacion}% de las compras de los últimos {meses} meses "
                     f"({_pesos(compras_cli)})." if es_clave else "")

        f_cli = facturas[(facturas["nit"] == r.nit) & (facturas["saldo"] > 0)].copy()
        f_cli["dias_mora"] = [(corte - v).days for v in f_cli["fecha_vencimiento"]]
        vencidas = f_cli[f_cli["dias_mora"] > p["dias_gracia"]].sort_values("saldo", ascending=False)
        evidencia = {
            "cliente": nombre, "nit": r.nit, "fecha_corte": corte.isoformat(),
            "saldo_total": float(r.saldo_total), "saldo_vencido": float(r.saldo_vencido),
            "dias_mora_max": int(r.dias_mora_max), "facturas_vencidas": int(r.facturas_vencidas),
            "cupo": cupo, "ultima_factura_emitida": r.ultima_emision.isoformat(),
            "tendencia": txt_tend, "saldo_vencido_corte_anterior": previo,
            "componentes_prioridad": {"monto": round(monto_n, 3), "mora": round(mora_n, 3),
                                      "tendencia": tendencia},
            "regla_version": regla_version,
            "compras_ventana": compras_cli, "meses_valor_cliente": meses,
            "participacion_compras_pct": participacion, "cliente_clave": es_clave,
            "facturas": [
                {"numero": str(x.numero), "vencimiento": x.fecha_vencimiento.isoformat(),
                 "dias_mora": int(x.dias_mora), "saldo": float(x.saldo)}
                for x in vencidas.head(15).itertuples(index=False)
            ],
        }

        if r.saldo_vencido >= p["umbral_cobro_minimo"]:
            if prioridad >= 70:
                accion = "Llamar hoy al responsable de pagos y cerrar un acuerdo de pago con fecha."
            elif prioridad >= 40:
                accion = "Enviar estado de cuenta y programar llamada de cobro esta semana."
            else:
                accion = "Enviar recordatorio de pago por correo con el estado de cuenta."
            if es_clave:
                accion += " Cliente clave: que la gestión la haga quien lleva la relación comercial."
            motivo = (f"{nombre} tiene {_pesos(r.saldo_vencido)} vencidos en {int(r.facturas_vencidas)} "
                      f"factura(s); la más antigua lleva {int(r.dias_mora_max)} días de mora. "
                      f"Saldo vencido {txt_tend}. {txt_valor}").strip()
            salida.append(ResultadoRegla(r.nit, nombre, "cobrar", prioridad, accion, motivo,
                                         float(r.saldo_vencido), evidencia,
                                         nivel_aprobacion("cobrar", float(r.saldo_vencido), es_clave, p)))

        razones = []
        if r.dias_mora_max >= p["umbral_congelar_dias"] and r.ultima_emision >= limite_compra:
            razones.append(f"lleva {int(r.dias_mora_max)} días de mora y sigue comprando "
                           f"(última factura {r.ultima_emision.isoformat()})")
        buen_pagador = r.dias_mora_max <= p["ampliar_mora_maxima_dias"]
        # Un cliente que paga al día y se pasó del cupo no se frena: se le recomienda ampliar el cupo.
        ampliar_en_vez_de_frenar = bool(p.get("ampliar_activo")) and buen_pagador
        if (p.get("congelar_por_sobrecupo") and cupo and r.saldo_total > cupo
                and not ampliar_en_vez_de_frenar):
            razones.append(f"su saldo total {_pesos(r.saldo_total)} supera el cupo de {_pesos(cupo)}")
        if razones:
            motivo = f"{nombre} " + " y ".join(razones) + "."
            accion = "Frenar nuevos despachos a crédito hasta que haya un acuerdo de pago."
            if es_clave:
                motivo += " " + txt_valor
                accion = ("Revisar con el gerente antes de frenar: es cliente clave. Propuesta: frenar nuevos "
                          "despachos a crédito hasta cerrar un acuerdo de pago con fecha.")
            salida.append(ResultadoRegla(
                r.nit, nombre, "congelar_credito", prioridad, accion, motivo, float(r.saldo_vencido),
                evidencia, nivel_aprobacion("congelar_credito", float(r.saldo_vencido), es_clave, p)))

        # Ampliar cupo: buen pagador, compra seguido y ya usa casi todo su cupo.
        uso = (r.saldo_total / cupo) if cupo else 0.0
        if (p.get("ampliar_activo") and cupo and buen_pagador and uso >= p["ampliar_uso_minimo"]
                and r.ultima_emision >= limite_compra):
            aumento = min(float(p["ampliar_maximo_cop"]), cupo * float(p["ampliar_pct_sugerido"]))
            aumento = max(1_000_000.0, round(aumento / 1_000_000) * 1_000_000)
            nuevo = cupo + aumento
            prio_amp = round(100 * (0.5 * min(uso, 1.0) + 0.5 * compras_cli / max_compras), 1)
            ev_amp = {**evidencia, "uso_cupo_pct": round(100 * uso, 1), "aumento_sugerido": aumento,
                      "cupo_sugerido": nuevo}
            motivo = (f"{nombre} usa el {100 * uso:.0f}% de su cupo ({_pesos(r.saldo_total)} de {_pesos(cupo)}), "
                      f"no tiene facturas en mora y compró por {_pesos(compras_cli)} en los últimos {meses} meses "
                      f"(última factura {r.ultima_emision.isoformat()}). Con el cupo actual, sus próximas compras "
                      f"quedarían frenadas.")
            salida.append(ResultadoRegla(
                r.nit, nombre, "ampliar_cupo", prio_amp,
                f"Ampliar el cupo de {_pesos(cupo)} a {_pesos(nuevo)} (+{_pesos(aumento)}).",
                motivo, float(r.saldo_vencido), ev_amp,
                nivel_aprobacion("ampliar_cupo", aumento, es_clave, p)))

    return sorted(salida, key=lambda x: x.prioridad, reverse=True)
