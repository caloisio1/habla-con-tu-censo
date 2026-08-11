"""comun/ejecutor.py — Ejecución de las consultas analíticas, compartida por los cuatro motores.

POR QUÉ. Las consultas que la app genera de verdad no son filtros puntuales: son
cruces (departamento × sexo × tramo de edad), JOINs contra el nomenclátor y
jerarquías. Sobre SQLite esas consultas cuestan entre 2 y 19 segundos, y en las
más pesadas son casi la mitad del tiempo total de la respuesta. DuckDB, que es
columnar, las resuelve entre 5 y 12 veces más rápido.

UN SOLO MOTOR. Antes esto ejecutaba en DuckDB y repetía en SQLite lo que DuckDB
rechazara. Esa red se sacó a propósito: dos motores dentro del mismo sistema no
son una arquitectura, son un parche encima de otro, y encima obligaban a
mantener dos copias de cada censo. Volver atrás es `git revert` y reiniciar.

QUÉ SE PIERDE Y POR QUÉ SE ACEPTA. Con red, una consulta que DuckDB rechazaba se
contestaba igual, más lento. Sin red, devuelve error. La apuesta es que un
rechazo VISIBLE se arregla y uno tapado no: el último que quedaba —un ORDER BY
por una columna no agrupada— se arregló en `comun/orden.py`, de forma
determinista y para todas las consultas, y sólo se pudo encontrar porque se lo
midió. Medido después de ese arreglo: 0 rechazos en 118 pasadas sobre 15 formas
de consulta y los cuatro censos (informes/fallback_amplio_20260811.md).

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
con consultas canario al abrir cada conexión. Ya no hay a dónde degradar, así que
un canario que no calque la semántica esperada deja la base SIN abrir: la app
avisa que no puede responder. Es fail-closed a propósito —los dos invariantes
cambian la respuesta sin dar error, que es la peor forma de romperse—, y arrancar
igual sería servir cifras falsas con cara de buenas.

PREFERIBLE NO CONTESTAR A CONTESTAR MAL. Es la regla que ordena todo lo demás.
Cuando DuckDB rechaza una consulta porque compara texto con número —`asc_afro = 1`
sobre una columna que guarda 'Si'/'No'—, no está diciendo "no sé hacer esto":
está diciendo "esta consulta no tiene sentido". SQLite la ejecutaba igual y
devolvía 0,0 % de afrodescendientes en TODOS los departamentos, cuando la cifra
real va de 2,9 % a 16,9 %. Una cifra plausible, publicable y falsa. Esos fallos
se levantan como ConsultaIncoherente y el motor los convierte en un rechazo.

Por la misma razón se RECHAZAN las construcciones que los dos motores no
resolvían igual (LIKE, GLOB, UPPER, LOWER): ver `_INCOMPATIBLES`.

NADA DE ESTO ES MUDO. Cada rechazo se avisa en el journal
(`journalctl -u censo-query-uy | grep ejecutor`) y queda contado en `estado()`.
Un rechazo que nadie ve es un rechazo que nadie arregla.
"""
import collections
import decimal
import os
import re
import sys
import threading

# Semántica de NULL heredada de SQLite: ASC -> primero, DESC -> último. Se
# conserva aunque SQLite ya no ejecute nada, porque es la semántica contra la que
# se escribieron los prompts, los guards y las cifras publicadas: cambiarla ahora
# movería respuestas sin que nadie lo pidiera.
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

# CONSTRUCCIONES CUYA RESPUESTA DEPENDERÍA DEL MOTOR. Mientras hubo dos motores
# se derivaban a SQLite; ahora se RECHAZAN, que es la misma decisión de fondo:
# antes que devolver una cifra que depende de quién ejecute, no se devuelve nada.
#   LIKE/GLOB : el LIKE de SQLite es INSENSIBLE a mayúsculas en ASCII y el de
#               DuckDB no. "'Paso de los Toros' LIKE '%toros'" da 1 en SQLite y
#               false en DuckDB: devolvería MENOS filas, sin error.
#   UPPER/LOWER: SQLite solo cambia el caso de los ASCII (UPPER('peñarol') deja
#               la ñ intacta) y DuckDB es Unicode. Cambia el texto que se muestra.
#
# Rechazar no cuesta cobertura: son 0 casos en las 76 consultas reales del corpus
# y 0 en las 118 pasadas de la medición amplia. El sistema NUNCA las genera por
# su cuenta -para eso está el nomenclátor, que resuelve los nombres a códigos
# ANTES del SQL-; aparecerían sólo si el modelo se sale del molde, y ahí un
# rechazo visible es justo lo que hace falta para corregirlo.
_INCOMPATIBLES = re.compile(r"\b(LIKE|GLOB|UPPER|LOWER)\b", re.I)

