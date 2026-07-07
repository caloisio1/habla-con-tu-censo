"""sql_guard_2023.py — Guard sqlglot para el motor Censo 2023 (versión ponderada).

Misma interfaz que el guard 2011 (validar / suprimir_celdas_chicas / SQLNoSeguro /
UMBRAL_SUPRESION), NO modifica el guard 2011. Reglas propias 2023:

  a) Prohibido JOIN/mezcla personas_2023 ↔ viviendas_2023 (cualquier scope). JOIN solo
     con nomenclátor (departamentos_2023, localidades_2023, barrios_mvd_2023).
  b) En consultas sobre personas_2023 la métrica publicada de personas debe ser SUM(W)
     (exigir ≥1 SUM que involucre W en la proyección externa) y debe existir el conteo
     CRUDO por celda (≥1 COUNT) para evaluar supresión.
  c) Supresión estructural: celda con n crudo < UMBRAL_SUPRESION se descarta (salida y
     mapas). El n crudo nunca se expone si < umbral (la fila entera se suprime).
  d) Identificadores (vivienda_key, hogar_key, DIRECCION_ID, VIVID, HOGID, PERID,
     ID_HOGAR) libres en subconsultas/GROUP BY interno; en la proyección externa SOLO
     dentro de COUNT(DISTINCT ...). (Regla 2011 replicada.)
"""
import json, os
import sqlglot
from sqlglot import exp

UMBRAL_SUPRESION = 5
LIMITE_MAXIMO = 300

_COLS = json.load(open(os.path.join(os.path.dirname(__file__), "cols_2023.json")))
FACT_TABLES = {"personas_2023", "viviendas_2023"}
# paises: nomenclátor de países para nacidos en el exterior (PERMI01_4/06_4/07_4).
NOMENCLATOR = {"departamentos_2023", "localidades_2023", "barrios_mvd_2023", "paises"}
TABLAS_PERMITIDAS = FACT_TABLES | NOMENCLATOR
_COLUMNAS_VALIDAS = ({c.lower() for cols in _COLS.values() for c in cols}
                     | {"codigo", "nombre", "nombre_oficial", "alfa3"})

KEYS_RESTRINGIDAS = {"vivienda_key", "hogar_key", "direccion_id", "vivid",
                     "hogid", "perid", "id_hogar"}
_FUNCS_PROHIBIDAS = {"load_extension", "readfile", "writefile", "edit", "fsdir", "zipfile"}


class SQLNoSeguro(Exception):
    """Se lanza cuando una consulta no pasa validación. NO debe ejecutarse."""


def _parsear(sql):
    try:
        arboles = [a for a in sqlglot.parse(sql, read="sqlite") if a is not None]
    except Exception as e:
        raise SQLNoSeguro(f"No se pudo parsear la consulta: {e}")
    if len(arboles) != 1:
        raise SQLNoSeguro("Debe ser una sola sentencia SQL.")
    return arboles[0]


def _cols_scope_externo(nodo, externo):
    for c in nodo.find_all(exp.Column):
        if c.find_ancestor(exp.AggFunc) is not None:
            continue
        if c.find_ancestor(exp.Select) is not externo:
            continue
        yield c


def _tablas(arbol):
    return {t.name.lower() for t in arbol.find_all(exp.Table)}


def _suma_sobre_w(arbol):
    """True si la proyección externa tiene al menos un SUM que involucra la columna W."""
    for proj in arbol.expressions:
        for s in proj.find_all(exp.Sum):
            if any(col.name.lower() == "w" for col in s.find_all(exp.Column)):
                return True
    return False


def _count_distinct_hogar(arbol):
    """True si la proyección externa cuenta hogares: COUNT(DISTINCT hogar_key)."""
    for proj in arbol.expressions:
        for cnt in proj.find_all(exp.Count):
            if cnt.find(exp.Distinct) and any(
                col.name.lower() == "hogar_key" for col in cnt.find_all(exp.Column)):
                return True
    return False


def _alias_de_tablas(arbol):
    """Mapa alias/nombre-en-la-consulta -> nombre real de tabla (minúsculas)."""
    m = {}
    for t in arbol.find_all(exp.Table):
        real = t.name.lower()
        m[real] = real
        if t.alias:
            m[t.alias.lower()] = real
    return m


def _aplicar_limite(arbol):
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


def _identificar_conteos(arbol):
    """Nombres de salida de las columnas de la proyección externa que SON COUNT/COUNT(DISTINCT).
    A las sin alias les inyecta uno determinista para localizar su valor en las filas."""
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


