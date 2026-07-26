"""Tests del guard de los motores 1996 y 2004. Run: pytest

El caso que motivó la mayoría de estos tests: `_tablas_por_ambito` leía
`args["from"]`, pero esta versión de sqlglot usa `args["from_"]`. El resultado
era un conjunto vacío por ámbito, y con él la regla de universos distintos
(prohibido vincular personas_1996 con viviendas_1996) dejaba pasar los JOIN EN
SILENCIO. Un control que no falla cuando debe es peor que no tenerlo, así que
acá se prueban las dos direcciones: lo que tiene que pasar y lo que tiene que
rechazarse.
"""
import pytest

from sql_guard_historicos import (GUARD_1996, GUARD_2004, SQLNoSeguro,
                                  suprimir_celdas_chicas, UMBRAL_SUPRESION)


def _ok(guard, sql):
    seguro, conteos = guard.validar(sql)
    assert seguro.upper().startswith("SELECT")
    return seguro, conteos


def _rechaza(guard, sql):
    with pytest.raises(SQLNoSeguro):
        guard.validar(sql)


# --- consultas legítimas ---------------------------------------------------

def test_personas_por_sexo():
    _ok(GUARD_1996, "SELECT sexo, COUNT(*) AS personas, COUNT(*) AS n_crudo "
                    "FROM personas_1996 GROUP BY sexo")


def test_hogares_por_clave_materializada():
    _ok(GUARD_1996, "SELECT COUNT(DISTINCT hogar_key) AS hogares, COUNT(*) AS n_crudo "
                    "FROM personas_1996")


def test_join_al_nomenclator_es_valido():
    _ok(GUARD_1996, "SELECT d.nombre, COUNT(*) AS personas, COUNT(*) AS n_crudo "
                    "FROM personas_1996 p JOIN cod_departamentos d ON p.dpto = d.dpto "
                    "GROUP BY d.nombre")


def test_dos_subconsultas_independientes_se_permiten():
    """No es una mezcla de universos: son dos conteos lado a lado."""
    _ok(GUARD_1996,
        "SELECT (SELECT COUNT(DISTINCT hogar_key) FROM personas_1996) AS hogares, "
        "(SELECT COUNT(*) FROM viviendas_1996 WHERE condocup <> '1') AS desocupadas, "
        "(SELECT COUNT(*) FROM viviendas_1996) AS n_crudo")


def test_2004_filtra_por_bandera_de_registro():
    _ok(GUARD_2004, "SELECT nom_dpto, COUNT(*) AS personas, COUNT(*) AS n_crudo "
                    "FROM censo2004 WHERE per = 1 GROUP BY nom_dpto")


def test_identificador_dentro_de_count_distinct():
    _ok(GUARD_2004, "SELECT COUNT(DISTINCT id_viv) AS viviendas, COUNT(*) AS n_crudo "
                    "FROM censo2004 WHERE viv = 1")


# --- universos distintos: las tres formas de vincular ----------------------

def test_join_explicito_entre_tablas_de_hechos():
    _rechaza(GUARD_1996,
             "SELECT COUNT(*) AS n_crudo FROM personas_1996 p "
             "JOIN viviendas_1996 v ON p.vivienda_key = v.vivienda_key")


def test_join_implicito_por_coma():
    _rechaza(GUARD_1996, "SELECT COUNT(*) AS n_crudo FROM personas_1996, viviendas_1996")


def test_mezcla_dentro_de_subconsulta():
    _rechaza(GUARD_1996,
             "SELECT COUNT(*) AS n_crudo FROM personas_1996 p WHERE p.vivienda_key IN "
             "(SELECT v.vivienda_key FROM viviendas_1996 v "
             "JOIN personas_1996 p2 ON p2.vivienda_key = v.vivienda_key)")


# --- microdatos sin agregar ------------------------------------------------

def test_select_estrella():
    _rechaza(GUARD_1996, "SELECT * FROM personas_1996")


def test_filas_individuales():
    _rechaza(GUARD_1996, "SELECT edad, sexo FROM personas_1996 LIMIT 10")


def test_sin_count_no_hay_supresion_posible():
    _rechaza(GUARD_1996, "SELECT sexo, MAX(edad) AS m FROM personas_1996 GROUP BY sexo")


def test_identificador_crudo_en_proyeccion():
    _rechaza(GUARD_1996, "SELECT hogar_key, COUNT(*) AS n_crudo FROM personas_1996 "
                         "GROUP BY hogar_key")
    _rechaza(GUARD_2004, "SELECT id_viv, COUNT(*) AS n_crudo FROM censo2004 GROUP BY id_viv")


# --- superficie de ataque --------------------------------------------------

def test_tabla_de_metadatos_no_es_consultable():
    _rechaza(GUARD_1996, "SELECT COUNT(*) AS n_crudo FROM dominios_observados")


def test_columna_inexistente():
    _rechaza(GUARD_1996, "SELECT ingresos, COUNT(*) AS n_crudo FROM personas_1996 "
                         "GROUP BY ingresos")


def test_sentencia_que_no_es_select():
    _rechaza(GUARD_1996, "DELETE FROM personas_1996")


def test_dos_sentencias():
    _rechaza(GUARD_1996, "SELECT COUNT(*) AS n_crudo FROM personas_1996; "
                         "SELECT COUNT(*) AS n_crudo FROM viviendas_1996")


def test_limite_excesivo():
    _rechaza(GUARD_1996, "SELECT sexo, COUNT(*) AS n_crudo FROM personas_1996 "
                         "GROUP BY sexo LIMIT 100000")


def test_se_inyecta_limite_si_falta():
    seguro, _ = GUARD_1996.validar(
        "SELECT sexo, COUNT(*) AS n_crudo FROM personas_1996 GROUP BY sexo")
    assert "LIMIT" in seguro.upper()


# --- supresión -------------------------------------------------------------

def test_supresion_descarta_la_fila_entera():
    filas = [{"sexo": "1", "personas": 100, "n_crudo": 100},
             {"sexo": "2", "personas": 3, "n_crudo": 3}]
    seguras, suprimidas = suprimir_celdas_chicas(filas, ["personas", "n_crudo"])
    assert suprimidas == 1
    assert len(seguras) == 1
    assert all(f["n_crudo"] >= UMBRAL_SUPRESION for f in seguras)
