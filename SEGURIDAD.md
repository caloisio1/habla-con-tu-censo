# SEGURIDAD — Habla con tu Censo

Documento para auditoría externa. Describe el código del tag `portable-20260915`
desplegado con `docker-compose.yml` (ver [DESPLIEGUE.md](DESPLIEGUE.md)).

Cada afirmación indica de dónde sale: una línea de código o una prueba ejecutada. Las
pruebas se corrieron el 15-sep-2026 sobre el stack de Docker Compose de este tag,
publicado solo en `127.0.0.1`, con las cuatro bases reales y una clave de OpenAI
inválida (ninguna pregunta llegó a responderse con el modelo).

---

## 1. Flujo de una pregunta

1. El navegador envía `POST /preguntar` (o `/preguntar_stream`) con
   `{"texto": "...", "censo": "2023"}`.
2. nginx aplica el tope de frecuencia y el tamaño máximo del cuerpo, y pasa el pedido
   a la app.
3. La app envía a OpenAI el esquema del censo y el texto de la pregunta. El modelo
   devuelve una sentencia SQL.
4. El validador del censo correspondiente analiza el SQL con `sqlglot` y lo rechaza o
   lo reescribe (sección 3).
5. El SQL validado se ejecuta en DuckDB, sobre la base del censo abierta en solo
   lectura.
6. Se eliminan las filas cuyo conteo crudo está entre 1 y 4.
7. La app envía a OpenAI la pregunta, el SQL validado y hasta 60 filas del resultado
   ya suprimido. El modelo redacta la respuesta.
8. La app devuelve JSON (o eventos SSE) con la respuesta, el SQL ejecutado, las filas
   y, si corresponde, los datos del mapa.

Orden de los pasos 6 y 7 en el código: la supresión (`pipeline.sobre_filas`) está
antes que la llamada al redactor en los tres motores: `consultar_2023.py:529` → `:539`,
`motor_historico.py:622` → `:636`, `app/main.py:641` → `:651`.

---

## 2. Superficie expuesta

### Puertos

| Puerto | Servicio | Exposición |
|---|---|---|
| 80/tcp | nginx | Publicado en el host (`PUERTO_HTTP`). En modo `https` solo redirige a 443 y sirve `/.well-known/acme-challenge/`. |
| 443/tcp | nginx | Publicado en el host (`PUERTO_HTTPS`). Solo escucha en modo `https`. |
| 8010/tcp | app (uvicorn) | Solo en la red interna del compose (`expose`, no `ports`). |

No hay puerto de base de datos: DuckDB es una biblioteca dentro del proceso de la app
y las bases son archivos.

### Endpoints

| Método y ruta | Qué hace | Controles |
|---|---|---|
| `GET /` | Página HTML. Inyecta la clave de CARTO (`app/main.py`, `home()`). | `Cache-Control: no-cache`. |
| `GET /static/*` | Sirve **todo** el contenido de `app/static/`: HTML, CSS, fuentes, logos, GeoJSON y diccionarios JSON. | Cualquier archivo que se copie a ese directorio queda público. |
| `POST /preguntar` | Pregunta. Responde JSON. | 30 pedidos/min por IP, ráfaga de 15, 8 conexiones simultáneas por IP; cuerpo ≤ 16 KB. |
| `POST /preguntar_stream` | La misma pregunta, con respuesta `text/event-stream` (SSE). | Los mismos. |
| `GET /docs`, `/redoc`, `/openapi.json` | Documentación que FastAPI publica por defecto. **La app los expone**; el nginx del compose responde 404. | Si la app se publica sin este nginx, quedan accesibles. |

No hay autenticación, sesiones ni cookies. La app no envía `Set-Cookie`, y
`index.html` no usa `localStorage`, `sessionStorage` ni `document.cookie`.

Entrada:

- `texto`: `str`, sin largo máximo en la app. El único límite es el
  `client_max_body_size 16k` de nginx.
- `censo`: `str`, sin lista de valores. `1996`, `2004` y `2011` van a su motor;
  **cualquier otro valor se procesa como 2023** (`app/main.py`, `_despachar`).
- Si falta `texto`, FastAPI responde 422 con el detalle de validación, que incluye el
  cuerpo recibido.

Resultados de la prueba contra el nginx del compose:

