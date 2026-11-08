"""Ontara · C&M LABS

Convierte los datos de una empresa en decisiones trazables:
qué hacer, por qué y con qué evidencia.

Capas:
    1. Integración      -> ontara.ingesta, ontara.calidad
    2. Ontología/reglas -> ontara.ontologia, ontara.reglas, ontara.decisiones
    3. Aplicación       -> ontara.impacto, ontara.asistente, app/streamlit_app.py

Regla de oro: la IA no es la fuente de la verdad. Todo número sale de las
capas 1 y 2; el asistente solo lee resultados ya calculados.
"""

__version__ = "0.1.0"
