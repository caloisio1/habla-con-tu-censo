"""comun/nomenclator.py — Catálogos de entidades de cada censo.

Es la ÚNICA parte del resolver que depende del censo: la lógica de resolución
(comun/resolver.py) es común a los cuatro, lo que cambia es esta lista de
entidades. Cada censo tiene su propio universo de localidades, sus propios
códigos y sus propios nombres, y esa diferencia es real, no un accidente.

Los catálogos se leen de las bases en SOLO LECTURA y se cachean en memoria: son
unos pocos miles de filas por censo, se arman una vez por proceso.

NOTA SOBRE 1996 — el nomenclátor de localidades sale de `cod_localidades_1963_2023`,
que es una tabla de CORRESPONDENCIAS (una fila por cada par localidad histórica ×
localidad 2023) y por lo tanto tiene la clave (dpto, cod_1996) REPETIDA. Unirla
tal cual multiplica los registros de personas: el total del país pasa de 3.163.763
a 16.321.955 (+415,9 %). Por eso acá y en el SQL se usa siempre la forma
deduplicada, agrupada por el departamento del código 2023 (que es el correcto: la
columna `dpto` tiene al menos una fila mal asignada, La Paloma de Rocha marcada
como Salto).
"""
import os
import threading
from collections import namedtuple

from comun import ejecutor

AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASES = {
    "1996": os.environ.get("CENSO1996_DB", os.path.join(AQUI, "datos", "censo1996.db")),
    "2004": os.environ.get("CENSO2004_DB", os.path.join(AQUI, "datos", "censo2004.db")),
    "2011": os.environ.get("CENSO_DB", os.path.join(AQUI, "datos", "censo.db")),
    "2023": os.environ.get("CENSO2023_DB", os.path.join(AQUI, "datos", "censo2023.db")),
}

CENSOS = ("1996", "2004", "2011", "2023")

# Tipos de entidad que entiende el resolver.
LOCALIDAD = "localidad"
DEPARTAMENTO = "departamento"
BARRIO = "barrio"
CCZ = "ccz"
ETIQUETA = "etiqueta"
TIPOS_GEO = (LOCALIDAD, DEPARTAMENTO, BARRIO, CCZ)

# codigo: el valor que va al SQL. nombre: el literal tal cual está en la base.
# variable: solo para las etiquetas de valor (a qué variable pertenece).
Entidad = namedtuple("Entidad", "censo tipo codigo nombre departamento variable")

# ── SQL canónico del nomenclátor de localidades de 1996 ───────────────────
# Una sola definición, usada por el prompt, por el guard y por este módulo: si
# cambia, cambia en un solo lugar. GROUP BY sobre el departamento del código 2023
# garantiza una fila por (departamento, código) y elimina el fan-out.
SUBCONSULTA_LOCALIDADES_1996 = (
    "(SELECT substr(cod_2023,1,2) AS dpto, cod_1996 AS cod, MIN(nom_1996) AS nombre "
    "FROM cod_localidades_1963_2023 WHERE cod_1996<>'' AND cod_2023<>'' GROUP BY 1,2)"
)

# Tabla cuya unión directa produce el fan-out. El guard la vigila.
TABLA_CRUZADA = "cod_localidades_1963_2023"

_CACHE = {}
_LOCK = threading.Lock()


def _consultar(censo, sql, params=()):
    """Catálogos del censo, por el MISMO camino que las consultas de usuario.

    Antes esto abría el `.db` con sqlite3 directo, sin pasar por el ejecutor. Eso
    hacía que el nomenclátor -la capa que convierte "Salto" en un código antes de
    que el modelo escriba el SQL, y que es por lo tanto el camino central- fuera
    el único punto del sistema que seguía atado a SQLite aunque DuckDB estuviera
    perfecto. Pasando por `ejecutor.filas` la base la elige el ejecutor: si está
    el `.duckdb` hermano, se sirve de ahí.

    Se pide por `tuplas()` y no por `filas()` porque los llamadores de este
    módulo desempaquetan por posición (`for c, n, d in ...`) y hay consultas con
    columnas HOMÓNIMAS -`SELECT COUNT(*), COUNT(*)` para el par (ponderado,
    crudo)- que en un dict colapsan en una sola clave."""
    return ejecutor.tuplas(BASES[censo], sql, params)


