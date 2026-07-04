"""Tests para normalizar_departamentos (bug de tildes). Run: pytest"""

import os

# main.py instancia OpenAI() al importar (requiere OPENAI_API_KEY en el entorno);
# usamos una dummy solo si no hay una real, para importar la funcion pura sin red.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from app.main import normalizar_departamentos


def test_normaliza_paysandu():
    sql = "SELECT COUNT(*) AS personas FROM personas WHERE departamento = 'PAYSANDÚ'"
    esperado = "SELECT COUNT(*) AS personas FROM personas WHERE departamento = 'PAYSANDU'"
    assert normalizar_departamentos(sql) == esperado


def test_normaliza_otros_departamentos():
    assert "RIO NEGRO" in normalizar_departamentos("WHERE departamento = 'RÍO NEGRO'")
    assert "SAN JOSE" in normalizar_departamentos("WHERE departamento = 'SAN JOSÉ'")
    assert "TACUAREMBO" in normalizar_departamentos("WHERE departamento = 'TACUAREMBÓ'")


def test_normaliza_minusculas():
    assert normalizar_departamentos("paysandú") == "paysandu"


def test_no_altera_sql_sin_tildes():
    sql = "SELECT COUNT(*) AS personas FROM personas WHERE departamento = 'SALTO' LIMIT 200"
    assert normalizar_departamentos(sql) == sql
