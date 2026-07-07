"""
sql_guard.py — Validación de SQL generado por el LLM sobre MICRODATOS del censo,
con un PARSER REAL (sqlglot). Reemplaza el guard v3 basado en regex.

Principio: si una consulta no puede verificarse como segura, NO se ejecuta.

La tabla `personas` tiene una fila por persona, así que además de las defensas
habituales rige el control de divulgación estadística:

  A. Solo agregados: cada columna de la proyección externa es una expresión
     agregada o una columna del GROUP BY. Nunca filas individuales.
  B. Supresión de celdas chicas: las filas cuyo conteo < UMBRAL_SUPRESION se
     descartan tras ejecutar. La supresión es ESTRUCTURAL: el guard identifica
     en el árbol qué columnas de salida SON conteos y se las pasa al supresor;
     si no puede identificarlas, RECHAZA (fail-closed).

Reglas (todas sobre el árbol parseado, no sobre texto):
  1. Parsea, sentencia única, solo SELECT (UNION/DML/PRAGMA/… se rechazan).
  2. SELECT * prohibido (COUNT(*) sí, es un agregado).
  3. Tablas ⊆ {personas, localidades}; JOIN solo personas↔localidades por codloc.
  4. Columnas ⊆ diccionario.json + derivadas + keys + columnas de localidades
     (+ alias de salida de la propia consulta).
  5. Debe existir al menos un COUNT (toda consulta cuenta personas/hogares/viv).
  6. hogar_key / vivienda_key: libres en subconsultas, WHERE, JOIN y GROUP BY
     internos; en la proyección externa SOLO dentro de COUNT(DISTINCT ...).
     PERID sin restricciones especiales.
  7. LIMIT obligatorio, tope LIMITE_MAXIMO (se agrega si falta).
"""

import sqlglot
from sqlglot import exp

from app import dicc

# Celdas con menos personas que esto se suprimen (control de divulgación).
UMBRAL_SUPRESION = 5

# Techo de filas. 300 cubre el nivel geográfico más grande que se mapea.
LIMITE_MAXIMO = 300

TABLAS_PERMITIDAS = {"personas", "localidades", "paises"}

# Columnas del nomenclátor de países (nacidos en el exterior: PERMI01_4/06_4/07_4).
COLUMNAS_PAISES = {"codigo", "nombre", "nombre_oficial", "alfa3"}

# Columnas de personas que referencian un código de país (JOIN válido a paises.codigo).
_COLS_PAIS = {"permi01_4", "permi06_4", "permi07_4"}

# Whitelist de columnas (todo en minúsculas; SQLite es case-insensitive).
_COLUMNAS_VALIDAS = (dicc.columnas_personas()
                     | {c.lower() for c in dicc.COLUMNAS_LOCALIDADES}
                     | COLUMNAS_PAISES)

# Identificadores de hogar/vivienda: reidentificantes.
KEYS_RESTRINGIDAS = {"hogar_key", "vivienda_key"}

# Funciones peligrosas de SQLite (I/O, extensiones) — defensa en profundidad.
_FUNCS_PROHIBIDAS = {
    "load_extension", "readfile", "writefile", "edit", "fsdir", "zipfile",
}


class SQLNoSeguro(Exception):
    """Se lanza cuando una consulta no pasa validación. NO debe ejecutarse."""


def _parsear(sql: str) -> exp.Expression:
    try:
        arboles = [a for a in sqlglot.parse(sql, read="sqlite") if a is not None]
    except Exception as e:  # sqlglot.errors.ParseError y afines
        raise SQLNoSeguro(f"No se pudo parsear la consulta: {e}")
    if len(arboles) != 1:
        raise SQLNoSeguro("Debe ser una sola sentencia SQL.")
    return arboles[0]