def validar(sql):
    """Valida un SQL del LLM para el motor 2023. Devuelve (sql_seguro, columnas_de_conteo).
    Lanza SQLNoSeguro si algo no puede verificarse (fail-closed)."""
    arbol = _parsear(sql)

    if not isinstance(arbol, exp.Select):
        raise SQLNoSeguro("Solo se permiten consultas SELECT.")

    for star in arbol.find_all(exp.Star):
        if star.find_ancestor(exp.AggFunc) is None:
            raise SQLNoSeguro("SELECT * prohibido: los microdatos solo se consultan agregados.")

    tablas = _tablas(arbol)
    if not tablas:
        raise SQLNoSeguro("No se detectó tabla de origen.")
    for t in tablas:
        if t not in TABLAS_PERMITIDAS:
            raise SQLNoSeguro(f"Tabla no permitida: {t}")

    # (a) prohibido mezclar las dos tablas de hechos (JOIN o subconsulta, cualquier scope)
    if FACT_TABLES <= tablas:
        raise SQLNoSeguro(
            "Prohibido vincular personas_2023 con viviendas_2023 (universos distintos; §3.2 INE)."
        )

    # columnas whitelisted (+ alias de salida propios)
    alias_salida = {a.alias.lower() for a in arbol.find_all(exp.Alias) if a.alias}
    permitidas = _COLUMNAS_VALIDAS | alias_salida
    for c in arbol.find_all(exp.Column):
        if c.name.lower() not in permitidas:
            raise SQLNoSeguro(f"Columna no permitida: {c.name}")

    for f in arbol.find_all(exp.Anonymous):
        if f.name.lower() in _FUNCS_PROHIBIDAS:
            raise SQLNoSeguro(f"Función no permitida: {f.name}")

    # (b/raw-n) debe existir al menos un COUNT (conteo crudo para supresión)
    if not list(arbol.find_all(exp.Count)):
        raise SQLNoSeguro("Consulta sin COUNT: falta el conteo crudo por celda para la supresión.")

    # (b) personas -> métrica publicada por SUM(W) o hogares por COUNT(DISTINCT hogar_key);
    #     nunca un COUNT(*) crudo como cifra de personas.
    if "personas_2023" in tablas and not (_suma_sobre_w(arbol) or _count_distinct_hogar(arbol)):
        raise SQLNoSeguro(
            "Métrica de personas inválida: usá SUM(W) (personas) o COUNT(DISTINCT hogar_key) "
            "(hogares); COUNT(*) es solo el conteo crudo para la supresión, no la cifra publicada."
        )

    # solo agregados en la proyección externa (nunca filas individuales de las tablas de hechos).
    # Se permiten columnas SIN agregar si pertenecen al NOMENCLÁTOR (lookup, no microdato).
    alias_tab = _alias_de_tablas(arbol)
    grupo = arbol.args.get("group")
    group_exprs = grupo.expressions if grupo else []
    group_nombres = {g.name.lower() for g in group_exprs if isinstance(g, exp.Column)}
    group_sql = {g.sql(dialect="sqlite").lower() for g in group_exprs}
    for proj in arbol.expressions:
        for c in _cols_scope_externo(proj, arbol):
            if c.name.lower() in group_nombres:
                continue
            if c.sql(dialect="sqlite").lower() in group_sql:
                continue
            if c.table and alias_tab.get(c.table.lower()) in NOMENCLATOR:
                continue  # nombre/código de nomenclátor: no es microdato
            raise SQLNoSeguro(
                f"Proyección no agregada: la columna '{c.name}' no está agregada ni en el GROUP BY."
            )

    # (d) identificadores en proyección externa: solo dentro de COUNT(DISTINCT ...)
    for proj in arbol.expressions:
        for c in proj.find_all(exp.Column):
            if c.name.lower() not in KEYS_RESTRINGIDAS:
                continue
            if c.find_ancestor(exp.Select) is not arbol:
                continue
            cnt = c.find_ancestor(exp.Count)
            dist = c.find_ancestor(exp.Distinct)
            if cnt is None or dist is None or dist.find_ancestor(exp.Count) is not cnt:
                raise SQLNoSeguro(
                    f"{c.name} en la proyección externa solo puede aparecer dentro de COUNT(DISTINCT ...)."
                )

    # (c) supresión estructural: identificar columnas de conteo (fail-closed)
    try:
        columnas_conteo = _identificar_conteos(arbol)
    except Exception as e:
        raise SQLNoSeguro(f"No se pudieron identificar los conteos: {e}")

    arbol = _aplicar_limite(arbol)
    return arbol.sql(dialect="sqlite", comments=False), columnas_conteo


def suprimir_celdas_chicas(filas, columnas_conteo):
    """Descarta cada fila cuyo conteo crudo < UMBRAL_SUPRESION (control de divulgación).
    El n crudo < umbral nunca llega al usuario: se suprime la fila entera."""
    claves = {c.lower() for c in columnas_conteo}
    seguras, suprimidas = [], 0
    for fila in filas:
        conteos = [v for k, v in fila.items()
                   if k.lower() in claves and isinstance(v, int) and not isinstance(v, bool)]
        if conteos and min(conteos) < UMBRAL_SUPRESION:
            suprimidas += 1
        else:
            seguras.append(fila)
    return seguras, suprimidas
