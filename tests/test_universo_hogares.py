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
from comun import indicadores, nivel_universitario, universo     # noqa: E402

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
    """PET = 12 y más (piso fijado por Carlos el 13-ago: 'Menor de 12 años' es 11 o
    menos, así que el universo relevado empieza en los 12). Son otro denominador, no el
    mismo de la desocupación."""
    for codigos in ("IN (2, 3)", "= 2"):
        sql = _tasa(codigos)
        assert "PERNA01 >= 12" in sql
        assert "PERNA01 >= 14" not in sql
        assert "POBPCOAC IN ('2', '3')" not in sql


def test_la_pet_no_pierde_a_quien_no_contesto_actividad():
    """La PET la define la EDAD. Sacar del denominador a los que no contestaron condición
    de actividad sube la tasa de 60,85 % a 62,46 %."""
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
    assert "POBPCOAC IN ('2', '3')" not in sql and "PERNA01 >= 12" not in sql


def test_la_frase_de_universo_nombra_la_categoria_excluida():
    fuera = universo.tabla(DICC)
    frase = universo.frase_universo(universo.presentes_en(
        "SELECT NIVELEDU25MAS FROM personas_2023", fuera), fuera)
    assert "Menor de 25" in frase


# ------------------------------- 5. "universitario": lo dice la propia pregunta
#
# Hasta el 15-ago-2026 acá se preguntaba con un chip ante cualquier mención de la
# universidad. Decisión de Carlos ese día: no hace falta: "universidad" es
# universidad, y "universidad o más" incluye el posgrado. La regla vive ahora en
# comun/nivel_universitario.py y ya no pasa por indicadores.

@pytest.mark.parametrize("pregunta", [
    "¿Qué porcentaje de la población tiene nivel universitario?",
    "¿Cuántas personas tienen educación universitaria?",
    "¿Cuántos universitarios hay en Salto?",
    "¿Cuántas personas terminaron la universidad?",
])
def test_universidad_sola_es_solo_universidad(pregunta):
    """13,61 % en 2023. El posgrado es una categoría APARTE del máximo nivel."""
    assert nivel_universitario.detectar(pregunta, "2023") == nivel_universitario.SOLO
    assert "NO incluyas" in nivel_universitario.instruccion(pregunta, "2023")


@pytest.mark.parametrize("pregunta", [
    "¿Cuántas personas tienen universidad o más?",
    "¿Qué porcentaje tiene nivel universitario o superior?",
    "¿Cuántos tienen nivel universitario o mayor?",
    "¿Cuántos tienen al menos nivel universitario?",
    "¿Cuántos alcanzaron universidad o posgrado?",
])
def test_universidad_o_mas_incluye_posgrado(pregunta):
    """16,48 % en 2023."""
    assert nivel_universitario.detectar(pregunta, "2023") == nivel_universitario.CON_POSGRADO
    assert "IN ('9','10')" in nivel_universitario.instruccion(pregunta, "2023")


def test_lo_que_se_conto_se_declara_siempre():
    """En los DOS casos, no solo cuando se agrega el posgrado: una cifra de
    universitarios que excluye a los doctorados es igual de sorprendente que una que
    los incluye, y quien la lea tiene que poder auditarla."""
    for pregunta in ("nivel universitario", "nivel universitario o más"):
        assert nivel_universitario.declaraciones(pregunta, "2023")


def test_universitario_ya_no_pasa_por_el_chip():
    """El chip interrumpía cada vez que alguien nombraba la universidad."""
    assert indicadores.desambiguar("¿Cuántos universitarios hay en Salto?", "2023") is None


def test_sin_universidad_no_aplica():
    for pregunta in ("¿Cuántas personas hay en Salto?", "¿Cuántos tienen secundaria?"):
        assert nivel_universitario.detectar(pregunta, "2023") is None


def test_en_1996_no_hay_distincion_que_hacer():
    """`nivel` tiene UNA sola categoría universitaria (6 = Universidad): no hay nada
    que decidir, y decir 'no relevado' sería falso."""
    assert nivel_universitario.detectar("¿Cuántos universitarios hay?", "1996") is None
    assert indicadores.desambiguar("¿Cuántos universitarios hay?", "1996") is None


def test_en_2004_no_se_relevo_educacion():
    assert nivel_universitario.detectar("¿Cuántos universitarios hay?", "2004") is None


# ------------------------------------- 6. la geografía NO tiene códigos centinela

