"""Tests del ejecutor de consultas (comun/ejecutor.py). Run: pytest tests/test_ejecutor.py

Lo que se prueba acá es la EQUIVALENCIA entre los dos motores, no la velocidad.
DuckDB corre las mismas consultas sobre los mismos archivos .db, y la única forma
de que ese cambio sea aceptable es que devuelva exactamente lo mismo que SQLite.

Las divergencias peligrosas no dan error: dan otra cifra. Por eso cada una tiene
su test, escrito contra el resultado de SQLite, que es el que la app viene dando
desde siempre y contra el que se validaron todas las baterías.

Todo corre sobre una base temporal chiquita: es determinista, no toca las bases
de producción y no cuesta tokens.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from comun import ejecutor

duckdb = pytest.importorskip("duckdb")


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    """Una base de juguete con la forma de las de verdad: códigos geográficos en
    TEXT con cero a la izquierda, edad entera, un peso float y NULLs."""
    ruta = str(tmp_path_factory.mktemp("ejecutor") / "mini.db")
    cx = sqlite3.connect(ruta)
    cx.execute("CREATE TABLE p (depto TEXT, nombre TEXT, edad INTEGER, w REAL)")
    cx.executemany("INSERT INTO p VALUES (?,?,?,?)", [
        ("01", "Montevideo", 17, 1.5),
        ("01", "Montevideo", 22, 2.0),
        ("03", "Paso de los Toros", 64, 1.0),
        ("10", "Peñarol", 7, 3.0),
        (None, None, None, 1.0),
    ])
    cx.commit()
    cx.close()
    return ruta


def por_sqlite(base, sql):
    cx = sqlite3.connect("file:%s?mode=ro" % base, uri=True)
    cx.row_factory = sqlite3.Row
    try:
        return [dict(f) for f in cx.execute(sql).fetchall()]
    finally:
        cx.close()


def iguales(base, sql):
    """El invariante del módulo entero: los dos motores dicen lo mismo."""
    assert ejecutor.filas(base, sql) == por_sqlite(base, sql)


# --- los dos invariantes que rompen en silencio -----------------------------

def test_tramo_quinquenal_de_edad(base):
    """El patrón más usado de la app. SQLite: 17/5*5 = 15. DuckDB por defecto
    daba 17.0, o sea un grupo por edad en vez de uno por tramo."""
    sql = "SELECT (edad/5)*5 AS tramo, COUNT(*) AS n FROM p WHERE edad IS NOT NULL GROUP BY 1 ORDER BY 1"
    iguales(base, sql)
    assert ejecutor.filas(base, sql)[0]["tramo"] == 5      # el de 7 años cae en 5, no en 7.0


def test_division_entera_es_truncada(base):
    assert ejecutor.filas(base, "SELECT 17/5 AS a, -7/2 AS b")[0] == {"a": 3, "b": -3}


def test_el_null_va_primero_en_asc(base):
    """SQLite trata el NULL como el valor más chico. Si DuckDB lo mandara al
    final, un ORDER BY ... LIMIT 1 contestaría otro departamento."""
    iguales(base, "SELECT depto FROM p ORDER BY depto")
    assert ejecutor.filas(base, "SELECT depto FROM p ORDER BY depto LIMIT 1")[0]["depto"] is None


def test_maximo_por_orden_descendente(base):
    iguales(base, "SELECT depto, SUM(w) AS t FROM p GROUP BY 1 ORDER BY t DESC LIMIT 1")


# --- tipos: lo que sale tiene que poder viajar en JSON ----------------------

def test_los_booleanos_salen_como_0_y_1(base):
    """SQLite no tiene tipo booleano; el frontend y el redactor esperan 0/1."""
    f = ejecutor.filas(base, "SELECT 1=1 AS si, 1=2 AS no")[0]
    assert f == {"si": 1, "no": 0}
    assert isinstance(f["si"], int) and not isinstance(f["si"], bool)


def test_round_no_devuelve_decimal(base):
    """ROUND sobre un literal decimal da Decimal en DuckDB, y json.dumps no lo
    sabe serializar: sería un 500 en la API."""
    import json
    f = ejecutor.filas(base, "SELECT ROUND(2.5,1) AS r")[0]
    assert isinstance(f["r"], float)
    json.dumps(f)   # tiene que no explotar


def test_el_porcentaje_tipico_de_la_app(base):
    iguales(base, "SELECT depto, ROUND(100.0*SUM(w)/(SELECT SUM(w) FROM p),1) AS pct "
                  "FROM p GROUP BY 1 ORDER BY 1")


# --- lo que se manda a SQLite a propósito -----------------------------------

def test_like_se_sirve_por_sqlite(base):
    """El LIKE de SQLite es insensible a mayúsculas y el de DuckDB no:
    devolvería MENOS filas sin dar error. Va por SQLite."""
    sql = "SELECT nombre FROM p WHERE nombre LIKE '%toros'"
    assert ejecutor.filas(base, sql) == [{"nombre": "Paso de los Toros"}]
    iguales(base, sql)


def test_upper_se_sirve_por_sqlite(base):
    """SQLite solo pasa a mayúscula los ASCII: UPPER('Peñarol') deja la ñ."""
    iguales(base, "SELECT UPPER(nombre) AS n FROM p WHERE nombre='Peñarol'")


def test_las_derivadas_se_cuentan_aparte_de_las_caidas(base):
    ejecutor.filas(base, "SELECT nombre FROM p WHERE nombre LIKE 'M%'")
    e = ejecutor.estado()
    clave = os.path.abspath(base)
    assert e["derivadas"].get(clave, 0) >= 1
    assert e["caidas"].get(clave, 0) == 0   # mandarla a SQLite a propósito NO es un fallo


# --- la red de seguridad ----------------------------------------------------

def test_si_duckdb_falla_la_respuesta_igual_sale(base):
    """sqlite_version() no existe en DuckDB: tiene que caer a SQLite y contestar."""
    f = ejecutor.filas(base, "SELECT sqlite_version() AS v")
    assert f and f[0]["v"]
    assert ejecutor.estado()["caidas"].get(os.path.abspath(base), 0) >= 1


def test_escalar_da_lo_mismo_que_la_consulta_completa(base):
    assert ejecutor.escalar(base, "SELECT COUNT(*) FROM p") == 5


def test_se_puede_apagar_con_una_variable_de_entorno(base, monkeypatch):
    """La palanca de emergencia: CENSO_MOTOR_SQL=sqlite y no se usa DuckDB."""
    monkeypatch.setattr(ejecutor, "MOTOR", "sqlite")
    assert ejecutor._conexion(base) is None
    iguales(base, "SELECT depto, COUNT(*) AS n FROM p GROUP BY 1 ORDER BY 1")


# --- los canarios que deciden si se usa DuckDB ------------------------------

def test_los_canarios_pasan_en_esta_maquina(base):
    """Si algún canario fallara, la base se serviría por SQLite. Que este test
    pase confirma que el camino rápido está realmente activo."""
    for _nombre, sql, esperado in ejecutor._CANARIOS:
        assert [tuple(f.values()) for f in ejecutor.filas(base, sql)] == esperado
    assert ejecutor._conexion(base) is not None
