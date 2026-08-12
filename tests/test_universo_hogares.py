"""Tests de los tres defectos reportados por el muestrista del INE (12-ago-2026).

Son pruebas de GUARD, no de modelo: lo que se comprueba es que la corrección no
dependa de que el generador de SQL se acuerde. El origen de los tres es el mismo —se
le confiaba al prompt algo que tenía que ser estructural— y por eso los tres se
prueban acá y no en la batería, que mide al modelo.

Run: pytest tests/test_universo_hogares.py -q
"""
import os
import sys

import pytest

AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AQUI)

from sql_guard_2023 import LIMITE_MAXIMO, SQLNoSeguro, validar   # noqa: E402
from comun import universo                                       # noqa: E402

DICC = os.path.join(AQUI, "diccionario_llm_2023.json")

HOGARES_PONDERADO = (
    "SELECT ROUND(SUM(w)) AS hogares, COUNT(*) AS n_crudo FROM "
    "(SELECT hogar_key, MAX(W) AS w FROM personas_2023 WHERE hogar_key IS NOT NULL "
    "GROUP BY hogar_key)")


# ---------------------------------------------------------------- 1. hogares

def test_hogares_sin_ponderar_se_rechaza():
    """COUNT(DISTINCT hogar_key) como cifra publicada: 1.255.062 en vez de 1.376.921."""
    with pytest.raises(SQLNoSeguro) as e:
        validar("SELECT COUNT(DISTINCT hogar_key) AS hogares, COUNT(*) AS n_crudo "
                "FROM personas_2023 WHERE hogar_key IS NOT NULL")
    assert "ponderar" in str(e.value).lower()


def test_hogares_ponderados_pasan():
    sql, conteos = validar(HOGARES_PONDERADO)
    assert "sum(w)" in sql.lower()
    assert conteos, "sin columna de conteo no hay supresión"


def test_hogares_ponderados_con_desglose_pasan():
    """El corte va DENTRO de la subconsulta: así el desglose también sale ponderado."""
    sql, _ = validar(
        "SELECT d.codigo AS geo_codigo, d.nombre AS geo_nombre, ROUND(SUM(h.w)) AS hogares, "
        "COUNT(*) AS n_crudo FROM (SELECT DEPARTAMENTO, hogar_key, MAX(W) AS w "
        "FROM personas_2023 WHERE hogar_key IS NOT NULL GROUP BY DEPARTAMENTO, hogar_key) h "
        "JOIN departamentos_2023 d ON h.DEPARTAMENTO = d.codigo GROUP BY d.codigo, d.nombre")
    assert "geo_codigo" in sql


def test_count_distinct_hogar_sigue_valiendo_como_control():
    """Prohibido como CIFRA, permitido como conteo crudo junto a la métrica ponderada."""
    sql, conteos = validar(
        "SELECT ROUND(SUM(W)) AS personas, COUNT(DISTINCT hogar_key) AS hogares_crudo, "
        "COUNT(*) AS n_crudo FROM personas_2023")
    assert "count(distinct" in sql.lower()


# --------------------------------------------------- 2. tope de filas / mapa

def test_el_tope_cubre_el_desglose_mas_grande():
    """4.297 segmentos censales con población: con 300 se perdían 3.997 en silencio."""
    assert LIMITE_MAXIMO >= 4297


def test_limite_grande_se_acepta():
    sql, _ = validar("SELECT DEPARTAMENTO || SECCION || SEGMENTO AS geo_codigo, "
                     "ROUND(SUM(W)) AS personas, COUNT(*) AS n_crudo FROM personas_2023 "
                     "GROUP BY DEPARTAMENTO, SECCION, SEGMENTO LIMIT 5000")
    assert "5000" in sql


def test_limite_por_encima_del_maximo_se_rechaza():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT ROUND(SUM(W)) AS personas, COUNT(*) AS n_crudo "
                "FROM personas_2023 LIMIT 999999")


# ------------------------------------------------------- 3. fuera de universo

def test_la_tabla_de_universo_sale_del_diccionario():
    """Derivada, no escrita a mano: si mañana aparece otra variable con piso de edad,
    entra sola."""
    fuera = universo.tabla(DICC)
    assert set(fuera) == {"niveledu25mas", "pobpcoac", "disc_tiene", "dificultad"}
    assert fuera["niveledu25mas"]["codigos"] == ["0"]
    assert fuera["pobpcoac"]["codigos"] == ["1"]


def test_perdido_no_se_confunde_con_fuera_de_universo():
    fuera = universo.tabla(DICC)
    for info in fuera.values():
        assert not ({"7777", "8888", "9898", "9999"} & set(info["codigos"]))


@pytest.mark.parametrize("var,codigo", [("NIVELEDU25MAS", "'0'"), ("POBPCOAC", "'1'"),
                                        ("DISC_TIENE", "'2'"), ("DIFICULTAD", "'0'")])
