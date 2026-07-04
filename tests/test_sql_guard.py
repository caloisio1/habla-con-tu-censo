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


# ---- Geografía (v3: localidades y mapas coropléticos) ----

def test_join_localidades_pasa():
    sql = validar(
        "SELECT COUNT(*) AS personas FROM personas "
        "JOIN localidades ON personas.codloc = localidades.codloc "
        "WHERE localidades.nombre = 'PASO DE LOS TOROS'"
    )
    assert "localidades" in sql.lower()


def test_group_by_codsec_con_clave_en_salida_pasa():
    validar("SELECT codsec, COUNT(*) AS personas FROM personas GROUP BY codsec")


def test_group_by_barrio_con_clave_en_salida_pasa():
    validar(
        "SELECT BARRIO85, COUNT(*) AS personas FROM personas "
        "WHERE departamento='MONTEVIDEO' GROUP BY BARRIO85"
    )


def test_group_by_ccz_con_clave_en_salida_pasa():
    validar(
        "SELECT CCZ, COUNT(DISTINCT hogar_key) AS hogares FROM personas "
        "WHERE departamento='MONTEVIDEO' GROUP BY CCZ"
    )


def test_hogar_key_en_salida_sigue_rechazado():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT hogar_key, COUNT(*) AS personas FROM personas GROUP BY hogar_key")


def test_tabla_fuera_de_whitelist_sigue_rechazada():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT COUNT(*) FROM viviendas JOIN personas ON 1=1")


def test_keyword_dentro_de_literal_no_rechaza():
    # 'BELLA UNION' / 'Union' contienen UNION como dato, no como operador.
    validar("SELECT COUNT(*) AS personas FROM personas p JOIN localidades l "
            "ON p.codloc=l.codloc WHERE l.nombre='BELLA UNION'")
    validar("SELECT COUNT(*) AS personas FROM personas "
            "WHERE BARRIO85='Union' AND departamento='MONTEVIDEO'")


def test_union_real_sigue_rechazado():
    # UNION como operador (fuera de literal) debe seguir rechazándose.
    with pytest.raises(SQLNoSeguro):
        validar("SELECT COUNT(*) FROM personas UNION SELECT clave FROM secretos")