_GEO_BASE = ("SELECT DEPARTAMENTO || SECCION || SEGMENTO AS geo_codigo, "
             "ROUND(SUM(W)) AS personas, COUNT(*) AS n_crudo FROM personas_2023 "
             "WHERE %s GROUP BY DEPARTAMENTO, SECCION, SEGMENTO")


def test_no_se_descarta_la_seccion_99_de_montevideo():
    """Es una sección real con 75.855 personas ponderadas (2,17 % del país) y polígono
    propio. La perdían las dos formas del filtro."""
    for lit in ("'99'", "99"):
        sql, _ = validar(_GEO_BASE % ("SECCION NOT IN (7777, 8888, 9898, 9999, %s)" % lit))
        assert "99" not in sql.split("WHERE")[1].split("GROUP")[0]


def test_el_filtro_numerico_no_se_lleva_el_segmento_099():
    """`SEGMENTO NOT IN (..., 99)` sin comillas hace que DuckDB castee '099' a 99 y lo
    saque: 7.825 personas. Con comillas no pasaba. La misma divergencia muda entre texto
    y número de siempre."""
    sql, _ = validar(_GEO_BASE % "SEGMENTO NOT IN (7777, 8888, 9898, 9999, 99)")
    assert "99" not in sql.split("WHERE")[1].split("GROUP")[0]


def test_se_limpia_tambien_el_distinto_de():
    sql, _ = validar(_GEO_BASE % "SECCION <> '99'")
    assert "'99'" not in sql


def test_preguntar_POR_la_seccion_99_sigue_funcionando():
    """Sólo se limpian los contextos negativos: un `SECCION = '99'` es alguien
    preguntando por esa sección, y borrarlo sería el mismo error al revés."""
    sql, _ = validar("SELECT ROUND(SUM(W)) AS personas, COUNT(*) AS n_crudo "
                     "FROM personas_2023 WHERE DEPARTAMENTO = '01' AND SECCION = '99'")
    assert "SECCION = '99'" in sql


def test_no_se_toca_la_exclusion_de_nulos_en_geografia():
    """NULL sí es inválido en la geografía: 1.868 registros sin sección ni segmento."""
    sql, _ = validar(_GEO_BASE % "SECCION IS NOT NULL AND SEGMENTO IS NOT NULL")
    assert "IS NULL" in sql.upper()


def test_los_perdidos_de_una_variable_normal_no_se_tocan():
    sql, _ = validar("SELECT ROUND(SUM(W)) AS p, COUNT(*) AS n FROM personas_2023 "
                     "WHERE PERPA01 NOT IN ('7777', '8888', '9898', '9999')")
    assert "7777" in sql


# --------------------- 7. ASISTENCIA: sólo 1 y 2 son valores aceptables

def test_asistencia_se_acota_a_los_valores_validos():
    """Regla de Carlos (12-ago): asiste a un establecimiento educativo, y sólo 1 y 2 son
    respuestas. Acá el fuera de universo viene en NULL y no en un código —a los 118.249
    chicos de 0 a 3 años no se les preguntó—, así que la exclusión de `universo.tabla`
    no lo veía y quedaba a cargo del prompt."""
    sql, _ = validar("SELECT ROUND(100.0*SUM(CASE WHEN ASISTENCIA = 1 THEN W ELSE 0 END)"
                     "/SUM(W),2) AS pct, COUNT(*) AS n_crudo FROM personas_2023")
    assert "ASISTENCIA IN ('1', '2')" in sql


def test_los_valores_validos_salen_del_diccionario():
    """Aceptable = está en value_labels y no es un perdido. Sin listas escritas a mano."""
    v = universo.valores_validos(DICC, ("ASISTENCIA",))
    assert v["asistencia"]["codigos"] == ["1", "2"]


def test_la_restriccion_llega_aunque_la_pregunta_traiga_su_propio_filtro():
    sql, _ = validar("SELECT ROUND(100.0*SUM(CASE WHEN ASISTENCIA = 1 THEN W ELSE 0 END)"
                     "/SUM(W),2) AS pct, COUNT(*) AS n_crudo FROM personas_2023 "
                     "WHERE PERNA01 < 5")
    assert "ASISTENCIA IN ('1', '2')" in sql


def test_una_consulta_sin_asistencia_no_se_toca():
    sql, _ = validar("SELECT ROUND(SUM(W)) AS personas, COUNT(*) AS n_crudo FROM personas_2023")
    assert "ASISTENCIA" not in sql
