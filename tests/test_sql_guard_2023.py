"""Tests del guard del motor 2023 (ponderado). Run: pytest

Cubren el caso que hacía fallar consultas correctas: el modelo calcula el
porcentaje sobre un CTE ya agregado, así que la consulta de salida arrastra
columnas sueltas que no están en ningún GROUP BY externo. La fila que traen ya
es una celda agregada, no una persona; lo que sí tiene que seguir garantizado es
que la métrica publicada salga de SUM(W) y que el conteo crudo llegue a la
salida (sin él no puede aplicarse la supresión de celdas chicas).
"""

import pytest

from sql_guard_2023 import validar, SQLNoSeguro

CTE_OK = (
    "WITH t AS (SELECT DEPARTAMENTO, SUM(W) AS personas, COUNT(*) AS n_crudo "
    "FROM personas_2023 GROUP BY DEPARTAMENTO) "
    "SELECT DEPARTAMENTO, personas, n_crudo, "
    "100.0 * personas / (SELECT SUM(personas) FROM t) AS pct FROM t"
)


def test_cte_agregado_permite_columna_suelta_en_la_salida():
    sql, conteos = validar(CTE_OK)
    assert sql.upper().startswith("WITH")
    assert [c.lower() for c in conteos] == ["n_crudo"]


def test_cte_sin_conteo_crudo_no_pasa():
    with pytest.raises(SQLNoSeguro):
        validar("WITH t AS (SELECT DEPARTAMENTO, SUM(W) AS personas FROM personas_2023 "
                "GROUP BY DEPARTAMENTO) SELECT DEPARTAMENTO, personas FROM t")


def test_cte_sin_sum_w_no_pasa():
    # COUNT(*) es el conteo crudo para la supresión, nunca la cifra publicada
    with pytest.raises(SQLNoSeguro):
        validar("WITH t AS (SELECT DEPARTAMENTO, COUNT(*) AS n FROM personas_2023 "
                "GROUP BY DEPARTAMENTO) SELECT DEPARTAMENTO, n FROM t")


def test_cte_con_columna_fuera_del_group_by_no_pasa():
    with pytest.raises(SQLNoSeguro):
        validar("WITH t AS (SELECT DEPARTAMENTO, PERPE01, SUM(W) AS p, COUNT(*) AS c "
                "FROM personas_2023 GROUP BY DEPARTAMENTO) "
                "SELECT DEPARTAMENTO, PERPE01, p, c FROM t")


def test_cte_no_puede_cruzar_universos():
    with pytest.raises(SQLNoSeguro):
        validar("WITH t AS (SELECT hogar_key, SUM(W) AS p, COUNT(*) AS n FROM personas_2023 "
                "GROUP BY hogar_key) SELECT t.n, v.VIVID FROM t, viviendas_2023 v")


def test_cte_no_habilita_tablas_fuera_del_whitelist():
    with pytest.raises(SQLNoSeguro):
        validar("WITH t AS (SELECT name AS n FROM sqlite_master) "
                "SELECT n, COUNT(*) AS c FROM t GROUP BY n")


def test_group_by_ordinal():
    sql, conteos = validar("SELECT DEPARTAMENTO, SUM(W) AS personas, COUNT(*) AS n "
                           "FROM personas_2023 GROUP BY 1")
    assert [c.lower() for c in conteos] == ["n"]


def test_group_by_alias_de_salida():
    validar("SELECT CASE DEPARTAMENTO WHEN '01' THEN 'Montevideo' ELSE 'Interior' END AS zona, "
            "SUM(W) AS personas, COUNT(*) AS n FROM personas_2023 GROUP BY zona")


def test_consulta_clasica_sigue_igual():
    sql, conteos = validar("SELECT DEPARTAMENTO, SUM(W) AS personas, COUNT(*) AS n "
                           "FROM personas_2023 GROUP BY DEPARTAMENTO")
    assert "LIMIT" in sql.upper()
    assert [c.lower() for c in conteos] == ["n"]