| Pedido | Código |
|---|---|
| `GET /` | 200 |
| `GET /docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect` | 404 |
| `GET /static/x.bak_1` | 404 |
| `GET /preguntar` | 403 |
| `POST /` | 403 |
| `POST /preguntar` con un cuerpo de 20 KB | 413 |
| `POST /preguntar` sin `texto` | 422 |
| 40 `POST /preguntar` simultáneos desde una IP | 8 × 200, 32 × 429 |
| `GET /logs/usage.jsonl`, `/.env`, `/datos/diccionario.json` | 404 |
| `GET /static/../logs/usage.jsonl`, `/static/%2e%2e/.env` (`--path-as-is`) | 404 |
| `GET /static/..%2f..%2fdatos/diccionario.json`, `/static/../../datos/censo2023.duckdb` | 400 |
| Cadenas `sk-` en el HTML de `/` | 0 |

### Cabeceras (nginx del compose)

En los dos modos: `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`,
`Referrer-Policy: strict-origin-when-cross-origin` y `server_tokens off`.

Solo en modo `https`: `Strict-Transport-Security: max-age=86400`, `TLSv1.2` y
`TLSv1.3`, HTTP/2.

No hay `Content-Security-Policy`.

### Tope de frecuencia y dirección IP

El límite se cuenta por `$binary_remote_addr`, que es la IP que ve nginx dentro del
contenedor. En la prueba local (pedidos a `127.0.0.1`), nginx registró `172.18.0.1`,
la puerta de enlace de la red de Docker, y no la IP de origen. **No está verificado**
qué IP registra cuando el pedido llega desde internet. Si fuera la puerta de enlace,
el tope se aplicaría a todos los clientes juntos. Figura en la lista de pendientes de
DESPLIEGUE.md.

### Contenedores

- **app:** corre como uid 10001 (`censo`), sin privilegios. El código en `/app` es de
  root y el usuario de la app no lo puede modificar. Solo puede escribir en
  `/app/logs` (volumen), `/home/censo` y `/tmp`. Las bases se montan en `/datos` en
  solo lectura (`:ro`).
- **nginx:** imagen oficial `nginx:1.28-alpine` con la configuración de
  `despliegue/nginx/`.
- La imagen de la app no contiene `.env`, bases (`*.duckdb`) ni archivos de respaldo
  (`.dockerignore`; verificado con `find` dentro de la imagen).

---

## 3. Validador de SQL

Hay tres validadores, todos basados en `sqlglot`:

- `app/sql_guard.py`: Censo 2011.
- `sql_guard_2023.py`: Censo 2023.
- `sql_guard_historicos.py`: 1996 y 2004, parametrizado por censo.

Cada uno recibe el SQL que generó el modelo y devuelve un SQL reescrito más la lista
de columnas de conteo, o lanza `SQLNoSeguro`. Si lo rechaza, la consulta no se ejecuta.

### Reglas comunes

Tomadas de los docstrings y verificadas por `tests/test_sql_guard*.py`:

1. Si el SQL no se puede analizar, se rechaza.
2. Una sola sentencia, y solo `SELECT`. Se rechazan `UNION`, DML, `PRAGMA`, `ATTACH`,
   `COPY` e `INSTALL`.
3. `SELECT *` está prohibido.
4. Tablas en lista blanca: las del censo más su nomenclátor.
5. Columnas en lista blanca (`cols_*.json`, `datos/diccionario.json`), más los alias
   de la propia consulta.
6. La proyección externa solo admite expresiones agregadas o columnas del `GROUP BY`,
   y exige al menos un `COUNT`: es el conteo crudo con el que se decide la supresión.
7. Identificadores (`hogar_key`, `vivienda_key`, `PERID`, etc.): en la proyección
   externa solo pueden aparecer dentro de `COUNT(DISTINCT ...)`.
8. Si falta `LIMIT`, se agrega uno como resguardo: 50.000 en 2011, 1996 y 2004;
   5.000 en 2023.
9. El `ORDER BY` se completa con todas las columnas de la proyección para que el corte
   del `LIMIT` sea determinista (`comun/orden.py`).

### Reglas propias de 2023

- Prohibido combinar `personas_2023` con `viviendas_2023`.
- La cifra de personas publicada tiene que ser `SUM(W)` (ponderada), y además tiene que
  estar el `COUNT` crudo por celda.
- Las categorías fuera de universo las excluye el validador: no se deja en manos del
  modelo.
- Lista de funciones prohibidas: `load_extension`, `readfile`, `writefile`, `edit`,
  `fsdir`, `zipfile`.

### Ejecución