def _cols_de_scope_externo(nodo: exp.Expression, externo: exp.Select):
    """Columnas de `nodo` que pertenecen al SELECT externo (no a una
    subconsulta anidada) y que NO están dentro de una función agregada."""
    for c in nodo.find_all(exp.Column):
        if c.find_ancestor(exp.AggFunc) is not None:
            continue
        if c.find_ancestor(exp.Select) is not externo:
            continue  # pertenece a una subconsulta: otro scope
        yield c


def _validar_join(arbol: exp.Expression) -> None:
    """JOIN permitido solo con el nomenclátor: personas↔localidades por codloc, o
    personas↔paises por un código de país (PERMI01_4/06_4/07_4 = paises.codigo).
    Se exige que la condición use exclusivamente esas columnas (no reidentifica)."""
    for j in arbol.find_all(exp.Join):
        using = j.args.get("using")
        on = j.args.get("on")
        if using:
            cols = {u.name.lower() for u in using}
        elif on is not None:
            cols = {c.name.lower() for c in on.find_all(exp.Column)}
        else:
            raise SQLNoSeguro("JOIN sin condición no permitido.")
        es_localidad = cols == {"codloc"}
        es_pais = "codigo" in cols and cols <= ({"codigo"} | _COLS_PAIS)
        if not (es_localidad or es_pais):
            raise SQLNoSeguro(
                "JOIN solo permitido con el nomenclátor: localidades por codloc "
                "o paises por PERMI01_4/PERMI06_4/PERMI07_4 = paises.codigo."
            )


def _aplicar_limite(arbol: exp.Select) -> exp.Select:
    lim = arbol.args.get("limit")
    if lim is None:
        return arbol.limit(LIMITE_MAXIMO)
    try:
        n = int(lim.expression.name)
    except (AttributeError, ValueError):
        raise SQLNoSeguro("LIMIT no numérico.")
    if n > LIMITE_MAXIMO:
        raise SQLNoSeguro(f"LIMIT excede el máximo de {LIMITE_MAXIMO}.")
    return arbol


_contador_alias = 0


def _identificar_conteos(arbol: exp.Select) -> list[str]:
    """Devuelve los nombres de salida de las columnas de la proyección externa
    que SON un COUNT/COUNT(DISTINCT). A las que no tienen alias les inyecta uno
    determinista para poder localizar su valor en las filas resultantes."""
    global _contador_alias
    nombres = []
    for i, proj in enumerate(list(arbol.expressions)):
        interno = proj.this if isinstance(proj, exp.Alias) else proj
        if isinstance(interno, exp.Count):
            if isinstance(proj, exp.Alias):
                nombres.append(proj.alias)
            else:
                alias = f"conteo_{i}"
                proj.replace(exp.alias_(proj.copy(), alias))
                nombres.append(alias)
    return nombres


