# Ontara · Cartera

Ontara, de C&M LABS, convierte los datos de una empresa en **decisiones trazables**: qué hacer, por qué y con qué
evidencia. Este repositorio es el MVP del primer paquete de decisión: **Ontara Cartera**, que le dice al líder de
cartera a quién cobrarle primero y a quién frenarle el crédito, y mide el resultado en pesos.

**Regla de oro:** la IA no es la fuente de la verdad. Todas las cifras salen de reglas determinísticas; Claude solo
consulta resultados ya calculados, los explica y redacta mensajes.

## Cómo está organizado (modelo de 3 capas)

| Capa | Módulo | Qué hace |
| --- | --- | --- |
| 1. Integración | `ontara/ingesta.py` | Lee Excel/CSV de Siigo, World Office, SAP o Excel propio. Entiende pesos `$ 1.234.567,50`, NIT con dígito de verificación y fechas día/mes/año |
| 1. Integración | `ontara/calidad.py` | Semáforo de calidad con 8 validaciones; muestra el número de fila para corregir en el origen |
| 2. Ontología | `ontara/ontologia.py` + `plantillas/comercial.yaml` | Traduce columnas al lenguaje del negocio (cliente, factura, saldo) por sinónimos, sin IA |
| 2. Reglas | `ontara/reglas.py` + `reglas/cartera.yaml` | Prioridad de cobro (monto × mora × tendencia) y regla de frenar crédito. Parámetros versionados |
| 2. Decisiones | `ontara/decisiones.py` | Memoria de decisiones: evidencia, aprobación, ejecución, cierre automático y resultado medido |
| 3. Aplicación | `ontara/impacto.py` | Línea base (primera carga) e impacto en pesos, separando decisiones atendidas y no atendidas |
| 3. Aplicación | `ontara/asistente.py` | Claude con 4 herramientas de solo lectura; nombres y NIT anonimizados antes de salir (Ley 1581) |
| 3. Aplicación | `app/streamlit_app.py` | Bandeja de decisiones, carga de datos, impacto, asistente y reglas |

## Probarlo en local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app/streamlit_app.py
```

En **Cargar datos** sube `datos_ejemplo/cartera_corte_2026-10-31.csv` (fecha de corte 31/10/2026) y luego
`cartera_corte_2026-11-07.csv` (07/11/2026). Los datos son inventados.

Pruebas automáticas:

```bash
pytest
```

## Configuración (variables de entorno)

| Variable | Para qué | Por defecto |
| --- | --- | --- |
| `DATABASE_URL` | Base del cliente. En producción, Cloud SQL: `postgresql+psycopg://usuario:clave@/ontara?host=/cloudsql/PROYECTO:REGION:INSTANCIA` | SQLite local `ontara.db` |
| `ONTARA_EMPRESA` | Nombre que aparece en la app | Empresa de demostración |
| `ONTARA_USUARIOS` | Usuarios y roles en JSON: `{"maria": {"clave_sha256": "<hash>", "rol": "lider"}}`. Roles: `gerente` (puede cambiar reglas) y `lider` | Sin login (modo demo) |
| `ANTHROPIC_API_KEY` | Activa el asistente | Asistente apagado |
| `ONTARA_MODELO` | Modelo de Claude | `claude-opus-5-5` |
| `ONTARA_ESFUERZO` | Esfuerzo del modelo (`low`, `medium`, `high`) | `medium` |
| `ONTARA_ANONIMIZAR_IA` | `0` para enviar nombres reales a la API (no recomendado) | `1` |

Para generar el hash de una clave: `python -c "import hashlib; print(hashlib.sha256(b'MiClave').hexdigest())"`.

## Desplegar en Google Cloud (un cliente = un servicio)

```bash
gcloud run deploy ontara-CLIENTE --source . --region us-central1 \
  --set-env-vars ONTARA_EMPRESA="Nombre del cliente" \
  --set-secrets DATABASE_URL=ontara-CLIENTE-db:latest,ANTHROPIC_API_KEY=anthropic-key:latest,ONTARA_USUARIOS=ontara-CLIENTE-usuarios:latest \
  --add-cloudsql-instances PROYECTO:REGION:INSTANCIA
```

## Pendiente antes de usar datos reales

- Revisión de Ley 1581 con abogado (C&M LABS actúa como encargado del tratamiento).
- Configurar `ONTARA_USUARIOS`; el modo demo no tiene login.
- Ajustar los parámetros de `reglas/cartera.yaml` con el líder de cartera del cliente piloto.
