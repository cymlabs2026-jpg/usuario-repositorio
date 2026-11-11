"""Punto de entrada de la versión de prueba en la web (Streamlit Community Cloud).

Activa el modo demostración (datos de prueba inventados ya cargados, sin login) y abre la app normal.
El despliegue de un cliente (Dockerfile / Cloud Run) usa app/streamlit_app.py y no pasa por aquí.
"""
import os
import runpy
from pathlib import Path

os.environ.setdefault("ONTARA_DEMO", "1")
os.environ.setdefault("ONTARA_EMPRESA", "Distribuidora de prueba (datos inventados)")
runpy.run_path(str(Path(__file__).resolve().parent / "app" / "streamlit_app.py"), run_name="__main__")
