"""Tests para construir_mapa (armado del payload de mapa coroplético). Run: pytest"""

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from app.main import construir_mapa


def test_mapa_por_departamento():
    sql = "SELECT departamento, COUNT(*) AS personas FROM personas GROUP BY departamento"
    filas = [{"departamento": "MONTEVIDEO", "personas": 1319108},
             {"departamento": "RIVERA", "personas": 103493}]
    m = construir_mapa(sql, filas)
    assert m["nivel"] == "departamento"
    assert m["datos"][0] == {"clave": "MONTEVIDEO", "valor": 1319108}


def test_mapa_por_seccion_usa_codsec():
    sql = "SELECT codsec, COUNT(*) AS personas FROM personas GROUP BY codsec"
    filas = [{"codsec": 101, "personas": 5000}]
    m = construir_mapa(sql, filas)
    assert m["nivel"] == "seccion"
    assert m["datos"][0]["clave"] == 101


def test_mapa_barrio_ignora_where_departamento():
    # 'departamento' aparece en el WHERE pero el GROUP BY es por barrio.
    sql = ("SELECT BARRIO85, COUNT(*) AS personas FROM personas "
           "WHERE departamento='MONTEVIDEO' GROUP BY BARRIO85")
    filas = [{"BARRIO85": "Casavalle", "personas": 30000}]
    m = construir_mapa(sql, filas)
    assert m["nivel"] == "barrio"


def test_mapa_valor_es_porcentaje_si_no_hay_conteo():
    sql = ("SELECT departamento, 100.0*SUM(CASE WHEN asc_afro='Si' THEN 1 END)"
           "/COUNT(*) AS porcentaje FROM personas WHERE asc_afro IS NOT NULL "
           "GROUP BY departamento")
    filas = [{"departamento": "RIVERA", "porcentaje": 17.1}]
    m = construir_mapa(sql, filas)
    assert m["datos"][0] == {"clave": "RIVERA", "valor": 17.1}


def test_sin_group_by_geografico_no_hay_mapa():
    sql = "SELECT sexo, COUNT(*) AS personas FROM personas GROUP BY sexo"
    assert construir_mapa(sql, [{"sexo": "Mujeres", "personas": 1}]) is None
