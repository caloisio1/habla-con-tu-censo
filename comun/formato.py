"""formato.py — formateo DETERMINÍSTICO de cifras en la narración.

Mejora de diseño (beneficia a cualquier modelo): el redactor NO es responsable del
formato de los números. Después de narrar, el código reemplaza los valores de las
columnas de MÉTRICA por su forma con separador de miles (estilo UY: punto), de manera
data-driven y a prueba de colisiones:

- Sólo toca valores enteros >= 1000 de columnas de métrica (personas, hogares,
  viviendas, n_validos, …). Excluye columnas de CÓDIGO/identificador.
- Excluye cualquier valor que además aparezca como CÓDIGO en el mismo resultado
  (p. ej. un conteo que coincida numéricamente con un código de sección): ante la duda,
  no se toca. Así nunca corrompe años ('2011'/'2023'), ni códigos de sección/segmento/geo.
- Reemplaza sólo ocurrencias del entero "suelto" (fronteras de dígito/decimal estrictas),
  de más largo a más corto, para no partir números mayores ni decimales.
"""
import re

# Pistas de que una columna es un CÓDIGO/identificador (no una cantidad a formatear).
# Precisas: nada de substrings sueltos como "id" (matchearía "n_valIDos", "considerado"...).
_COD_HINTS = ("cod", "geo_", "clave", "_id", "perid", "hogid", "vivid", "direccion",
              "ccz", "secc", "seccion", "segmento", "localidad", "departamento",
              "barrio85", "region", "universo", "_key", "codloc", "codsec")
# Columnas de conteo crudo que nunca se narran.
_EXCLUIR = ("n_crudo", "n", "conteo_1", "conteo_0")


def _es_columna_codigo(col: str) -> bool:
    c = col.lower()
    if c in ("id", "geo_codigo", "codigo", "area"):
        return True
    return any(h in c for h in _COD_HINTS)


def _entero(v):
    """Devuelve el int si v es un entero (int, o float con parte decimal nula); si no, None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def fmt_miles(n: int) -> str:
    """12985 -> '12.985' (separador de miles con punto, estilo Uruguay)."""
    return f"{n:,}".replace(",", ".")


def aplicar_formato_miles(texto: str, datos) -> str:
    if not texto or not datos:
        return texto
    metricas, codigos = set(), set()
    for row in datos:
        if not isinstance(row, dict):
            continue
        for col, v in row.items():
            n = _entero(v)
            if n is None:
                continue
            if _es_columna_codigo(col):
                codigos.add(n)
            elif col.lower() not in _EXCLUIR and abs(n) >= 1000:
                metricas.add(n)
    # Ante colisión con un código, no se toca el valor (defensa: nunca corromper códigos).
    seguros = sorted(metricas - codigos, key=lambda x: -len(str(abs(x))))
    for v in seguros:
        s = re.escape(str(v))
        texto = re.sub(rf"(?<![\d.]){s}(?![\d.])", fmt_miles(v), texto)
    return texto
