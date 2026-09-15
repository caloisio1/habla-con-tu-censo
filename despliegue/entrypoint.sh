#!/bin/sh
# Chequeos de arranque del contenedor de la app. Si falta algo, el contenedor NO
# arranca y dice qué falta, en vez de levantar una app que responde mal.
#
# Por qué fallar acá y no dejar que la app lo descubra:
#  - Sin logs/ escribible, usage_log traga el error en silencio (a propósito: el
#    registro no debe romper una consulta) y el tope de gasto, que suma leyendo ese
#    log, deja de ver el gasto. Sería un tope que no corta.
#  - Sin una base, ese censo contesta con error recién cuando alguien pregunta.
set -eu

falta=0

if ! ( : > /app/logs/.escribible ) 2>/dev/null; then
    echo "ERROR: /app/logs no es escribible por el usuario $(id -u). Sin ese log el tope de gasto no suma." >&2
    falta=1
else
    rm -f /app/logs/.escribible
fi

for var in CENSO_DB CENSO1996_DB CENSO2004_DB CENSO2023_DB; do
    eval ruta=\${$var:-}
    if [ -z "$ruta" ]; then
        echo "ERROR: la variable $var no está definida." >&2
        falta=1
        continue
    fi
    # La app abre el .duckdb HERMANO del nombre configurado (comun/ejecutor.py:nativa_de).
    nativa="${ruta%.*}.duckdb"
    if [ ! -r "$nativa" ]; then
        echo "ERROR: $var=$ruta, pero no se puede leer $nativa (¿se copió la base al volumen de datos?)." >&2
        falta=1
    fi
done

if [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "ERROR: OPENAI_API_KEY vacía en el .env. Sin clave ninguna pregunta se puede responder." >&2
    falta=1
fi

if [ -z "${CARTO_BASEMAP_KEY:-}" ]; then
    echo "AVISO: CARTO_BASEMAP_KEY vacía: los mapas salen con la marca de agua de CARTO." >&2
fi

[ "$falta" -eq 0 ] || exit 1
exec "$@"
