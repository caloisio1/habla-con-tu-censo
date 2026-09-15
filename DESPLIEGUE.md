# DESPLIEGUE — Habla con tu Censo

Cómo instalar la app desde cero, a partir del tag, en un servidor propio, con Docker
Compose. Para la superficie de ataque y los datos que se guardan, ver
[SEGURIDAD.md](SEGURIDAD.md).

> **Estado de este documento (15-sep-2026).** Todo lo que sigue se ejecutó en un stack
> aislado de Docker (Docker 29.1.3, Compose v5.0.0, Linux x86_64), a partir de los
> archivos del tag y las cuatro bases. **Todavía no se probó en un servidor limpio
> instalado por otra persona**: esa prueba está pendiente, con su lista de
> verificación, en la [sección 11](#11-pendiente-prueba-desde-cero-en-un-servidor-descartable).

---

## 1. Qué se instala

```
internet ──80/443──▶ nginx ──8010 (red interna)──▶ app (FastAPI/uvicorn)
                                                    │
                                                    ├─▶ /datos/*.duckdb (solo lectura)
                                                    ├─▶ /app/logs (volumen)
                                                    └─▶ api.openai.com (HTTPS)

navegador del usuario ──▶ basemaps.cartocdn.com (teselas) y cdnjs.cloudflare.com (JS)
```

Son dos contenedores:

- `app`: imagen construida con el `Dockerfile` de la raíz.
- `nginx`: imagen construida con `despliegue/nginx/Dockerfile`. Aplica el tope de
  frecuencia, las cabeceras y el TLS.

---

## 2. Requisitos

| | Medido | Recomendado |
|---|---|---|
| **Sistema** | Linux x86_64 con Docker Engine y el plugin Compose v2 | Ubuntu 24.04 LTS. En arm64 no se probó. |
| **CPU** | La instancia de referencia tiene 8 vCPU | 2 vCPU. **No medido**: la demora de una pregunta la domina el modelo (~40 s sin caché). |
| **RAM** | Contenedor app: 80 MiB en reposo, 316 MiB de pico durante la capa A de la batería. nginx: 8 MiB. Proceso de la instancia de referencia tras 6 h con caché: 415 MB. | 2 GB |
| **Disco** | Imágenes: 536 MB (app) + 93 MB (nginx). Bases: 1,16 GB. `usage.jsonl` en la referencia: 1,6 MB. Logs de Docker: hasta 100 MB por servicio (10 × 10 MB). | 5 GB libres |
| **Puertos de entrada** | 80/tcp y 443/tcp | |
| **Salida** | El servidor necesita 443/tcp hacia `api.openai.com`. Al construir, además, Docker Hub, PyPI y `extensions.duckdb.org`. | |
| **DNS** | Solo para https: registro A (y AAAA si hay IPv6) del dominio apuntando al servidor. | |

---

## 3. Datos: qué hay que copiar

Las cuatro bases **no están en el repositorio ni en la imagen**. Hay que pedirlas al
responsable de la instancia de referencia y copiarlas **con estos nombres exactos** en
el directorio `datos-censo/`, junto a `docker-compose.yml` (u otro directorio, fijado
con `DATOS_CENSO` en `.env`):

| Archivo | Bytes | SHA-256 | Censo | Origen |
|---|---:|---|---|---|
| `censo.duckdb` | 344.731.648 | `9e39550793051047e40205b0265b6924e73a915b16d10f1ec98ba6478fda1fd9` | 2011 | Microdatos INE, `Base unificada Viv_Hog_Pers.sav` (820 MB). Se reconstruye con `datos/cargar.py --duckdb` (en el repo). |
| `censo1996.duckdb` | 373.305.344 | `67e6e26f858044d38d9691699cb8e5c67fa3fd258842c1c66dbb19008afbd58a` | 1996 | Microdatos INE, Censo 1996. Script de construcción **fuera del repo**. |
| `censo2004.duckdb` | 62.140.416 | `acc6604f5fdfbd47c108e54c330d9c02eceb58ce566227428cc26191b37286f9` | 2004 (Fase I) | Microdatos INE en DBF (`cpv04_*`). Script de construcción **fuera del repo**. |
| `censo2023.duckdb` | 383.004.672 | `d63a476b71ef4507c7caa0df38d24234aecb9a4bc19f9b4d312e9fa5d7224a8a` | 2023 (ponderado) | Microdatos INE 2023 más el nomenclátor. Script de construcción **fuera del repo**. |

Para desplegar **no** hacen falta los microdatos originales: solo estos cuatro
archivos.

Verificar la copia:

```sh
cd datos-censo
cat > SHA256SUMS <<'EOF'
9e39550793051047e40205b0265b6924e73a915b16d10f1ec98ba6478fda1fd9  censo.duckdb
67e6e26f858044d38d9691699cb8e5c67fa3fd258842c1c66dbb19008afbd58a  censo1996.duckdb
acc6604f5fdfbd47c108e54c330d9c02eceb58ce566227428cc26191b37286f9  censo2004.duckdb
d63a476b71ef4507c7caa0df38d24234aecb9a4bc19f9b4d312e9fa5d7224a8a  censo2023.duckdb
EOF
sha256sum -c SHA256SUMS
chmod 644 *.duckdb
cd ..
```

Algunos detalles:

- La configuración nombra archivos `.db` (`/datos/censo2023.db`), pero la app abre el
  `.duckdb` con el mismo nombre (`comun/ejecutor.py`, `nativa_de`). Los `.db` no
  existen y no hacen falta.
- El contenedor lee las bases con el uid 10001. Con permisos `644` alcanza.
- Si falta una base, el contenedor **no arranca** e indica cuál.
- Lo demás que la app lee en tiempo de ejecución ya está en el repo: diccionarios,
  esquemas, `cols_*.json`, GeoJSON y `app/static/`.

---

## 4. Variables (`.env`)

```sh
cp .env.example .env
chmod 600 .env
```

### Obligatorias

| Variable | Si falta |
|---|---|
| `OPENAI_API_KEY` | El contenedor app no arranca: `ERROR: OPENAI_API_KEY vacía`. |
| `DOMINIO` | `docker compose` no arranca: `required variable DOMINIO is missing a value`. En modo http se puede usar `_` (cualquier nombre). |

### Recomendadas

| Variable | Por defecto si falta | Nota |
|---|---|---|
| `CARTO_BASEMAP_KEY` | vacía | Sin clave, los mapas salen con la marca de agua "API KEY REQUIRED". Clave gratuita en carto.com/basemaps/apikey. |
| `CENSO_MODELO_SQL` / `CENSO_MODELO_REDACTOR` | `gpt-5.5` / `gpt-5.5` | `.env.example` trae la configuración de la instancia de referencia (`gpt-5.5` / `gpt-5.4-mini`). |
| `CENSO_ESFUERZO_SQL` / `CENSO_ESFUERZO_REDACTOR` | `high` / `low` | |
| `CENSO_TOPE_USD_MES` / `CENSO_TOPE_USD_DIA` | `300` / `25` | Tope de gasto. Cuando se alcanza, no se llama al modelo. |

### Despliegue

Las lee Compose.

| Variable | Por defecto | |
|---|---|---|
| `NGINX_MODO` | `http` | `http`: solo el 80. `https`: el 80 redirige al 443 (requiere el certificado, §6). |
| `PUERTO_HTTP` / `PUERTO_HTTPS` | `80` / `443` | Admiten IP: `127.0.0.1:18080` publica solo en local. |
| `DATOS_CENSO` | `./datos-censo` | Directorio del host con las cuatro bases. |
| `LETSENCRYPT_DIR` | `/etc/letsencrypt` | Certificados del host (modo https). |

Con Compose se **ignoran** `CENSO_DB`, `CENSO1996_DB`, `CENSO2004_DB` y
`CENSO2023_DB`: las rutas las fija `docker-compose.yml`.

### Opcionales

Ver el final de `.env.example`.

`CENSO_PRECALENTAR=1` (el valor por defecto) llama al modelo con las preguntas de
ejemplo **en cada arranque**, así que cada arranque gasta.

---

## 5. Instalación

```sh
# 1. Código del tag
git clone --branch endurecimiento-20260915 https://github.com/caloisio1/habla-con-tu-censo.git
cd habla-con-tu-censo

# 2. Datos (§3)
mkdir datos-censo
#    ...copiar las cuatro bases y verificar con sha256sum -c

# 3. Configuración (§4)
cp .env.example .env && chmod 600 .env
#    editar: OPENAI_API_KEY, DOMINIO, CARTO_BASEMAP_KEY

# 4. Construir y levantar
mkdir -p certbot-www
docker compose up -d --build

# 5. Comprobar
docker compose ps            # app: "Up ... (healthy)"; nginx: "Up"
docker compose logs app      # sin líneas ERROR
curl -sI http://DOMINIO/     # 200
```

La primera construcción descarga las imágenes base y los paquetes (en la prueba local
tardó 49 s).

Si el contenedor `app` se reinicia en bucle, `docker compose logs app` indica qué
falta: base, clave o `logs/` no escribible.

---

## 6. HTTPS (Let's Encrypt)

Requiere que `DOMINIO` resuelva al servidor.

```sh
# 1. Arrancar en modo http (NGINX_MODO=http en .env) y pedir el certificado por webroot
sudo apt install certbot
sudo certbot certonly --webroot -w "$PWD/certbot-www" -d DOMINIO \
     --email CORREO --agree-tos --no-eff-email \
     --deploy-hook "docker compose -f $PWD/docker-compose.yml exec -T nginx nginx -s reload"

# 2. Pasar a https
sed -i 's/^NGINX_MODO=.*/NGINX_MODO=https/' .env
docker compose up -d nginx

# 3. Renovación: la hace el timer de certbot del host con el mismo webroot y el hook
sudo certbot renew --dry-run
```

Lo que se verificó con un certificado autofirmado en `LETSENCRYPT_DIR`:

- La plantilla https pasa `nginx -t`.
- `http://` responde 301 a `https://`.
- `/.well-known/acme-challenge/` se sirve por http.
- `https://` responde 200 por HTTP/2, con HSTS.
- `/docs` responde 404.

**No verificado:** la emisión real con Let's Encrypt y la renovación con el hook (§11).

---

## 7. Verificar la instalación: la batería

### Capa A

Determinista, sin modelo, sin costo, segundos:

```sh
docker compose exec -T app python bateria_censos.py A
# esperado:  VERDE  96 controles, 0 fallos
```

Controla:

- las cifras ancla de cada censo contra los totales publicados;
- la supresión;
- el nomenclátor;
- el módulo común.

Si da ROJO, la instalación no está bien (casi siempre, una base que no corresponde).

### Tests unitarios

```sh
docker compose exec -T app python -m pytest -q -p no:cacheprovider tests
# esperado:  370 passed
```

370 es la cifra del tag, verificada en la instancia de referencia (fuera de Docker). En
el contenedor se verificaron 309 antes de agregar `tests/test_endurecimiento.py`: la
cifra de 370 **dentro del contenedor** queda para la prueba de §11.

### Batería completa (capas A y B)

Llama al modelo, **gasta** y cuenta contra el tope. La capa B hace preguntas reales de
punta a punta en los cuatro censos. En la instancia de referencia son 156 controles:
152 deterministas y 4 de narración.

El criterio es **3 de 3 corridas con la caché apagada, por control**. Un control que
pasa 2 de 3 es una falla y no se promedia.

```sh
for n in 1 2 3; do
  docker compose exec -T -e CENSO_CACHE=0 -e BATERIA_SALIDA=logs/corrida_$n.json \
      app python bateria_censos.py AB
done

docker compose exec -T app python - <<'EOF'
import json, collections
t = collections.Counter(); todos = set()
for n in (1, 2, 3):
    for r in json.load(open(f"logs/corrida_{n}.json")):
        k = (r["capa"], r["control"]); todos.add(k); t[k] += bool(r["ok"])
malos = sorted(k for k in todos if t[k] < 3)
print(f"{len(todos)} controles; {len(todos) - len(malos)} a 3/3")
for k in malos:
    print(f"FALLA {t[k]}/3  [{k[0]}] {k[1]}")
EOF
```

Resultado esperado: `156 controles; 156 a 3/3` y ninguna línea `FALLA`.

La receta de conteo se probó con tres corridas de la capa A (`96 controles; 96 a 3/3`).
**La capa B no se corrió en este despliegue** (§11).

---

## 8. Operación

| Tarea | Comando |
|---|---|
| Estado | `docker compose ps` |
| Rechazos del validador | `docker compose logs app \| grep RECHAZO` |
| Métricas de tokens | `docker compose exec app tail logs/usage.jsonl` |
| Reiniciar sin cambiar la configuración | `docker compose restart app` |
| Aplicar un cambio del `.env` | `docker compose up -d app` (ver §9: `restart` **no** alcanza) |
| Actualizar a otro tag | `git fetch --tags && git checkout <tag> && docker compose up -d --build`, y después la capa A |

`logs/usage.jsonl` es también el contador del tope de gasto: borrarlo lo pone en cero.

---

## 9. Rotación de claves

Las dos claves viven solo en `.env` y en el entorno del contenedor `app`.

> **`docker compose restart app` NO toma los valores nuevos del `.env`.** Reinicia el
> contenedor con el entorno con que fue creado. Hay que **recrearlo** con
> `docker compose up -d app`. Verificado: después de `restart`, `printenv` mostró la
> clave vieja; después de `up -d`, la nueva.

Recrear el contenedor vacía la caché en memoria. Si `CENSO_PRECALENTAR=1`, al arrancar
vuelve a llamar al modelo.

### OpenAI

1. Crear la clave nueva en platform.openai.com.
2. Reemplazar `OPENAI_API_KEY=` en `.env`.
3. `docker compose up -d app`, y esperar `healthy` en `docker compose ps`.
4. Confirmar que el contenedor tiene la nueva, mostrando solo el final:
   `docker compose exec -T app python -c "import os; print(os.environ['OPENAI_API_KEY'][-4:])"`
5. Hacer una pregunta desde la página y verificar que responde.
6. Revocar la clave vieja en platform.openai.com.

### CARTO

1. Pedir la clave nueva en carto.com/basemaps/apikey.
2. Reemplazar `CARTO_BASEMAP_KEY=` en `.env`.
3. `docker compose up -d app`.
4. Confirmar que la página la sirve: `curl -s https://DOMINIO/ | grep -o 'const CARTO_KEY = [^;]*'`.
5. Confirmar que una tesela con la clave nueva viene sin marca de agua:
   `curl -s -o t.png "https://a.basemaps.cartocdn.com/light_all/12/1407/2471.png?key=CLAVE"`
   y abrir `t.png`. Sin clave, o con una clave inválida, CARTO devuelve la tesela con la
   marca, con código 200: el mapa no se rompe.
6. La página se sirve con `Cache-Control: no-cache`, así que los navegadores toman la
   clave nueva con una recarga normal.

Límites de CARTO: 5 millones de teselas por mes de uso libre; las teselas raster
(las que usa la app) están en retiro sin fecha publicada. El consumo no se puede medir
desde el servidor, porque el navegador pide las teselas directo a CARTO.

---

## 10. Si cambia el dominio

1. **DNS:** apuntar el dominio nuevo al servidor.
2. **`.env`:** `DOMINIO=nuevo.dominio`.
3. **Certificado** (modo https):
   1. `NGINX_MODO=http` y `docker compose up -d nginx`.
   2. `certbot certonly --webroot ...` para el dominio nuevo (§6).
   3. `NGINX_MODO=https` y `docker compose up -d nginx`.
4. **README:** `README.md` y `README.en.md` enlazan la demo en la línea 11. Cambiarla
   si el dominio nuevo reemplaza la demo.
5. **CARTO:** revisar en su panel si la clave tiene alguna restricción por dominio. No
   se verificó si CARTO ofrece esa restricción.

**No hay que tocar** el código ni la configuración de la app. `git grep` no encuentra
el dominio en ningún archivo de código: la página usa rutas relativas. OpenAI no
depende del dominio.

---

## 11. Pendiente: prueba desde cero en un servidor descartable

**No realizada.** La portabilidad está verificada solo en un stack aislado dentro de la
instancia de referencia, no en un servidor limpio.

Para cerrarla:

1. Alquilar un VPS descartable (Ubuntu 24.04 LTS x86_64, 2 vCPU, 4 GB, 40 GB). Llevar
   **solo** el tag, este documento y los cuatro archivos de datos. Instalar Docker
   según docs.docker.com.
2. Seguir las secciones 3 a 5 **al pie de la letra**. Anotar cada paso que haya hecho
   falta y no esté escrito, y corregir este documento.
3. `docker compose ps`: app `healthy`, nginx `Up`, sin `ERROR` en los logs.
4. **Desde otra máquina, por IPv4 y por IPv6:**
   - `GET /` da 200.
   - `/docs` da 404.
   - `GET /preguntar` da 403.
   - En `docker compose logs nginx` aparece la **IP real** del cliente y no una
     `172.x`.
   - Si aparece `172.x`, el tope de frecuencia es uno solo para todos. Corregirlo (por
     ejemplo, `"userland-proxy": false` en `/etc/docker/daemon.json`, o nginx con
     `network_mode: host`) y documentarlo.
5. Tope de frecuencia desde una IP externa: 40 `POST /preguntar` tienen que devolver
   algunos 429.
6. Certificado real de Let's Encrypt por webroot (§6) y `certbot renew --dry-run` con
   el hook.
7. Capa A: `VERDE 96 controles`. pytest: `370 passed`.
8. Batería AB **tres veces** con `CENSO_CACHE=0` (§7): 3/3 por control. Cualquier 2/3
   o menos es falla. Anotar el costo de las tres corridas a partir de
   `logs/usage.jsonl`.
9. Rotar las dos claves según §9, con una clave temporal de OpenAI.
10. Reiniciar el VPS: los dos contenedores tienen que volver solos
    (`restart: unless-stopped`), y app `healthy`.
11. Medir en ese VPS la demora de una pregunta real y el pico de RAM, y completar §2.
12. Borrar el VPS, confirmar en el panel del proveedor que ya no existe y revocar la
    clave temporal.