def _localidades(censo):
    if censo == "2023":
        return [Entidad(censo, LOCALIDAD, c, n, d, None) for c, n, d in _consultar(
            censo, "SELECT codloc, nombre, nomdepto FROM localidades_2023")]
    if censo == "2011":
        return [Entidad(censo, LOCALIDAD, str(c), n, d, None) for c, n, d in _consultar(
            censo, "SELECT codloc, nombre, departamento FROM localidades")]
    if censo == "2004":
        return [Entidad(censo, LOCALIDAD, str(c), n, d, None) for c, n, d in _consultar(
            censo, "SELECT cod_ine, nom_loc, nom_dpto FROM ref_localidades_2004")]
    # 1996: nomenclátor deduplicado (ver la nota del encabezado)
    deptos = {c: n for c, n in _consultar(censo, "SELECT dpto, nombre FROM cod_departamentos")}
    filas = _consultar(censo, "SELECT dpto, cod, nombre FROM " + SUBCONSULTA_LOCALIDADES_1996)
    return [Entidad(censo, LOCALIDAD, d + c, n, deptos.get(d, d), None) for d, c, n in filas]


def _departamentos(censo):
    if censo == "2023":
        filas = _consultar(censo, "SELECT codigo, nombre FROM departamentos_2023")
    elif censo == "2011":
        filas = [(n, n) for (n,) in _consultar(
            censo, "SELECT DISTINCT departamento FROM personas WHERE departamento IS NOT NULL")]
    else:
        filas = _consultar(censo, "SELECT dpto, nombre FROM cod_departamentos")
    return [Entidad(censo, DEPARTAMENTO, str(c), n, n, None) for c, n in filas]


def _barrios(censo):
    if censo == "2023":
        filas = _consultar(censo, "SELECT codbarrio, nombre FROM barrios_mvd_2023")
    elif censo == "2011":
        # En 2011 los barrios están en la propia tabla de personas, y —a diferencia
        # de los otros tres censos— con tildes y en formato mixto ('Bañados de
        # Carrasco'). El literal del SQL tiene que respetar esa forma exacta.
        filas = [(n, n) for (n,) in _consultar(
            censo, "SELECT DISTINCT BARRIO85 FROM personas WHERE BARRIO85 IS NOT NULL")]
    else:
        filas = _consultar(censo, "SELECT barrio, nombre FROM cod_barrios_mvd")
    return [Entidad(censo, BARRIO, str(c), n, "MONTEVIDEO", None) for c, n in filas]


def _ccz(censo):
    if censo != "2011":
        return []
    return [Entidad(censo, CCZ, str(c), "CCZ %s" % c, "MONTEVIDEO", None)
            for (c,) in _consultar(censo, "SELECT DISTINCT CCZ FROM personas WHERE CCZ IS NOT NULL")]


def _etiquetas(censo):
    """Etiquetas de valor de las variables categóricas, con su variable."""
    salida = []
    if censo in ("1996", "2004"):
        sql = ("SELECT variable, codigo, etiqueta FROM diccionario_valores WHERE es_perdido=0"
               if censo == "1996" else
               "SELECT variable, codigo, etiqueta FROM diccionario_valores")
        for var, cod, et in _consultar(censo, sql):
            salida.append(Entidad(censo, ETIQUETA, str(cod), et, None, var))
    elif censo == "2011":
        from app import dicc
        for v in dicc.variables():
            for cod, et in (v.get("value_labels") or {}).items():
                salida.append(Entidad(censo, ETIQUETA, str(cod), et, None, v["nombre"]))
    else:
        import json
        ruta = os.path.join(AQUI, "diccionario_llm_2023.json")
        with open(ruta, encoding="utf-8") as fh:
            d = json.load(fh)
        for tabla in ("personas_2023", "viviendas_2023"):
            for v in d["tablas"].get(tabla, {}).get("variables", []):
                for cod, et in (v.get("value_labels") or {}).items():
                    salida.append(Entidad(censo, ETIQUETA, str(cod), et, None, v["nombre"]))
    return salida


def catalogo(censo, tipo=None):
    """Entidades de un censo, opcionalmente filtradas por tipo. Se cachea."""
    if censo not in BASES:
        raise ValueError("censo desconocido: %r" % (censo,))
    with _LOCK:
        if censo not in _CACHE:
            _CACHE[censo] = (_localidades(censo) + _departamentos(censo)
                             + _barrios(censo) + _ccz(censo) + _etiquetas(censo))
    if tipo is None:
        return _CACHE[censo]
    tipos = (tipo,) if isinstance(tipo, str) else tuple(tipo)
    return [e for e in _CACHE[censo] if e.tipo in tipos]


