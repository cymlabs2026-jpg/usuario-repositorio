"""Piso 1 (Datos) y piso 4 (Decisiones): esquema de la base de datos del cliente.

Funciona igual en PostgreSQL (producción) y SQLite (local y pruebas).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, MetaData,
    String, Table, Text, create_engine, inspect, text,
)
from sqlalchemy.engine import Engine

from . import config

metadata = MetaData()


def ahora() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- Capa 1: integración ----------------------------------------------------
cargas = Table(
    "cargas", metadata,
    Column("id", Integer, primary_key=True),
    Column("creada_en", DateTime, nullable=False, default=ahora),
    Column("fecha_corte", Date, nullable=False),
    Column("archivo", String(255)),
    Column("usuario", String(120)),
    Column("filas_total", Integer, nullable=False),
    Column("filas_validas", Integer, nullable=False),
    Column("semaforo", String(10), nullable=False),        # verde | amarillo | rojo
    Column("calidad", JSON, nullable=False),                # detalle de cada validación
    Column("mapeo", JSON, nullable=False),                  # campo estándar -> columna original
)

# --- Capa 2: ontología ------------------------------------------------------
clientes = Table(
    "clientes", metadata,
    Column("nit", String(20), primary_key=True),
    Column("nombre", String(255), nullable=False),
    Column("cupo", Float),
    Column("vendedor", String(120)),
    Column("actualizado_en", DateTime, default=ahora),
)

facturas = Table(
    "facturas", metadata,
    Column("id", Integer, primary_key=True),
    Column("carga_id", Integer, ForeignKey("cargas.id"), nullable=False, index=True),
    Column("nit", String(20), nullable=False, index=True),
    Column("numero", String(60), nullable=False),
    Column("fecha_emision", Date, nullable=False),
    Column("fecha_vencimiento", Date, nullable=False),
    Column("valor", Float),
    Column("saldo", Float, nullable=False),
    Column("vendedor", String(120)),
)

reglas_version = Table(
    "reglas_version", metadata,
    Column("id", Integer, primary_key=True),
    Column("paquete", String(40), nullable=False),
    Column("version", Integer, nullable=False),
    Column("parametros", JSON, nullable=False),
    Column("creada_en", DateTime, default=ahora),
    Column("creada_por", String(120)),
    Column("nota", Text),
)

# --- Memoria de decisiones ----------------------------------------------------
decisiones = Table(
    "decisiones", metadata,
    Column("id", Integer, primary_key=True),
    Column("paquete", String(40), nullable=False),          # cartera, margen, ...
    Column("tipo", String(40), nullable=False),             # cobrar, congelar_credito
    Column("nit", String(20), nullable=False, index=True),
    Column("estado", String(20), nullable=False),           # ver ESTADOS en decisiones.py
    Column("prioridad", Float, nullable=False),
    Column("recomendacion", Text, nullable=False),
    Column("motivo", Text, nullable=False),
    Column("evidencia", JSON, nullable=False),
    Column("regla_version_id", Integer, ForeignKey("reglas_version.id"), nullable=False),
    Column("carga_origen_id", Integer, ForeignKey("cargas.id"), nullable=False),
    Column("carga_actual_id", Integer, ForeignKey("cargas.id"), nullable=False),
    Column("saldo_vencido_inicial", Float, nullable=False),
    Column("nivel_aprobacion", String(20), nullable=False, default="lider",
           server_default="lider"),                         # lider | gerente (matriz de delegación)
    Column("grupo", String(20), nullable=False, default="tratamiento",
           server_default="tratamiento"),                   # tratamiento | control | fuera
    Column("creada_en", DateTime, default=ahora),
    Column("actualizada_en", DateTime, default=ahora),
)

acciones = Table(
    "acciones", metadata,
    Column("id", Integer, primary_key=True),
    Column("decision_id", Integer, ForeignKey("decisiones.id"), nullable=False, index=True),
    Column("tipo", String(30), nullable=False),             # aprobar, ajustar, descartar, ejecutar, nota, sistema
    Column("usuario", String(120), nullable=False),
    Column("detalle", Text),
    Column("fecha", DateTime, default=ahora),
)

resultados = Table(
    "resultados", metadata,
    Column("id", Integer, primary_key=True),
    Column("decision_id", Integer, ForeignKey("decisiones.id"), nullable=False, index=True),
    Column("carga_id", Integer, ForeignKey("cargas.id"), nullable=False),
    Column("saldo_vencido_antes", Float, nullable=False),
    Column("saldo_vencido_despues", Float, nullable=False),
    Column("pesos_recuperados", Float, nullable=False),
    Column("atendida", Boolean, nullable=False),            # hubo aprobación o ejecución antes de medir
    Column("medido_en", DateTime, default=ahora),
)

linea_base = Table(
    "linea_base", metadata,
    Column("id", Integer, primary_key=True),
    Column("carga_id", Integer, ForeignKey("cargas.id"), nullable=False),
    Column("metricas", JSON, nullable=False),
    Column("creada_en", DateTime, default=ahora),
)

consultas_ia = Table(
    "consultas_ia", metadata,
    Column("id", Integer, primary_key=True),
    Column("usuario", String(120), nullable=False),
    Column("pregunta", Text, nullable=False),
    Column("herramientas", JSON, nullable=False),           # qué herramientas usó Claude
    Column("respuesta", Text),
    Column("anonimizado", Boolean, nullable=False),
    Column("modelo", String(60)),
    Column("fecha", DateTime, default=ahora),
)


accesos = Table(
    "accesos", metadata,
    Column("id", Integer, primary_key=True),
    Column("usuario", String(255), nullable=False),
    Column("evento", String(20), nullable=False),           # ingreso | fallido | salida | denegado
    Column("metodo", String(20), nullable=False),           # google | microsoft | clave | demo
    Column("detalle", Text),
    Column("fecha", DateTime, default=ahora),
)


def _migrar(motor: Engine) -> None:
    """Agrega a una base existente las columnas nuevas que tienen valor por defecto.
    Suficiente para el piloto; con varios clientes en producción conviene pasar a Alembic."""
    insp = inspect(motor)
    with motor.begin() as cx:
        for tabla in metadata.sorted_tables:
            if not insp.has_table(tabla.name):
                continue
            existentes = {c["name"] for c in insp.get_columns(tabla.name)}
            for col in tabla.columns:
                if col.name in existentes or col.server_default is None:
                    continue
                tipo = col.type.compile(dialect=motor.dialect)
                defecto = col.server_default.arg
                cx.execute(text(f"ALTER TABLE {tabla.name} ADD COLUMN {col.name} {tipo} "
                                f"NOT NULL DEFAULT '{defecto}'"))


def crear_motor(url: str | None = None) -> Engine:
    motor = create_engine(url or config.database_url(), future=True)
    metadata.create_all(motor)
    _migrar(motor)
    return motor
