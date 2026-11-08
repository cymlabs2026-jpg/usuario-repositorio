# Ontara · Cartera

Ontara, de C&M LABS, convierte los datos de una empresa en **decisiones trazables**: qué hacer, por qué y con qué
evidencia. Este repositorio es el MVP del primer paquete de decisión: **Ontara Cartera** (crédito y cobro para crecer),
que le dice al líder de cartera a quién cobrarle primero, a quién frenarle el crédito y a qué buenos pagadores
ampliarles el cupo para que sigan comprando. Mide el resultado en pesos y en caja liberada.

**Regla de oro:** la IA no es la fuente de la verdad. Todas las cifras salen de reglas determinísticas; Claude solo
consulta resultados ya calculados, los explica y redacta mensajes.

## Cómo está organizado (modelo de 3 capas)

| Capa | Módulo | Qué hace |
| --- | --- | --- |
| 1. Integración | `ontara/ingesta.py` | Lee Excel/CSV de Siigo, World Office, SAP o Excel propio. Entiende pesos `$ 1.234.567,50`, NIT con dígito de verificación y fechas día/mes/año |
| 1. Integración | `ontara/calidad.py` | Semáforo de calidad con 8 validaciones; muestra el número de fila para corregir en el origen |
| 2. Ontología | `ontara/ontologia.py` + `plantillas/comercial.yaml` | Traduce columnas al lenguaje del negocio (cliente, factura, saldo) por sinónimos, sin IA |
| 2. Reglas | `ontara/reglas.py` + `reglas/cartera.yaml` | Prioridad de cobro (monto × mora × tendencia), frenar crédito y ampliar cupo; clientes clave por curva ABC de compras; matriz de delegación. Parámetros versionados |
| 2. Decisiones | `ontara/decisiones.py` | Memoria de decisiones: evidencia, aprobación según el rol, ejecución, cierre automático, grupo de control y resultado medido |
| 3. Aplicación | `ontara/impacto.py` | Línea base (primera carga), impacto en pesos, días de cartera, caja liberada y comparación contra el grupo de control |
| 3. Aplicación | `ontara/acceso.py` | Login con Google o Microsoft (o clave PBKDF2), roles y bitácora de accesos |
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

## Las tres decisiones del paquete

| Decisión | Cuándo aparece | Quién la aprueba |
| --- | --- | --- |
| Cobrar | Saldo vencido (después de los días de gracia) mayor al umbral mínimo | Líder hasta `aprobacion_lider_max_cop`; por encima, gerente |
| Frenar crédito | Mora mayor a `umbral_congelar_dias` y sigue comprando, o saldo por encima del cupo **con mora** | Gerente si es cliente clave; si no, igual que cobrar |
| Ampliar cupo | Sin facturas en mora, compró en los últimos `dias_compra_reciente` días y usa al menos `ampliar_uso_minimo` del cupo. Un buen pagador pasado del cupo recibe esta decisión en vez de "frenar" | Líder hasta el monto; por encima, gerente |

**Clientes clave:** los que, ordenados por compras de los últimos `meses_valor_cliente` meses, suman el
`pct_clientes_clave` % de las compras. Las compras se acumulan entre cortes (cada factura cuenta una vez), así que
el dato mejora con cada carga.

**Grupo de control:** el `pct_grupo_control` % de las decisiones de cobro con saldo vencido hasta
`control_monto_maximo` no se muestra en la bandeja. Al comparar cuánto se recupera con y sin la bandeja se obtiene
el efecto real de Ontara. La asignación es reproducible y, si la deuda supera el tope, la decisión sale del control.

**Caja liberada:** días de cartera ganados frente a la línea base × ventas diarias. Si se registran las ventas
mensuales de contabilidad (`ventas_mensuales_cop`) se usan esas; si no, se estiman con las facturas emitidas en los
últimos 30 días de los cortes cargados. Con los datos de ejemplo la cifra sale negativa porque son inventados.

## Configuración (variables de entorno)

| Variable | Para qué | Por defecto |
| --- | --- | --- |
| `DATABASE_URL` | Base del cliente. En producción, Cloud SQL: `postgresql+psycopg://usuario:clave@/ontara?host=/cloudsql/PROYECTO:REGION:INSTANCIA` | SQLite local `ontara.db` |
| `ONTARA_EMPRESA` | Nombre que aparece en la app | Empresa de demostración |
| `ONTARA_USUARIOS` | Usuarios autorizados y su rol en JSON (ver abajo). Roles: `gerente` (aprueba montos altos y cambia reglas) y `lider` | Sin login (modo demo) |
| `ANTHROPIC_API_KEY` | Activa el asistente | Asistente apagado |
| `ONTARA_MODELO` | Modelo de Claude | `claude-opus-5-5` |
| `ONTARA_ESFUERZO` | Esfuerzo del modelo (`low`, `medium`, `high`) | `medium` |
| `ONTARA_ANONIMIZAR_IA` | `0` para enviar nombres reales a la API (no recomendado) | `1` |

### Login

```json
{"maria@empresa.com": {"rol": "lider"},
 "gerente@empresa.com": {"rol": "gerente"},
 "contador": {"rol": "lider", "clave": "pbkdf2_sha256$600000$..."}}
```

- **Con Google o Microsoft (recomendado):** crea `.streamlit/secrets.toml` con la sección `[auth]` (`redirect_uri`,
  `cookie_secret`) y `[auth.google]` o `[auth.microsoft]` (`client_id`, `client_secret`, `server_metadata_url`), según la
  [documentación de Streamlit](https://docs.streamlit.io/develop/concepts/connections/authentication). Solo entran los
  correos que estén en `ONTARA_USUARIOS`. En Cloud Run, monta el archivo desde Secret Manager.
- **Con usuario y clave:** genera el hash con `python -m ontara.acceso "LaClave"` y ponlo en `clave`. Los hashes SHA-256
  simples de la versión anterior ya no se aceptan.
- Cada ingreso, intento fallido, cuenta no autorizada y salida queda en la bitácora (página **Accesos**, solo gerente).

## Desplegar en Google Cloud (un cliente = un servicio)

```bash
gcloud run deploy ontara-CLIENTE --source . --region us-central1 \
  --set-env-vars ONTARA_EMPRESA="Nombre del cliente" \
  --set-secrets DATABASE_URL=ontara-CLIENTE-db:latest,ANTHROPIC_API_KEY=anthropic-key:latest,ONTARA_USUARIOS=ontara-CLIENTE-usuarios:latest \
  --add-cloudsql-instances PROYECTO:REGION:INSTANCIA
```

## Pendiente antes de usar datos reales

- Revisión de Ley 1581 con abogado (C&M LABS actúa como encargado del tratamiento).
- Configurar `ONTARA_USUARIOS` y el login con Google o Microsoft; el modo demo no tiene login.
- Registrar las ventas mensuales en Reglas y acordar con el gerente el monto de delegación y el % de grupo de control.
- Ajustar los parámetros de `reglas/cartera.yaml` con el líder de cartera del cliente piloto.
