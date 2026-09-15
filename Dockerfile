# Habla con tu Censo — imagen de la aplicación (FastAPI + uvicorn).
#
# La imagen lleva SOLO el código del tag. Las cuatro bases DuckDB, el .env y logs/
# se montan desde afuera (ver docker-compose.yml y DESPLIEGUE.md): una imagen sin
# datos ni claves se puede publicar o copiar sin riesgo.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Usuario sin privilegios. uid fijo para que los permisos de los volúmenes sean
# predecibles en cualquier servidor. El home tiene que ser escribible: DuckDB guarda
# sus extensiones en ~/.duckdb (con home en /app, que es de root, los tests fallan).
RUN groupadd --system --gid 10001 censo \
 && useradd --system --uid 10001 --gid censo --create-home --home-dir /home/censo \
            --shell /usr/sbin/nologin censo

WORKDIR /app

# Dependencias primero (capa cacheable). constraints.txt fija las versiones EXACTAS
# con las que corre la instancia de referencia; requirements.txt dice qué se instala.
COPY requirements.txt despliegue/constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt

# El código. La app resuelve rutas relativas a /app (app/static, datos/diccionario.json,
# esquemas): el WORKDIR no es opcional.
COPY --chown=root:root . .

# logs/ es el único directorio donde la app escribe. Existe en la imagen con dueño
# censo para que el volumen con nombre herede ese dueño al crearse.
RUN mkdir -p /app/logs && chown censo:censo /app/logs \
 && chmod 0755 despliegue/entrypoint.sh

USER censo

# La extensión sqlite de DuckDB NO la usa la app (abre las bases nativas .duckdb),
# pero sí los tests del ejecutor, que comparan contra una base SQLite mínima. Se
# instala en el build para que tests/ corra igual sin salida a internet.
RUN python -c "import duckdb; duckdb.connect().execute('INSTALL sqlite')"

EXPOSE 8010

ENTRYPOINT ["despliegue/entrypoint.sh"]
# Un solo worker: la caché de respuestas vive en memoria del proceso. El tope de gasto
# NO depende de esto (lee el log compartido), pero la caché sí.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