# Fallos que significan que la CONSULTA está mal escrita, no que el motor no sepa
# resolverla. Se distinguen porque merecen otro mensaje: no es "no pude", es "eso
# que preguntaste no tiene sentido con estos datos".
_INCOHERENTES = {"ConversionException"}


class SinRespuesta(Exception):
    """Raíz de todo lo que este módulo levanta cuando no va a devolver filas.

    Existe para que los motores capturen UNA cosa y ninguna quede afuera. Mientras
    hubo red, casi todos los fallos se absorbían repitiendo en SQLite y el único
    que llegaba arriba era ConsultaIncoherente; sin red, cualquiera de estos
    llegaría al usuario como un error 500 si no se lo contara. Un rechazo tiene
    que salir explicado, no como una pantalla rota."""


class ConsultaIncoherente(SinRespuesta):
    """El SQL compara valores que no son comparables (texto contra número).

    No es un fallo del motor: es una consulta sin sentido, que SQLite
    respondería con una cifra falsa en vez de avisar."""


class ConstruccionAmbigua(SinRespuesta):
    """El SQL usa una construcción cuya respuesta dependería del motor.

    Ver `_INCOMPATIBLES`. No se ejecuta: una cifra que cambia según quién la
    calcule no es una cifra."""


class BaseNoDisponible(SinRespuesta):
    """La base no se pudo abrir, o se abrió sin la semántica esperada.

    Fail-closed: antes había a dónde degradar y ahora no, así que no se responde
    en vez de responder con otra semántica."""


class ConsultaRechazada(SinRespuesta):
    """DuckDB no pudo ejecutar la consulta.

    Antes esto se repetía en SQLite y nadie se enteraba. Ahora es visible, que es
    la condición para que se arregle: el último caso que quedaba -un ORDER BY por
    una columna no agrupada- se corrigió en `comun/orden.py` justamente porque se
    lo pudo ver."""


_CONEXIONES = {}          # ruta de la base -> conexión DuckDB, o None si no se pudo abrir
_CANDADO = threading.Lock()
_RECHAZOS = collections.Counter()    # base -> consultas que DuckDB no pudo ejecutar
_AMBIGUAS = collections.Counter()    # base -> consultas rechazadas por _INCOMPATIBLES
_AVISADAS = set()                 # bases cuyo primer rechazo ya se avisó (no se repite por consulta)
_MODO = {}                        # base -> 'nativo' | 'puente'  (como quedo abierta en DuckDB)


def _avisar(msg):
    """Una línea al journal. Va a stderr porque systemd lo captura sin configurar nada."""
    sys.stderr.write("[ejecutor] %s\n" % msg)
    sys.stderr.flush()


def nativa_de(db):
    """Ruta de la base DuckDB NATIVA que acompaña a una base SQLite, si existe.

    Por CONVENCIÓN, no por configuración: junto a `datos/censo2023.db` se busca
    `datos/censo2023.duckdb`. Poner el archivo la activa y borrarlo la desactiva,
    sin tocar código ni variables de entorno. Eso hace que el despliegue sea un
    `mv` y la vuelta atrás, otro."""
    ruta = os.path.splitext(db)[0] + ".duckdb"
    return ruta if os.path.exists(ruta) else None


