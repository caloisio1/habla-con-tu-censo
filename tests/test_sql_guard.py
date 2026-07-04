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
