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

EL INVARIANTE QUE NO SE PUEDE PERDER. SQLite trata el NULL como el valor más chico
(ASC -> primero); DuckDB por defecto lo manda al final. En un `ORDER BY ... LIMIT 1`
—"¿qué departamento tiene más X?"— una fila NULL en la punta cambia la respuesta
SIN error, en silencio. Por eso se fija `default_null_order` Y ADEMÁS se comprueba
con una consulta canario al abrir cada conexión. Si el canario no da exactamente
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
import os
import sqlite3
import sys
import threading

MOTOR = os.environ.get("CENSO_MOTOR_SQL", "duckdb").strip().lower()

# Semántica de NULL de SQLite: ASC -> primero, DESC -> último.
_ORDEN_NULOS = "NULLS_FIRST_ON_ASC_LAST_ON_DESC"

# Canario: no toca ninguna base, así que vale igual para los cuatro censos.
# Con la semántica de SQLite tiene que dar [None, 1, 2].
_CANARIO = "SELECT x FROM (VALUES (1),(NULL),(2)) t(x) ORDER BY x"
_CANARIO_ESPERADO = [(None,), (1,), (2,)]

_CONEXIONES = {}          # ruta de la base -> conexión DuckDB, o None si quedó degradada
_CANDADO = threading.Lock()
_CAIDAS = collections.Counter()   # base -> consultas que tuvieron que repetirse en SQLite
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
        con.execute("ATTACH '%s' AS s (TYPE sqlite, READ_ONLY); USE s;" % db)
        # El canario decide: si el orden de los NULL no calca a SQLite, no se usa.
        if con.execute(_CANARIO).fetchall() != _CANARIO_ESPERADO:
            con.close()
            _avisar("%s: el canario de orden de NULL NO calca a SQLite -> se sirve por SQLite" % db)
            return None
        _avisar("%s: servida por DuckDB (canario de NULL OK)" % db)
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


def filas(db, sql):
    """Ejecuta el SQL ya validado y devuelve una lista de dicts.

    Misma forma de salida que el camino viejo de SQLite (row_factory=Row -> dict),
    para que los llamadores no noten la diferencia."""
    db = os.path.abspath(db)   # una sola clave por base, en las conexiones y en el contador
    con = _conexion(db)
    if con is not None:
        try:
            cur = con.cursor()
            cur.execute("USE s;")
            res = cur.execute(sql)
            columnas = [d[0] for d in res.description]
            return [dict(zip(columnas, f)) for f in res.fetchall()]
        except Exception as e:
            _caida(db, sql, e)   # cae a SQLite: se pierde velocidad, no la respuesta
    return _sqlite_filas(db, sql)


def escalar(db, sql):
    """Primer valor de la primera fila (COUNT(*) y similares). None si no hay filas."""
    db = os.path.abspath(db)
    con = _conexion(db)
    if con is not None:
        try:
            cur = con.cursor()
            cur.execute("USE s;")
            f = cur.execute(sql).fetchone()
            return f[0] if f else None
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
    que el camino rápido está sirviendo de verdad."""
    return {"motor_pedido": MOTOR,
            "bases": {db: ("duckdb" if c is not None else "sqlite")
                      for db, c in _CONEXIONES.items()},
            "caidas": dict(_CAIDAS)}