def _abrir(db):
    """Abre la base en DuckDB, o devuelve None si no se puede confiar en ella.

    Dos formas de abrir, en este orden:

    1. NATIVA. Si existe el .duckdb hermano, se abre directo en solo lectura.
       Es entre 23x y 46x más rápido que SQLite segun el censo, contra el 9x del
       puente, porque no hay traduccion: el formato ya es columnar.
    2. PUENTE. Si no, se attacha el .db de siempre con el lector sqlite. Queda
       como compatibilidad para una base que todavía no se haya reconstruido;
       el camino normal es el nativo.

    Devolver None significa que esa base NO se puede responder. Antes era una
    degradación silenciosa a SQLite; ahora es un fallo, y quien consulte recibe
    BaseNoDisponible."""
    try:
        import duckdb
    except ImportError:
        _avisar("duckdb no está instalado: no hay con qué responder")
        return None
    nativa = nativa_de(db)
    try:
        if nativa:
            con = duckdb.connect(nativa, read_only=True)
        else:
            con = duckdb.connect()
            con.execute("INSTALL sqlite; LOAD sqlite;")
        con.execute("SET GLOBAL default_null_order='%s';" % _ORDEN_NULOS)
        con.execute("SET GLOBAL integer_division=true;")
        if not nativa:
            con.execute("ATTACH '%s' AS s (TYPE sqlite, READ_ONLY); USE s;" % db)
        # Los canarios deciden: si algún invariante no da la semántica esperada,
        # la base no se abre. Ya no hay a dónde degradar, y responder con otra
        # semántica sería devolver cifras distintas sin avisar.
        for nombre, sql, esperado in _CANARIOS:
            if con.execute(sql).fetchall() != esperado:
                con.close()
                _avisar("%s: el canario de %s NO da la semántica esperada -> base NO disponible"
                        % (db, nombre))
                return None
        _MODO[os.path.abspath(db)] = "nativo" if nativa else "puente"
        _avisar("%s: servida por DuckDB %s (%d canarios OK)"
                % (db, "NATIVO" if nativa else "sobre el lector sqlite", len(_CANARIOS)))
        return con
    except Exception as e:
        _avisar("%s: no se pudo abrir en DuckDB (%s: %s) -> base NO disponible"
                % (db, type(e).__name__, e))
        return None


def _conexion(db):
    """Conexión DuckDB para esa base, creada una sola vez (~100 ms) y reutilizada.

    La ruta se normaliza porque los llamadores no coinciden: el motor 2011 pasa
    'datos/censo.db' relativa y los otros tres la absoluta. Sin normalizar, la
    misma base referida de las dos formas abriría DOS conexiones."""
    db = os.path.abspath(db)
    con = _CONEXIONES.get(db, False)
    if con is not False:
        return con
    with _CANDADO:
        if db not in _CONEXIONES:
            _CONEXIONES[db] = _abrir(db)
        return _CONEXIONES[db]


def _levantar(db, sql, e):
    """Convierte un fallo de DuckDB en la excepción que le corresponde.

    Se distingue el fallo que dice que la CONSULTA está mal del que dice que el
    motor no pudo, porque merecen mensajes distintos. Ninguno de los dos se
    esconde: antes el segundo se repetía en SQLite y el usuario nunca se
    enteraba.

    El SQL se registra largo a propósito. La causa más común es el GROUP BY:
    SQLite dejaba poner en el ORDER BY una columna que no está agrupada y DuckDB
    lo rechaza por SQL estándar. Distinguir eso de un problema real necesita ver
    la consulta entera, y con un recorte corto no se puede.

    Se avisa una vez por base: si algo se rechaza sistemáticamente no queremos el
    journal inundado, pero sí queremos saber que pasó y con qué SQL."""
    if type(e).__name__ in _INCOHERENTES:
        _avisar("%s: consulta INCOHERENTE (%s) | SQL: %s"
                % (db, str(e).replace("\n", " ")[:200],
                   " ".join(sql.split())[:600]))
        raise ConsultaIncoherente(str(e).split("\n")[0]) from e
    _RECHAZOS[db] += 1
    if db not in _AVISADAS:
        _AVISADAS.add(db)
        _avisar("%s: consulta RECHAZADA por DuckDB (%s: %s) | SQL: %s"
                % (db, type(e).__name__, str(e).replace("\n", " ")[:200],
                   " ".join(sql.split())[:600]))
    raise ConsultaRechazada(str(e).split("\n")[0]) from e