- DuckDB abre cada base con `read_only=True` (`comun/ejecutor.py:206`).
- Configuración aplicada: `integer_division=true` y `default_null_order`
  (`comun/ejecutor.py:210-211`).
- **No** se configuran `enable_external_access=false` ni `lock_configuration=true`.

### Supresión de celdas

`comun/supresion.py`:

- Una fila con conteo crudo entre 1 y 4 se elimina.
- Una fila con conteo 0 se informa como "sin casos", no como confidencialidad.
- En 2023 la decisión se toma con el `COUNT` crudo aunque la cifra publicada sea
  `SUM(W)`. El conteo crudo no se publica (`_ocultar_n_crudo`).

### Prueba adversarial

Se probaron 20 sentencias contra los cuatro validadores (80 casos). El SQL que pasaba
la validación se ejecutó en un DuckDB en memoria con configuración por defecto, sobre
una tabla ficticia de 100 filas con el nombre de la tabla del censo. No se ejecutó
contra las bases reales.

Rechazadas por los **cuatro** validadores:

- `FROM read_csv('/etc/hostname')`
- `FROM '/etc/hostname'`
- `FROM read_text(...)`
- `read_text(...)` en subconsulta, en `JOIN`, en CTE y en la proyección
- `glob('/root/*')`
- `...; ATTACH '/tmp/x.db'`
- `COPY (...) TO '/tmp/fuga.csv'`
- `INSTALL httpfs`
- `UNION ALL` con `read_text`
- `EXISTS (SELECT 1 FROM duckdb_settings())`
- `information_schema.tables`

2023 rechazó las 20. 2011, 1996 y 2004 dejaron pasar 5 cada uno:

| Sentencia | Resultado al ejecutar |
|---|---|
| `SELECT getenv('CANARIO') AS k, COUNT(*) AS n FROM <tabla> GROUP BY 1` | Error de catálogo: `getenv` no existe en DuckDB 1.5.5 para Python |
| `SELECT COUNT(*) AS n, getenv('CANARIO') AS k FROM <tabla> GROUP BY k` | Ídem |
| `SELECT COUNT(*) AS n FROM <tabla> WHERE getenv('CANARIO') LIKE 's%'` | Ídem |
| `SELECT current_setting('home_directory') AS s, COUNT(*) AS n FROM <tabla> GROUP BY 1` | Se ejecuta y devuelve el valor de esa opción de DuckDB |
| `SELECT COUNT(*) AS n FROM main.<tabla>` | Se ejecuta; es la misma tabla permitida |

En esos tres validadores las funciones escalares no están en lista blanca.

---

## 4. Datos que se guardan y dónde

| Qué | Dónde | Contenido | Rotación |
|---|---|---|---|
| Métricas de uso | Volumen `censo-logs`, `/app/logs/usage.jsonl` | **Por llamada al modelo:** `ts`, `censo`, `etapa`, `modelo`, `esfuerzo`, `prompt_tokens`, `completion_tokens`, `cached_tokens`, `reasoning_tokens`, `consulta_id`. **Por pregunta:** `ts`, `censo`, `consulta_id`, `etapa: "fin"`, `resultado`, `cache`, `veredicto`, `seg`. **No guarda** texto de la pregunta, respuesta ni IP (claves verificadas sobre el log real). | Ninguna. El tope de gasto suma leyendo este archivo: borrarlo pone el contador en cero. |
| Rechazos del validador, preguntas no respondibles y topes alcanzados | stderr de la app → log `json-file` de Docker | **Texto literal de la pregunta**, SQL generado, motivo, censo (`registro.py`) | 10 archivos de 10 MB |
| Log de acceso de uvicorn | stdout de la app → log de Docker | IP del contenedor nginx, método, ruta, código | Ídem |
| Log de acceso de nginx | stdout de nginx → log de Docker | IP de origen (la que ve nginx), fecha, método, ruta, código, bytes, `Referer`, `User-Agent`. No incluye el cuerpo del `POST` (la pregunta). | Ídem |
| Caché de respuestas | Memoria del proceso de la app | Pregunta → SQL y SQL → resultado. Vigencia de 6 h y 500 entradas (`comun/cache.py`). | Se pierde al reiniciar |
| Resultados de la batería | Volumen `censo-logs` (`logs/bateria_censos.json` o `BATERIA_SALIDA`) | Controles, valor obtenido y valor esperado | Ninguna |
| Claves | `.env` en el host, entorno del contenedor de la app | `OPENAI_API_KEY`, `CARTO_BASEMAP_KEY` | Manual (DESPLIEGUE.md §9) |
| Bases de los censos | Directorio del host montado en `/datos:ro` | Microdatos del INE: una fila por persona, vivienda u hogar | La app no los modifica |

