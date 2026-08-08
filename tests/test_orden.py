"""Tests del desempate del ORDER BY (comun/orden.py). Run: pytest tests/test_orden.py

Lo que se protege acá es que la MISMA pregunta devuelva SIEMPRE las mismas filas.
Un `ORDER BY ... LIMIT` con empates no define qué filas salen: si diez segmentos
tienen el mismo conteo y el LIMIT corta en el medio, cuál entra al mapa lo decide
el plan de ejecución. No es un problema de un motor -SQLite y DuckDB lo tienen
igual-; se hizo visible al tener dos.
"""
import os
import sys

import pytest
import sqlglot

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from comun.orden import desempatar


def sql(texto):
    return desempatar(sqlglot.parse_one(texto, read="sqlite")).sql(dialect="sqlite")


def test_agrega_las_columnas_que_faltan():
    assert sql("SELECT dpto, COUNT(*) AS n FROM t GROUP BY 1 ORDER BY n DESC LIMIT 300") == \
        "SELECT dpto, COUNT(*) AS n FROM t GROUP BY 1 ORDER BY n DESC, 1, 2 LIMIT 300"


def test_el_criterio_original_manda_y_el_desempate_va_al_final():
    """El desempate NO puede cambiar el orden principal: solo rompe empates."""
    s = sql("SELECT dpto, secc, COUNT(*) AS n FROM t GROUP BY 1,2 ORDER BY n DESC LIMIT 300")
    assert s.index("n DESC") < s.index(", 1, 2, 3")


def test_sin_order_by_tambien_se_ordena():
    """Un LIMIT sin ORDER BY devuelve un subconjunto ARBITRARIO. Con esto pasa a
    ser reproducible."""
    assert sql("SELECT dpto, COUNT(*) AS n FROM t GROUP BY 1 LIMIT 300") == \
        "SELECT dpto, COUNT(*) AS n FROM t GROUP BY 1 ORDER BY 1, 2 LIMIT 300"


def test_no_repite_lo_que_ya_estaba():
    assert sql("SELECT dpto, COUNT(*) AS n FROM t GROUP BY 1 ORDER BY 1, 2 LIMIT 300") == \
        "SELECT dpto, COUNT(*) AS n FROM t GROUP BY 1 ORDER BY 1, 2 LIMIT 300"


def test_es_idempotente():
    """Aplicarlo dos veces tiene que dar lo mismo: el guard puede correr de nuevo
    sobre un SQL ya normalizado."""
    una = sql("SELECT dpto, COUNT(*) AS n FROM t GROUP BY 1 ORDER BY n DESC LIMIT 300")
    dos = desempatar(sqlglot.parse_one(una, read="sqlite")).sql(dialect="sqlite")
    assert una == dos


def test_no_escribe_ninguna_clausula_de_nulos():
    """EL invariante. SQLite trata el NULL como el valor más chico y lo pone
    primero en ASC. Si el SQL generado dijera NULLS LAST, invertiría esa
    semántica en los dos motores -es el mismo invariante que protegen los
    canarios de comun/ejecutor.py-."""
    for consulta in [
        "SELECT dpto, COUNT(*) AS n FROM t GROUP BY 1 ORDER BY n DESC LIMIT 300",
        "SELECT dpto, COUNT(*) AS n FROM t GROUP BY 1 LIMIT 300",
        "SELECT a, b, c, COUNT(*) AS n FROM t GROUP BY 1,2,3 ORDER BY 2 LIMIT 50",
    ]:
        assert "NULLS" not in sql(consulta).upper()


def test_la_estrella_no_se_toca():
    """Con `*` no se sabe cuántas columnas son: forzar un orden a ciegas sería
    peor que el empate."""
    assert sql("SELECT * FROM t LIMIT 300") == "SELECT * FROM t LIMIT 300"


def test_funciona_con_cte():
    s = sql("WITH x AS (SELECT dpto, COUNT(*) c FROM t GROUP BY 1) "
            "SELECT dpto, c FROM x ORDER BY c DESC LIMIT 300")
    assert s.endswith("ORDER BY c DESC, 1, 2 LIMIT 300")


def test_sin_alias_tambien():
    assert sql("SELECT dpto, COUNT(*) FROM t GROUP BY 1 ORDER BY 2 DESC LIMIT 300") == \
        "SELECT dpto, COUNT(*) FROM t GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 300"


# --- la prueba de verdad: dos motores, el mismo corte ------------------------

def test_el_corte_del_limit_es_el_mismo_en_los_dos_motores(tmp_path):
    """Se arma un empate a propósito -20 filas con el mismo conteo, LIMIT en el
    medio- y se comprueba que SQLite y DuckDB devuelvan LAS MISMAS filas.

    Sin desempate esto falla: cada motor corta por donde quiere."""
    duckdb = pytest.importorskip("duckdb")
    import sqlite3

    ruta = str(tmp_path / "empates.db")
    cx = sqlite3.connect(ruta)
    cx.execute("CREATE TABLE t (cat TEXT, w INTEGER)")
    # 20 categorías con EXACTAMENTE el mismo peso: todo es empate.
    cx.executemany("INSERT INTO t VALUES (?,?)",
                   [("cat%02d" % i, 1) for i in range(20) for _ in range(3)])
    cx.commit()
    cx.close()

    consulta = "SELECT cat, COUNT(*) AS n FROM t GROUP BY 1 ORDER BY n DESC LIMIT 5"
    con_desempate = sql(consulta)

    cs = sqlite3.connect("file:%s?mode=ro" % ruta, uri=True)
    dk = duckdb.connect()
    dk.execute("INSTALL sqlite; LOAD sqlite;")
    dk.execute("SET GLOBAL default_null_order='NULLS_FIRST_ON_ASC_LAST_ON_DESC';")
    dk.execute("ATTACH '%s' AS s (TYPE sqlite, READ_ONLY); USE s;" % ruta)

    a = cs.execute(con_desempate).fetchall()
    b = [tuple(f) for f in dk.execute(con_desempate).fetchall()]
    assert len(a) == 5
    assert a == b, "el corte del LIMIT difiere entre motores pese al desempate"
    # y es el corte que se espera: las cinco primeras por categoría
    assert [f[0] for f in a] == ["cat00", "cat01", "cat02", "cat03", "cat04"]
    cs.close()
    dk.close()