def _cursor(db, sql, params):
    """Cursor con el SQL ya ejecutado, o la excepción que corresponda.

    Es el único punto por el que se ejecuta: `filas()`, `tuplas()` y `escalar()`
    se diferencian sólo en cómo leen el resultado."""
    if _INCOMPATIBLES.search(sql):
        _AMBIGUAS[os.path.abspath(db)] += 1
        _avisar("%s: consulta RECHAZADA por construcción ambigua | SQL: %s"
                % (db, " ".join(sql.split())[:600]))
        raise ConstruccionAmbigua(
            "La consulta usa LIKE, GLOB, UPPER o LOWER, cuya respuesta dependería "
            "del motor que la ejecute.")
    con = _conexion(db)
    if con is None:
        raise BaseNoDisponible("No se pudo abrir %s en DuckDB." % db)
    cur = con.cursor()
    if _MODO.get(os.path.abspath(db)) == "puente":
        # Solo el puente necesita el USE: el cursor no hereda el esquema activo
        # de la conexion. En la base nativa las tablas estan en main y un USE s
        # fallaria.
        cur.execute("USE s;")
    try:
        return cur.execute(sql, list(params)) if params else cur.execute(sql)
    except ConsultaIncoherente:
        raise
    except Exception as e:
        _levantar(os.path.abspath(db), sql, e)


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


def filas(db, sql, params=()):
    """Ejecuta el SQL ya validado y devuelve una lista de dicts.

    Misma forma de salida que el camino viejo de SQLite (row_factory=Row -> dict),
    para que los llamadores no noten la diferencia.

    `params` liga los `?` del SQL. Lo usa el nomenclátor, que consulta por código
    -no por nombre- y por lo tanto NO puede construir el SQL concatenando: el
    código sale de un catálogo, pero ligarlo es lo que garantiza que siga siendo
    un valor y no texto de consulta."""
    res = _cursor(db, sql, params)
    columnas = [d[0] for d in res.description]
    return [dict(zip(columnas, (_normalizar(v) for v in f)))
            for f in res.fetchall()]


def tuplas(db, sql, params=()):
    """Como `filas()`, pero POSICIONAL: una lista de tuplas, no de dicts.

    POR QUÉ EXISTE Y NO ALCANZA CON filas(). Un dict pierde las columnas
    HOMÓNIMAS. `SELECT COUNT(*), COUNT(*)` -que es como el nomenclátor pide el
    par (ponderado, crudo) en los censos sin ponderación- produce dos columnas
    con el mismo nombre, y al armar el dict la segunda pisa a la primera: la fila
    llega con UN valor donde el llamador espera DOS, y el desempaquetado falla.

    Los llamadores que desempaquetan por posición usan esta; los que leen por
    nombre de columna, `filas()`."""
    return [tuple(_normalizar(v) for v in f)
            for f in _cursor(db, sql, params).fetchall()]


def escalar(db, sql, params=()):
    """Primer valor de la primera fila (COUNT(*) y similares). None si no hay filas."""
    f = _cursor(db, sql, params).fetchone()
    return _normalizar(f[0]) if f else None


_MOTIVOS = {
    ConsultaIncoherente: "consulta incoherente",
    ConstruccionAmbigua: "construcción ambigua",
    BaseNoDisponible: "base no disponible",
    ConsultaRechazada: "consulta rechazada por el motor",
}


def motivo(e):
    """Etiqueta corta del porqué no hubo respuesta, para el veredicto.

    Vive acá y no en cada motor porque los tres registran el mismo veredicto y
    una etiqueta que se escribe tres veces se desincroniza dos."""
    return _MOTIVOS.get(type(e), "no se pudo consultar")


def estado():
    """Cómo quedó abierta cada base y cuántas consultas no se pudieron responder.

    Las conexiones son perezosas: recién después de la primera consulta de cada
    censo el diccionario `bases` está completo, y una base en `no disponible` es
    una que no se pudo abrir o cuyos canarios no dieron la semántica esperada.

    `rechazos` son consultas que DuckDB no pudo ejecutar: cada una es una
    pregunta que alguien hizo y no obtuvo respuesta, así que son las que hay que
    mirar y arreglar. `ambiguas` son las rechazadas por usar LIKE, GLOB, UPPER o
    LOWER, cuya respuesta dependería del motor.

    Los dos contadores en cero es lo normal. Antes esto medía cuánto se usaba la
    red de SQLite; ahora que no hay red, mide directamente cuánto se está
    dejando de responder."""
    return {"bases": {db: (_MODO.get(db, "duckdb") if c is not None else "no disponible")
                      for db, c in _CONEXIONES.items()},
            "rechazos": dict(_RECHAZOS),
            "ambiguas": dict(_AMBIGUAS)}
