"""comun/ejecutor.py — Ejecución de las consultas analíticas, compartida por los cuatro motores.

POR QUÉ. Las consultas que la app genera de verdad no son filtros puntuales: son
cruces (departamento × sexo × tramo de edad), JOINs contra el nomenclátor y
jerarquías. Sobre SQLite esas consultas cuestan entre 2 y 19 segundos, y en las
más pesadas son casi la mitad del tiempo total de la respuesta. DuckDB, que es
columnar, las resuelve entre 5 y 12 veces más rápido.

NO SE MIGRAN DATOS. DuckDB lee el MISMO archivo .db a través de su lector sqlite,
attachado en solo lectura. Las bases siguen siendo las de siempre, el build no
cambia, y volver atrás es cambiar una variable de entorno.

DÓNDE GANA Y DÓNDE NO. En un filtro muy selectivo (un departamento, una localidad)
SQLite tiene índices y DuckDB hace escaneo completo: ahí pierde. Gana en todo lo
demás, que es lo que el modelo escribe en la práctica —porque joinea y usa
expresiones, y entonces los índices no se aplican igual. Medido sobre las 19
consultas del SQL REAL de producción (logs/experimento_latencia.json), por este
mismo camino de código: 101,1 s -> 10,9 s en total (9,3x), con salida idéntica
fila por fila en las 19. Lo pesado es donde más gana: las jerárquicas 78,2 s ->
6,7 s. Las consultas simples pierden (0,25 s -> 0,44 s): ahí el escaneo columnar
cuesta más que el índice, y no importa porque ya eran instantáneas.

LOS DOS INVARIANTES QUE NO SE PUEDEN PERDER. Los dos cambian la respuesta SIN
error y en silencio, que es la peor forma de romperse:

1. ORDEN DE LOS NULL. SQLite trata el NULL como el valor más chico (ASC ->
   primero); DuckDB por defecto lo manda al final. En un `ORDER BY ... LIMIT 1`
   —"¿qué departamento tiene más X?"— una fila NULL en la punta cambia la
   respuesta.

2. DIVISIÓN ENTERA. En SQLite `17/5` es 3 (los dos operandos son enteros);
   en DuckDB es 3.4. Eso pega de lleno en el patrón más usado de la app, el
   tramo quinquenal de edad `(edad/5)*5`: 15 en SQLite, 17.0 en DuckDB. Un
   GROUP BY sobre esa expresión devolvería un grupo por cada edad en vez de uno
   por tramo. Se descubrió en producción porque un `PRINTF('%d')` sobre el
   float dio error y cayó a SQLite —o sea, se salvó por casualidad—; sin el
   PRINTF habría contestado mal y nadie se enteraba.

Por eso se fijan `default_null_order` e `integer_division` Y ADEMÁS se comprueban
con consultas canario al abrir cada conexión. Si algún canario no da exactamente
la semántica de SQLite, esa base queda servida por SQLite y se sigue andando.

RED DE SEGURIDAD. Cualquier excepción de DuckDB —una función que no soporta, un
tipo declarado que no coincide con lo guardado— se atrapa y la consulta se repite
en SQLite. Un fallo del motor rápido degrada la latencia, nunca la respuesta.
Esa red NO es muda: cada degradación se avisa una vez por base en el journal
(`journalctl -u censo-query-uy | grep ejecutor`) y queda contada en `estado()`.
Sin eso, perder DuckDB en producción sería invisible: las respuestas seguirían
saliendo bien, solo diez veces más lento, y nadie se enteraría.

APAGADO SIN DESPLIEGUE. `CENSO_MOTOR_SQL=sqlite` en el entorno vuelve todo a
SQLite sin tocar código; `=duckdb` (o nada) usa el camino rápido.
"""
import collections
import decimal
import os
import re
import sqlite3
import sys
import threading

MOTOR = os.environ.get("CENSO_MOTOR_SQL", "duckdb").strip().lower()

# Semántica de NULL de SQLite: ASC -> primero, DESC -> último.
_ORDEN_NULOS = "NULLS_FIRST_ON_ASC_LAST_ON_DESC"

