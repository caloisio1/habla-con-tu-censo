#!/usr/bin/env bash
# simplificar_segmentos_2023.sh — la cartografía de segmentos, en peso dibujable.
#
# Los 19 GeoJSON de segmentos censales de 2023 salían de la cartografía del INE con
# TODO el detalle: 4.537 polígonos, 1.947.848 vértices, 41,1 MB. A ese peso un mapa
# nacional por segmento no es lento, es imposible: son 41 MB al navegador antes de
# dibujar el primer polígono.
#
# Se simplifican con mapshaper, que es TOPOLÓGICO: simplifica los bordes COMPARTIDOS
# una sola vez, así que dos segmentos vecinos siguen pegados. Simplificar cada
# polígono por separado —lo que haría shapely— abriría grietas blancas entre vecinos
# al acercar el zoom.
#
# El 20 % es una decisión medida, no un número redondo (14-ago-2026):
#
#   nivel   gzip     cambio de área: mediana / p99 / máx   polígonos con >10 % de cambio
#   ------  -------  -----------------------------------   -----------------------------
#   10 %    1,3 MB   0,416 % / 9,13 % / 54,54 %            37
#   20 %    2,4 MB   0,130 % / 2,25 % / 19,44 %             1
#
# El 10 % pesa la mitad pero deforma 37 polígonos chicos más de un 10 %. El 20 % deja
# uno solo y sigue siendo 17 veces más liviano que el original. El área TOTAL del país
# cambia 0,0002 %.
#
# 'keep-shapes' impide que un segmento chico desaparezca —perder una unidad en
# silencio es exactamente lo que el mapa no puede hacer— y 'precision=0.00001' recorta
# los decimales sobrantes de las coordenadas (1e-5 grados ≈ 1 m).
#
# Los originales quedan en el historial de git: esto reescribe los archivos servidos.
#
# Uso:  ./simplificar_segmentos_2023.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/app/static/geo_2023"
command -v mapshaper >/dev/null || { echo "falta mapshaper (npm i -g mapshaper)"; exit 1; }

for f in "$DIR"/geo_2023_segmentos_*.json; do
  mapshaper "$f" -simplify visvalingam 20% keep-shapes \
            -o precision=0.00001 force "$f" >/dev/null
  printf '  %s  %s KB\n' "$(basename "$f")" "$(( $(stat -c%s "$f") / 1024 ))"
done

echo "total: $(du -ch "$DIR"/geo_2023_segmentos_*.json | tail -1 | cut -f1)"