En el navegador no se guarda nada.

---

## 5. Qué no sale del servidor

| Dato | Control | Verificación |
|---|---|---|
| Registros individuales | El validador solo admite proyecciones agregadas (§3). | `tests/test_sql_guard*.py`; capa A de `bateria_censos.py` |
| Celdas con conteo crudo 1–4 | Se suprimen antes de llamar al redactor y antes de responder. | Orden en el código (§1); `comun/supresion.py` |
| Conteo crudo por celda en 2023 | `_ocultar_n_crudo` | Código |
| `OPENAI_API_KEY` | Solo en el entorno del contenedor de la app. No está en la imagen ni en el HTML. | `find` en la imagen; 0 cadenas `sk-` en `/` |
| Bases `.duckdb` | Montadas en `/datos`, fuera de `app/static/`. | `GET` con recorrido de ruta → 400/404 |
| Logs | En `/app/logs`, fuera de `app/static/`. | `GET /logs/usage.jsonl` → 404 |

Lo que **sí** sale:

- El SQL ejecutado, en cada respuesta (campo `sql`, `app/main.py:654`).
- La clave de CARTO, en el HTML y en la URL de cada tesela del mapa.

---

## 6. Dependencias externas

| Servicio | Quién lo contacta | Qué recibe |
|---|---|---|
| **OpenAI** (`chat.completions`) | Servidor (contenedor app) | **Etapa SQL:** prompt de sistema con el esquema y las reglas del censo (textos del repo), texto literal de la pregunta y un `prompt_cache_key` fijo por etapa y censo. **Etapa redacción:** pregunta, SQL validado, hasta 60 filas del resultado ya suprimido, totales y leyendas de códigos. **No recibe** la IP del usuario ni un identificador de usuario: `comun/llm.py` envía solo `model`, `reasoning_effort`, `max_completion_tokens`, `messages` y `prompt_cache_key`. Autenticación con `OPENAI_API_KEY`. |
| **OpenAI** (precalentado) | Servidor, al arrancar | Las preguntas de ejemplo de la portada, si `CENSO_PRECALENTAR=1` (valor por defecto). |
| **CARTO** (`{a,b,c,d}.basemaps.cartocdn.com`) | Navegador del usuario | IP, `User-Agent`, `Referer` limitado al origen, coordenadas `z/x/y` de las teselas del área que muestra el mapa y `CARTO_BASEMAP_KEY`. |
| **cdnjs** (`cdnjs.cloudflare.com`) | Navegador del usuario | IP, `User-Agent` y origen. Descarga `marked` 12.0.2 y `leaflet` 1.9.4, **sin** atributo `integrity` (SRI). |
| Docker Hub, PyPI, `extensions.duckdb.org` | Host, al construir las imágenes | Pedidos de las imágenes base, los paquetes y la extensión sqlite de DuckDB |
| Let's Encrypt | Host (certbot), modo `https` | Nombre de dominio |

Las fuentes tipográficas se sirven desde `app/static/`. `github.com` y `gub.uy` son
enlaces: el navegador no los pide hasta que el usuario hace clic.

---

## 7. Límites de costo y de tiempo

- **Tope de gasto** (`comun/presupuesto.py`): USD por mes y por día
  (`CENSO_TOPE_USD_MES`, `CENSO_TOPE_USD_DIA`). Cuando se alcanza, no se llama al
  modelo; la caché sigue respondiendo.
- **Llamadas al modelo:** timeout de 60 s y reintentos solo ante errores transitorios
  (`comun/llm.py`).
- **nginx:** `proxy_read_timeout` de 180 s.

---

## 8. Lo que no está implementado

- Autenticación y cuota por usuario.
- `Content-Security-Policy`.
- SRI en los scripts de cdnjs.
- `enable_external_access=false` y `lock_configuration=true` en DuckDB.
- Lista blanca de funciones escalares en los validadores de 2011, 1996 y 2004.
- Límite de largo de `texto` en la app (solo el de nginx, 16 KB).
- Lista de valores válidos para `censo`.
- Rotación de `logs/usage.jsonl`.
- Verificación de la IP que usa el tope de frecuencia cuando el tráfico llega desde
  internet (§2).