def censos_con(nombre_normalizado, tipo=None):
    """En qué censos existe una entidad con ese nombre normalizado.

    Es lo que permite responder "esa localidad no figura en el Censo 2023; sí en
    1996 y 2004" en vez de fallar en silencio.
    """
    from comun.texto import normalizar
    salida = []
    for c in CENSOS:
        if any(normalizar(e.nombre) == nombre_normalizado for e in catalogo(c, tipo)):
            salida.append(c)
    return salida


def limpiar_cache():
    """Solo para los tests: obliga a releer las bases."""
    with _LOCK:
        _CACHE.clear()


# ── población de una entidad (para las opciones de desambiguación) ────────
# Se usa solo cuando hay que ofrecerle al usuario una lectura alternativa: la
# cifra va en el propio chip, así se elige viendo el número y no a ciegas. Se
# cachea porque una colisión suele repetirse (Salto, Montevideo, Canelones).
#
# La supresión se aplica ACÁ también: si la entidad tiene menos de 5 registros
# crudos, el chip se muestra SIN cifra. El resguardo no admite excepciones por
# tratarse de una ayuda de interfaz.
_POBLACION = {}

_SQL_POBLACION = {
    ("2023", LOCALIDAD): ("SELECT ROUND(SUM(W)), COUNT(*) FROM personas_2023 "
                          "WHERE DEPARTAMENTO||LOCALIDAD = ?"),
    ("2023", DEPARTAMENTO): ("SELECT ROUND(SUM(W)), COUNT(*) FROM personas_2023 "
                             "WHERE DEPARTAMENTO = ?"),
    ("2023", BARRIO): ("SELECT ROUND(SUM(p.W)), COUNT(*) FROM personas_2023 p "
                       "JOIN barrios_mvd_2023 b ON p.BARRIO85 = b.nombre "
                       "WHERE b.codbarrio = ?"),
    ("2011", LOCALIDAD): "SELECT COUNT(*), COUNT(*) FROM personas WHERE codloc = ?",
    ("2011", DEPARTAMENTO): "SELECT COUNT(*), COUNT(*) FROM personas WHERE departamento = ?",
    ("2011", BARRIO): "SELECT COUNT(*), COUNT(*) FROM personas WHERE BARRIO85 = ?",
    ("2011", CCZ): "SELECT COUNT(*), COUNT(*) FROM personas WHERE CCZ = ?",
    # En 2004 la tabla de hechos no trae el código INE de 5 dígitos sino dpto+loc
    # por separado, así que el código del nomenclátor se parte en sus dos mitades.
    ("2004", LOCALIDAD): ("SELECT COUNT(*), COUNT(*) FROM censo2004 WHERE per=1 "
                          "AND dpto = substr(printf('%05d', CAST(? AS INTEGER)),1,2) "
                          "AND loc = substr(printf('%05d', CAST(? AS INTEGER)),3)"),
    ("2004", DEPARTAMENTO): "SELECT COUNT(*), COUNT(*) FROM censo2004 WHERE per=1 AND dpto = ?",
    ("2004", BARRIO): ("SELECT COUNT(*), COUNT(*) FROM censo2004 WHERE per=1 "
                       "AND CAST(barrio AS INTEGER) = CAST(? AS INTEGER)"),
    ("1996", LOCALIDAD): ("SELECT COUNT(*), COUNT(*) FROM personas_1996 "
                          "WHERE dpto = substr(?,1,2) AND loc = substr(?,3)"),
    ("1996", DEPARTAMENTO): "SELECT COUNT(*), COUNT(*) FROM personas_1996 WHERE dpto = ?",
    ("1996", BARRIO): ("SELECT COUNT(*), COUNT(*) FROM personas_1996 "
                       "WHERE CAST(barrio AS INTEGER) = CAST(? AS INTEGER)"),
}

UMBRAL_SUPRESION = 5


def poblacion(entidad):
    """Personas de una entidad, o None si no se puede calcular o hay que suprimir."""
    clave = (entidad.censo, entidad.tipo, entidad.codigo)
    if clave in _POBLACION:
        return _POBLACION[clave]
    sql = _SQL_POBLACION.get((entidad.censo, entidad.tipo))
    valor = None
    if sql:
        params = (entidad.codigo, entidad.codigo) if sql.count("?") == 2 else (entidad.codigo,)
        try:
            ponderado, crudo = _consultar(entidad.censo, sql, params)[0]
            if crudo and crudo >= UMBRAL_SUPRESION and ponderado:
                valor = int(ponderado)
        # No se acota a sqlite3.Error: acá abajo ahora puede haber DuckDB, y el
        # contrato de esta función es devolver None cuando no se puede calcular.
        except Exception:
            valor = None
    _POBLACION[clave] = valor
    return valor