def validar(sql: str) -> tuple[str, list[str]]:
    """Valida un SQL del LLM. Devuelve (sql_seguro, columnas_de_conteo).
    Lanza SQLNoSeguro si algo no puede verificarse."""
    arbol = _parsear(sql)

    # 1. Solo SELECT (Union, Insert, Delete, Drop, Pragma, ... no son exp.Select).
    if not isinstance(arbol, exp.Select):
        raise SQLNoSeguro("Solo se permiten consultas SELECT.")

    # 2. SELECT * prohibido (permitido solo dentro de un agregado: COUNT(*)).
    for star in arbol.find_all(exp.Star):
        if star.find_ancestor(exp.AggFunc) is None:
            raise SQLNoSeguro(
                "SELECT * prohibido: los microdatos solo se consultan agregados."
            )

    # 3. Tablas whitelisted + JOIN solo personas↔localidades por codloc.
    tablas = list(arbol.find_all(exp.Table))
    if not tablas:
        raise SQLNoSeguro("No se detectó tabla de origen.")
    for t in tablas:
        if t.name.lower() not in TABLAS_PERMITIDAS:
            raise SQLNoSeguro(f"Tabla no permitida: {t.name}")
    _validar_join(arbol)

    # 4. Columnas whitelisted (+ alias de salida definidos en la consulta).
    alias_salida = {a.alias.lower() for a in arbol.find_all(exp.Alias) if a.alias}
    permitidas = _COLUMNAS_VALIDAS | alias_salida
    for c in arbol.find_all(exp.Column):
        if c.name.lower() not in permitidas:
            raise SQLNoSeguro(f"Columna no permitida: {c.name}")

    # 4b. Funciones peligrosas (I/O, extensiones).
    for f in arbol.find_all(exp.Anonymous):
        if f.name.lower() in _FUNCS_PROHIBIDAS:
            raise SQLNoSeguro(f"Función no permitida: {f.name}")

    # 5. Debe contar: al menos un COUNT en algún lugar del árbol.
    if not list(arbol.find_all(exp.Count)):
        raise SQLNoSeguro(
            "Consulta no agregada: toda consulta debe contar (COUNT), "
            "nunca devolver registros individuales."
        )

    # 6. Solo agregados: cada columna libre de la proyección externa debe estar
    #    en el GROUP BY. (Núcleo del control: nunca filas individuales.)
    grupo = arbol.args.get("group")
    group_exprs = grupo.expressions if grupo else []
    group_nombres = {g.name.lower() for g in group_exprs if isinstance(g, exp.Column)}
    group_sql = {g.sql(dialect="sqlite").lower() for g in group_exprs}
    for proj in arbol.expressions:
        for c in _cols_de_scope_externo(proj, arbol):
            if c.name.lower() in group_nombres:
                continue
            if c.sql(dialect="sqlite").lower() in group_sql:
                continue
            raise SQLNoSeguro(
                f"Proyección no agregada: la columna '{c.name}' no está agregada "
                "ni en el GROUP BY (devolvería filas individuales)."
            )

    # 7. hogar_key / vivienda_key en la proyección externa: solo COUNT(DISTINCT).
    for proj in arbol.expressions:
        for c in proj.find_all(exp.Column):
            if c.name.lower() not in KEYS_RESTRINGIDAS:
                continue
            if c.find_ancestor(exp.Select) is not arbol:
                continue  # dentro de una subconsulta: libre
            # Solo válido dentro de COUNT(DISTINCT ...): en sqlglot el DISTINCT
            # es un nodo Distinct hijo del Count (Count.this = Distinct(col)).
            cnt = c.find_ancestor(exp.Count)
            dist = c.find_ancestor(exp.Distinct)
            if cnt is None or dist is None or dist.find_ancestor(exp.Count) is not cnt:
                raise SQLNoSeguro(
                    f"{c.name} en la proyección externa solo puede aparecer "
                    "dentro de COUNT(DISTINCT ...)."
                )

    # 8. Supresión estructural: identificar columnas de conteo (fail-closed).
    try:
        columnas_conteo = _identificar_conteos(arbol)
    except Exception as e:
        raise SQLNoSeguro(f"No se pudieron identificar los conteos: {e}")

    # 9. LIMIT obligatorio y acotado.
    arbol = _aplicar_limite(arbol)

    return arbol.sql(dialect="sqlite", comments=False), columnas_conteo


def suprimir_celdas_chicas(
    filas: list[dict], columnas_conteo: list[str]
) -> tuple[list[dict], int]:
    """Control de divulgación: descarta cada fila cuyo conteo esté por debajo de
    UMBRAL_SUPRESION. Las columnas de conteo las identifica el guard sobre el
    árbol (por nombre de salida), no por heurística de nombres."""
    claves = {c.lower() for c in columnas_conteo}
    seguras, suprimidas = [], 0
    for fila in filas:
        conteos = [
            v for k, v in fila.items()
            if k.lower() in claves and isinstance(v, int) and not isinstance(v, bool)
        ]
        if conteos and min(conteos) < UMBRAL_SUPRESION:
            suprimidas += 1
        else:
            seguras.append(fila)
    return seguras, suprimidas