def test_se_inyecta_la_exclusion(var, codigo):
    """La corrección no puede depender del modelo: medido el 12-ago, la misma pregunta
    excluía el código en una corrida y no en la siguiente."""
    sql, _ = validar(f"SELECT ROUND(100.0*SUM(CASE WHEN {var} = 9 THEN W ELSE 0 END)/SUM(W),2) "
                     f"AS pct, COUNT(*) AS n_crudo FROM personas_2023")
    assert f"NOT {var} IN ({codigo})" in sql or f"{var} NOT IN ({codigo})" in sql


def test_la_exclusion_llega_al_cte():
    """El ámbito que lee microdatos puede ser un CTE; el filtro tiene que entrar ahí."""
    sql, _ = validar("WITH t AS (SELECT NIVELEDU25MAS AS nivel, SUM(W) AS p, COUNT(*) AS n "
                     "FROM personas_2023 GROUP BY NIVELEDU25MAS) "
                     "SELECT nivel, p, n FROM t")
    assert "NIVELEDU25MAS" in sql and "'0'" in sql


def test_no_se_inyecta_si_la_pregunta_apunta_a_esa_categoria():
    """`WHERE NIVELEDU25MAS='0'` es una pregunta deliberada por los menores de 25:
    inyectar la exclusión la convertiría en una contradicción que devuelve 0."""
    sql, _ = validar("SELECT ROUND(SUM(W)) AS personas, COUNT(*) AS n_crudo "
                     "FROM personas_2023 WHERE NIVELEDU25MAS = '0'")
    assert "NOT NIVELEDU25MAS IN" not in sql and "NIVELEDU25MAS NOT IN" not in sql


def test_no_se_toca_una_consulta_que_no_usa_esas_variables():
    sql, _ = validar("SELECT ROUND(SUM(W)) AS personas, COUNT(*) AS n_crudo FROM personas_2023")
    for var in ("NIVELEDU25MAS", "POBPCOAC", "DISC_TIENE", "DIFICULTAD"):
        assert var not in sql


# ------------------------------------------ 4. las tres tasas del mercado de trabajo

def _tasa(codigos):
    return validar("SELECT ROUND(100.0*SUM(CASE WHEN POBPCOAC %s THEN W ELSE 0 END)/SUM(W),2) "
                   "AS pct, COUNT(*) AS n_crudo FROM personas_2023" % codigos)[0]


def test_la_desocupacion_va_sobre_la_pea():
    """Decisión de Carlos (12-ago): 'el porcentaje de desocupados es idéntico a la tasa
    de desocupación, por eso siempre es sobre la PEA'. Antes daba 5,84 % o 9,35 % según
    la corrida."""
    assert "POBPCOAC IN ('2', '3')" in _tasa("= 3")


def test_actividad_y_empleo_van_sobre_la_pet():
    """PET = 14 y más. Son otro denominador, no el mismo de la desocupación."""
    for codigos in ("IN (2, 3)", "= 2"):
        sql = _tasa(codigos)
        assert "PERNA01 >= 14" in sql
        assert "POBPCOAC IN ('2', '3')" not in sql


def test_la_pet_no_pierde_a_quien_no_contesto_actividad():
    """La PET la define la EDAD. Sacar del denominador a los que no contestaron condición
    de actividad sube la tasa de 62,82 % a 64,43 %."""
    sql = _tasa("IN (2, 3)")
    assert "POBPCOAC NOT IN" not in sql and "NOT POBPCOAC IN" not in sql


def test_la_tasa_ya_bien_escrita_no_se_toca():
    sql, _ = validar(
        "SELECT ROUND(100.0*SUM(CASE WHEN POBPCOAC = 3 THEN W ELSE 0 END)/"
        "SUM(CASE WHEN POBPCOAC IN (2,3) THEN W ELSE 0 END),2) AS pct, "
        "COUNT(*) AS n_crudo FROM personas_2023")
    assert "POBPCOAC IN ('2', '3')" not in sql


def test_el_desglose_por_condicion_conserva_a_los_inactivos():
    """Restringir acá borraría a los inactivos, que SON la respuesta."""
    sql, _ = validar("SELECT POBPCOAC, ROUND(SUM(W)) AS personas, COUNT(*) AS n_crudo "
                     "FROM personas_2023 GROUP BY POBPCOAC")
    assert "POBPCOAC IN ('2', '3')" not in sql and "PERNA01 >= 14" not in sql


def test_la_frase_de_universo_nombra_la_categoria_excluida():
    fuera = universo.tabla(DICC)
    frase = universo.frase_universo(universo.presentes_en(
        "SELECT NIVELEDU25MAS FROM personas_2023", fuera), fuera)
    assert "Menor de 25" in frase