# Los canarios no tocan ninguna base, así que valen igual para los cuatro censos.
# Cada uno comprueba UN invariante, con el resultado que da SQLite.
_CANARIOS = (
    # orden de los NULL: el más chico va primero en ASC
    ("orden de NULL",
     "SELECT x FROM (VALUES (1),(NULL),(2)) t(x) ORDER BY x",
     [(None,), (1,), (2,)]),
    # división entera: el tramo quinquenal de 17 años es 15, no 17.0
    ("división entera",
     "SELECT (17/5)*5, 7/2, -7/2",
     [(15, 3, -3)]),
)

# CONSTRUCCIONES QUE LAS DOS BASES NO RESUELVEN IGUAL. No hay setting que las
# alinee, así que se sirven por SQLite y listo: preferimos perder velocidad antes
# que dar otra cifra. No cuesta nada porque en las 76 consultas reales del corpus
# no aparece ninguna (0%); están acá porque el SQL lo escribe un modelo y mañana
# puede escribirlas.
#   LIKE/GLOB : el LIKE de SQLite es INSENSIBLE a mayúsculas en ASCII y el de
#               DuckDB no. "'Paso de los Toros' LIKE '%toros'" da 1 en SQLite y
#               false en DuckDB: devolvería MENOS filas, sin error.
#   UPPER/LOWER: SQLite solo cambia el caso de los ASCII (UPPER('peñarol') deja
#               la ñ intacta) y DuckDB es Unicode. Cambia el texto que se muestra.
_INCOMPATIBLES = re.compile(r"\b(LIKE|GLOB|UPPER|LOWER)\b", re.I)

_CONEXIONES = {}          # ruta de la base -> conexión DuckDB, o None si quedó degradada
_CANDADO = threading.Lock()
_CAIDAS = collections.Counter()   # base -> consultas que tuvieron que repetirse en SQLite
_DERIVADAS = collections.Counter()  # base -> consultas mandadas a SQLite a propósito (_INCOMPATIBLES)
_AVISADAS = set()                 # bases cuya primera caída ya se avisó (no se repite por consulta)


def _avisar(msg):
    """Una línea al journal. Va a stderr porque systemd lo captura sin configurar nada."""
    sys.stderr.write("[ejecutor] %s\n" % msg)
    sys.stderr.flush()


def _abrir(db):
    """Abre y attacha la base en DuckDB, o devuelve None si no se puede confiar en ella.

    Devolver None NO es un error fatal: significa 'esta base se sirve por SQLite'."""
    try:
        import duckdb
    except ImportError:
        _avisar("duckdb no está instalado: todo se sirve por SQLite")
        return None
    try:
        con = duckdb.connect()
        con.execute("INSTALL sqlite; LOAD sqlite;")
        con.execute("SET GLOBAL default_null_order='%s';" % _ORDEN_NULOS)
        con.execute("SET GLOBAL integer_division=true;")
        con.execute("ATTACH '%s' AS s (TYPE sqlite, READ_ONLY); USE s;" % db)
        # Los canarios deciden: si algún invariante no calca a SQLite, no se usa.
        for nombre, sql, esperado in _CANARIOS:
            if con.execute(sql).fetchall() != esperado:
                con.close()
                _avisar("%s: el canario de %s NO calca a SQLite -> se sirve por SQLite"
                        % (db, nombre))
                return None
        _avisar("%s: servida por DuckDB (%d canarios OK)" % (db, len(_CANARIOS)))
        return con
    except Exception as e:
        _avisar("%s: no se pudo abrir en DuckDB (%s: %s) -> se sirve por SQLite"
                % (db, type(e).__name__, e))
        return None


