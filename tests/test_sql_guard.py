"""Tests for the SQL guardrail over microdata. Run with: pytest"""

import pytest

from app.sql_guard import validar, suprimir_celdas_chicas, SQLNoSeguro


def test_conteo_agregado_pasa():
    sql = validar(
        "SELECT departamento, COUNT(*) AS personas FROM personas GROUP BY departamento"
    )
    assert sql.upper().startswith("SELECT")
    assert "LIMIT" in sql.upper()  # límite agregado automáticamente


def test_filas_individuales_rechazadas():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT departamento, sexo, edad FROM personas WHERE edad = 87")


def test_select_estrella_rechazado():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT * FROM personas")


def test_delete_rechazado():
    with pytest.raises(SQLNoSeguro):
        validar("DELETE FROM personas")


def test_tabla_no_permitida_rechazada():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT COUNT(*) FROM usuarios")


def test_multiples_sentencias_rechazadas():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT COUNT(*) FROM personas; DROP TABLE personas")


def test_comentarios_rechazados():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT COUNT(*) FROM personas -- comentario")


def test_union_rechazado():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT COUNT(*) FROM personas UNION SELECT password FROM usuarios")


def test_supresion_celdas_chicas():
    filas = [
        {"departamento": "MONTEVIDEO", "personas": 500_000},
        {"departamento": "FLORES", "personas": 3},   # < 5 → suprimir
    ]
    seguras, n = suprimir_celdas_chicas(filas)
    assert n == 1
    assert len(seguras) == 1
    assert seguras[0]["departamento"] == "MONTEVIDEO"


# ---- Variables nuevas (v1: ascendencia étnico-racial y NBI) ----

def test_conteo_afro_por_departamento_pasa():
    sql = validar(
        "SELECT departamento, COUNT(*) AS personas FROM personas "
        "WHERE asc_afro='Si' GROUP BY departamento"
    )
    assert sql.upper().startswith("SELECT")


def test_hogares_distintos_por_nbi_pasa():
    sql = validar(
        "SELECT COUNT(DISTINCT hogar_key) AS hogares FROM personas WHERE nbi >= 2"
    )
    assert "COUNT(DISTINCT hogar_key)".lower() in sql.lower()


def test_hogar_key_fuera_de_count_distinct_rechazado():
    # hogar_key suelto (aunque haya un COUNT) debe rechazarse.
    with pytest.raises(SQLNoSeguro):
        validar(
            "SELECT hogar_key, COUNT(*) AS personas FROM personas GROUP BY hogar_key"
        )


def test_supresion_aplica_a_hogares():
    filas = [
        {"nbi": 3, "hogares": 40_000},
        {"nbi": 2, "hogares": 2},   # < 5 → suprimir
    ]
    seguras, n = suprimir_celdas_chicas(filas)
    assert n == 1
    assert len(seguras) == 1
    assert seguras[0]["nbi"] == 3
