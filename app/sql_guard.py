"""
sql_guard.py — Validation layer for LLM-generated SQL over census MICRODATA.

Principle: if a query cannot be verified as safe, it is NOT executed.

Because the underlying table contains one row per person, two extra rules
apply on top of the usual injection defenses (this is standard statistical
disclosure control, the same logic REDATAM applies):

  A. Aggregate-only: queries must return counts, never individual rows.
  B. Small-cell suppression: result cells with fewer than UMBRAL_SUPRESION
     persons are suppressed after execution (see main.py).

Rules enforced here:
1. Single statement, SELECT-only.
2. Only whitelisted tables and columns.
3. No comments, no semicolon chaining, no PRAGMA/ATTACH/UNION/etc.
4. Mandatory aggregation: COUNT(*) required, SELECT * forbidden.
5. Mandatory LIMIT (added if missing).
"""

import re

# Whitelist: the ONLY table/columns the LLM is allowed to touch.
# hogar_key NO va acá: solo se permite dentro de COUNT(DISTINCT hogar_key)
# (ver regla dedicada en validar()), nunca como columna libre.
# Las unidades geográficas (departamento, codsec, BARRIO85, CCZ, nombre de
# localidad) SÍ pueden aparecer en la salida: son públicas, no datos personales.
TABLAS_PERMITIDAS = {
    "personas": {"departamento", "sexo", "edad",
                 "asc_afro", "asc_principal", "nbi",
                 "secc", "loc", "barrio85", "ccz", "codsec", "codloc"},
    "localidades": {"codloc", "nombre", "departamento"},
}

# Results with fewer persons than this are suppressed (disclosure control).
UMBRAL_SUPRESION = 5

PALABRAS_PROHIBIDAS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|"
    r"vacuum|replace|grant|revoke|union|exec)\b",
    re.IGNORECASE,
)

# Techo de filas. 300 cubre el nivel geográfico más grande que se mapea
# (232 secciones censales); por debajo, un mapa por sección se truncaba.
LIMITE_MAXIMO = 300

# Literal de texto SQL entre comillas simples (con '' escapado interno).
_LITERAL = re.compile(r"'(?:[^']|'')*'")


def _sin_literales(sql: str) -> str:
    """Reemplaza cada literal de texto '...' por '' para que las validaciones
    estructurales (keywords, tablas) no matcheen palabras dentro de nombres
    geográficos: la localidad 'BELLA UNION' y el barrio 'Union' contienen la
    keyword UNION, pero como dato, no como operador."""
    return _LITERAL.sub("''", sql)


class SQLNoSeguro(Exception):
    """Raised when a query fails validation. The caller must NOT execute it."""


def validar(sql: str) -> str:
    """Validate an LLM-generated SQL string. Returns a safe version or raises."""
    limpio = sql.strip().rstrip(";").strip()

    # 1. Single statement, no comments
    if ";" in limpio:
        raise SQLNoSeguro("Múltiples sentencias no permitidas.")
    if "--" in limpio or "/*" in limpio:
        raise SQLNoSeguro("Comentarios SQL no permitidos.")

    # 2. SELECT-only
    if not re.match(r"^\s*select\b", limpio, re.IGNORECASE):
        raise SQLNoSeguro("Solo se permiten consultas SELECT.")

    # Las validaciones estructurales corren sobre el SQL SIN literales de texto,
    # para no confundir un nombre geográfico (ej. 'BELLA UNION') con un operador.
    analizable = _sin_literales(limpio)

    # 3. Forbidden keywords
    if PALABRAS_PROHIBIDAS.search(analizable):
        raise SQLNoSeguro("Palabra clave no permitida detectada.")

    # 4. Aggregate-only over microdata: never individual rows
    if re.search(r"select\s+\*", analizable, re.IGNORECASE):
        raise SQLNoSeguro("SELECT * prohibido: los microdatos solo se consultan agregados.")
    if not re.search(r"\bcount\s*\(", analizable, re.IGNORECASE):
        raise SQLNoSeguro(
            "Consulta no agregada: toda consulta debe contar personas (COUNT), "
            "nunca devolver registros individuales."
        )

    # 5. Table whitelist: every FROM/JOIN target must be whitelisted
    tablas_usadas = re.findall(
        r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", analizable, re.IGNORECASE
    )
    if not tablas_usadas:
        raise SQLNoSeguro("No se detectó tabla de origen.")
    for t in tablas_usadas:
        if t.lower() not in TABLAS_PERMITIDAS:
            raise SQLNoSeguro(f"Tabla no permitida: {t}")

    # 5b. hogar_key es un identificador de hogar (potencialmente reidentificante):
    # solo se admite dentro de COUNT(DISTINCT hogar_key), nunca suelto en SELECT,
    # GROUP BY, WHERE u ORDER BY.
    apariciones = len(re.findall(r"\bhogar_key\b", analizable, re.IGNORECASE))
    if apariciones:
        permitidas = len(re.findall(
            r"count\s*\(\s*distinct\s+hogar_key\s*\)", analizable, re.IGNORECASE
        ))
        if apariciones != permitidas:
            raise SQLNoSeguro(
                "hogar_key solo puede usarse dentro de COUNT(DISTINCT hogar_key)."
            )

    # 6. Mandatory LIMIT
    if not re.search(r"\blimit\s+\d+\b", limpio, re.IGNORECASE):
        limpio = f"{limpio} LIMIT {LIMITE_MAXIMO}"
    else:
        n = int(re.search(r"\blimit\s+(\d+)\b", limpio, re.IGNORECASE).group(1))
        if n > LIMITE_MAXIMO:
            raise SQLNoSeguro(f"LIMIT excede el máximo de {LIMITE_MAXIMO}.")

    return limpio


def suprimir_celdas_chicas(filas: list[dict]) -> tuple[list[dict], int]:
    """
    Statistical disclosure control: drop any result row whose person count
    is below UMBRAL_SUPRESION. Returns (safe_rows, n_suppressed).
    Count columns are detected as any integer-valued column named like a count.
    """
    seguras, suprimidas = [], 0
    for fila in filas:
        conteos = [
            v for k, v in fila.items()
            if isinstance(v, int) and ("count" in k.lower() or k.lower() in ("personas", "hogares", "n", "total"))
        ]
        if conteos and min(conteos) < UMBRAL_SUPRESION:
            suprimidas += 1
        else:
            seguras.append(fila)
    return seguras, suprimidas