def _conexion(db):
    """Conexión DuckDB para esa base, creada una sola vez (~100 ms) y reutilizada.

    La ruta se normaliza porque los llamadores no coinciden: el motor 2011 pasa
    'datos/censo.db' relativa y los otros tres la absoluta. Sin normalizar, la
    misma base referida de las dos formas abriría DOS conexiones."""
    if MOTOR != "duckdb":
        return None
    db = os.path.abspath(db)
    con = _CONEXIONES.get(db, False)
    if con is not False:
        return con
    with _CANDADO:
        if db not in _CONEXIONES:
            _CONEXIONES[db] = _abrir(db)
        return _CONEXIONES[db]


def _caida(db, sql, e):
    """Registra que una consulta tuvo que repetirse en SQLite. Avisa solo la primera
    vez por base: si DuckDB rechaza algo sistemáticamente, no queremos el journal
    inundado, pero sí queremos saber que pasó y con qué SQL."""
    _CAIDAS[db] += 1
    if db not in _AVISADAS:
        _AVISADAS.add(db)
        _avisar("%s: consulta repetida en SQLite (%s: %s) | SQL: %s"
                % (db, type(e).__name__, str(e).replace("\n", " ")[:200],
                   " ".join(sql.split())[:200]))


def _sqlite_filas(db, sql):
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(f) for f in con.execute(sql).fetchall()]
    finally:
        con.close()


def _normalizar(v):
    """Deja los valores de DuckDB con los MISMOS tipos que devolvía SQLite.

    DuckDB tiene tipos que SQLite no: ROUND sobre un literal decimal devuelve
    Decimal (que además json.dumps no sabe serializar, o sea 500 en la API) y una
    expresión booleana devuelve True/False donde SQLite devolvía 1/0. Los
    llamadores, el redactor y el frontend fueron escritos contra los tipos de
    SQLite, así que la conversión va acá y no en cada uno de ellos."""
    if isinstance(v, bool):          # antes que int: en Python bool ES int
        return int(v)
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v


def filas(db, sql):
    """Ejecuta el SQL ya validado y devuelve una lista de dicts.

    Misma forma de salida que el camino viejo de SQLite (row_factory=Row -> dict),
    para que los llamadores no noten la diferencia."""
    db = os.path.abspath(db)   # una sola clave por base, en las conexiones y en el contador
    if _INCOMPATIBLES.search(sql):
        _DERIVADAS[db] += 1
        return _sqlite_filas(db, sql)
    con = _conexion(db)
    if con is not None:
        try:
            cur = con.cursor()
            cur.execute("USE s;")
            res = cur.execute(sql)
            columnas = [d[0] for d in res.description]
            return [dict(zip(columnas, (_normalizar(v) for v in f)))
                    for f in res.fetchall()]
        except Exception as e:
            _caida(db, sql, e)   # cae a SQLite: se pierde velocidad, no la respuesta
    return _sqlite_filas(db, sql)


def escalar(db, sql):
    """Primer valor de la primera fila (COUNT(*) y similares). None si no hay filas."""
    db = os.path.abspath(db)
    con = _conexion(db) if not _INCOMPATIBLES.search(sql) else None
    if con is not None:
        try:
            cur = con.cursor()
            cur.execute("USE s;")
            f = cur.execute(sql).fetchone()
            return _normalizar(f[0]) if f else None
        except Exception as e:
            _caida(db, sql, e)
    con2 = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        f = con2.execute(sql).fetchone()
        return f[0] if f else None
    finally:
        con2.close()


def estado():
    """Qué motor quedó sirviendo cada base y cuántas consultas se cayeron a SQLite.

    Las conexiones son perezosas: recién después de la primera consulta de cada
    censo el diccionario `bases` está completo. `caidas` en cero es la señal de
    que el camino rápido está sirviendo de verdad.

    `caidas` son fallos inesperados de DuckDB; `derivadas` son las consultas que
    se mandaron a SQLite a propósito por tener una construcción que las dos bases
    no resuelven igual. Las primeras hay que mirarlas; las segundas son el
    sistema funcionando como se diseñó."""
    return {"motor_pedido": MOTOR,
            "bases": {db: ("duckdb" if c is not None else "sqlite")
                      for db, c in _CONEXIONES.items()},
            "caidas": dict(_CAIDAS),
            "derivadas": dict(_DERIVADAS)}
