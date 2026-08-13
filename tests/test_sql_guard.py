"""Tests del guardrail sobre microdatos (v4: parser real sqlglot). Run: pytest

Cambios respecto de v3 (justificados):
- validar() devuelve (sql_seguro, columnas_conteo) en vez de solo el sql: la
  supresión ahora es estructural y necesita saber qué columnas son conteos.
- suprimir_celdas_chicas(filas, columnas_conteo): recibe explícitamente las
  columnas de conteo identificadas por el guard (ya no las adivina por nombre).
- Los comentarios ya no se rechazan por texto: el parser los descarta y se
  ejecuta el SQL canónico re-serializado desde el árbol (sin comentarios).
"""

import pytest

from app.sql_guard import validar, suprimir_celdas_chicas, SQLNoSeguro


def _ok(sql):
    s, conteos = validar(sql)
    assert s.upper().startswith("SELECT")
    return s, conteos


# ---------- Núcleo v3 (debe seguir intacto) ----------

def test_conteo_agregado_pasa():
    sql, _ = _ok("SELECT departamento, COUNT(*) AS personas FROM personas GROUP BY departamento")
    assert "LIMIT" in sql.upper()


def test_filas_individuales_rechazadas():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT departamento, sexo, edad FROM personas WHERE edad = 87")


def test_select_estrella_rechazado():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT * FROM personas")


def test_delete_rechazado():
    with pytest.raises(SQLNoSeguro):
        validar("DELETE FROM personas")


def test_pragma_rechazado():
    with pytest.raises(SQLNoSeguro):
        validar("PRAGMA table_info(personas)")


def test_tabla_no_permitida_rechazada():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT COUNT(*) FROM usuarios")


def test_multiples_sentencias_rechazadas():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT COUNT(*) FROM personas; DROP TABLE personas")


def test_comentario_se_descarta_y_es_seguro():
    # Con parser real el comentario se ignora; el SQL devuelto no lo contiene.
    sql, _ = validar("SELECT COUNT(*) AS personas FROM personas -- comentario")
    assert "--" not in sql
    assert "comentario" not in sql


def test_limite_excesivo_rechazado():
    """El resguardo es 50.000 (Carlos, 13-ago: "no debería haber tope"). Un LIMIT por
    encima de eso sigue rechazándose; 5.000 ya NO, porque con el tope viejo de 300 un
    desglose por segmento censal —4.268 en 2011— salía recortado al 7 %."""
    with pytest.raises(SQLNoSeguro):
        validar("SELECT codsec, COUNT(*) AS personas FROM personas GROUP BY codsec LIMIT 100000")


def test_desglose_por_segmento_no_se_recorta():
    """La tercera observación del muestrista del INE: 4.268 segmentos, y devolvía 300."""
    sql, _ = validar("SELECT DPTO, SECC, SEGM, COUNT(*) AS personas FROM personas "
                     "GROUP BY DPTO, SECC, SEGM")
    assert "LIMIT 50000" in sql.upper()
    assert "LIMIT 300" not in sql.upper()


def test_el_top_n_del_modelo_se_respeta():
    """Un LIMIT chico puede ser intencional ('los 10 departamentos con más población')."""
    sql, _ = validar("SELECT departamento, COUNT(*) AS personas FROM personas "
                     "GROUP BY departamento ORDER BY 2 DESC LIMIT 10")
    assert "LIMIT 10" in sql.upper()


# ---------- Ascendencia / NBI (v1) ----------

def test_conteo_afro_por_departamento_pasa():
    _ok("SELECT departamento, COUNT(*) AS personas FROM personas "
        "WHERE asc_afro='Si' GROUP BY departamento")


def test_hogares_distintos_por_nbi_pasa():
    sql, conteos = _ok("SELECT COUNT(DISTINCT hogar_key) AS hogares FROM personas WHERE nbi >= 2")
    assert "count(distinct hogar_key)" in sql.lower()
    assert conteos == ["hogares"]


def test_hogar_key_fuera_de_count_distinct_rechazado():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT hogar_key, COUNT(*) AS personas FROM personas GROUP BY hogar_key")


# ---------- Geografía (v3) ----------

def test_join_localidades_pasa():
    sql, _ = _ok("SELECT COUNT(*) AS personas FROM personas "
                 "JOIN localidades ON personas.codloc = localidades.codloc "
                 "WHERE localidades.nombre = 'PASO DE LOS TOROS'")
    assert "localidades" in sql.lower()


def test_join_por_columna_distinta_de_codloc_rechazado():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT COUNT(*) AS personas FROM personas "
                "JOIN localidades ON personas.departamento = localidades.departamento")


def test_group_by_codsec_pasa():
    _ok("SELECT codsec, COUNT(*) AS personas FROM personas GROUP BY codsec")


def test_group_by_barrio_pasa():
    _ok("SELECT BARRIO85, COUNT(*) AS personas FROM personas "
        "WHERE departamento='MONTEVIDEO' GROUP BY BARRIO85")


def test_group_by_ccz_pasa():
    _ok("SELECT CCZ, COUNT(DISTINCT hogar_key) AS hogares FROM personas "
        "WHERE departamento='MONTEVIDEO' GROUP BY CCZ")


def test_porcentaje_sin_columna_de_conteo_pasa_sin_supresion():
    # Regresión "% afro por barrio": el COUNT(*) va dentro de la expresión de %,
    # no como columna suelta -> no hay columnas de conteo -> sin supresión.
    sql, conteos = _ok(
        "SELECT BARRIO85, 100.0*SUM(CASE WHEN asc_afro='Si' THEN 1 ELSE 0 END)/COUNT(*) "
        "AS porcentaje_afro FROM personas WHERE departamento='MONTEVIDEO' "
        "AND asc_afro IS NOT NULL AND BARRIO85 IS NOT NULL GROUP BY BARRIO85")
    assert conteos == []


def test_orden_por_alias_de_conteo_pasa():
    # ORDER BY sobre un alias de salida (no una columna real) no debe rechazarse.
    _ok("SELECT departamento, COUNT(*) AS personas FROM personas "
        "GROUP BY departamento ORDER BY personas DESC")


# ---------- Literales vs operador UNION ----------

def test_keyword_dentro_de_literal_no_rechaza():
    _ok("SELECT COUNT(*) AS personas FROM personas p JOIN localidades l "
        "ON p.codloc=l.codloc WHERE l.nombre='BELLA UNION'")
    _ok("SELECT COUNT(*) AS personas FROM personas "
        "WHERE BARRIO85='Union' AND departamento='MONTEVIDEO'")


def test_union_operador_rechazado():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT COUNT(*) FROM personas UNION SELECT clave FROM secretos")


# ---------- Nuevas variables v4 (145 crudas) ----------

def test_columna_cruda_del_diccionario_pasa():
    _ok("SELECT COUNT(DISTINCT vivienda_key) AS viviendas FROM personas WHERE VIVVO03=3")


def test_columna_fuera_del_diccionario_rechazada():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT COUNT(*) AS personas FROM personas WHERE COLUMNA_INVENTADA=1")


def test_count_where_perid_1_pasa():
    _ok("SELECT COUNT(*) AS hogares FROM personas WHERE PERID=1")


def test_jefas_mujeres_pasa():
    _ok("SELECT COUNT(*) AS personas FROM personas "
        "WHERE departamento='RIVERA' AND PERPA01=1 AND PERPH02=2")


# ---------- Jerárquicas (hogar_key en subconsulta) ----------

def test_subconsulta_jerarquica_hogar_key_en_where_pasa():
    sql, conteos = _ok(
        "SELECT COUNT(*) AS personas FROM personas WHERE hogar_key IN "
        "(SELECT hogar_key FROM personas WHERE asc_afro='Si')")
    assert conteos == ["personas"]


def test_subconsulta_having_count_distinct_pasa():
    _ok("SELECT COUNT(DISTINCT hogar_key) AS hogares FROM personas "
        "WHERE departamento='CANELONES' AND hogar_key IN "
        "(SELECT hogar_key FROM personas GROUP BY hogar_key HAVING COUNT(*) >= 5)")


def test_vivienda_key_en_proyeccion_externa_rechazada():
    with pytest.raises(SQLNoSeguro):
        validar("SELECT vivienda_key, COUNT(*) AS personas FROM personas GROUP BY vivienda_key")


# ---------- Supresión estructural ----------

def test_supresion_con_alias_personas():
    filas = [
        {"departamento": "MONTEVIDEO", "personas": 500_000},
        {"departamento": "FLORES", "personas": 3},
    ]
    seguras, n = suprimir_celdas_chicas(filas, ["personas"])
    assert n == 1 and len(seguras) == 1 and seguras[0]["departamento"] == "MONTEVIDEO"


def test_supresion_con_alias_arbitrario():
    # El alias del conteo puede ser cualquiera; la supresión usa la lista del guard.
    filas = [{"x": "A", "cuantos": 10}, {"x": "B", "cuantos": 2}]
    seguras, n = suprimir_celdas_chicas(filas, ["cuantos"])
    assert n == 1 and seguras == [{"x": "A", "cuantos": 10}]


def test_supresion_ignora_columnas_no_conteo():
    # Un porcentaje (float) no es conteo: no dispara supresión aunque sea chico.
    filas = [{"barrio": "X", "porcentaje_afro": 1.2}]
    seguras, n = suprimir_celdas_chicas(filas, [])
    assert n == 0 and seguras == filas


def test_fail_closed_sin_ningun_conteo_rechazado():
    # Sin COUNT no hay forma de controlar divulgación -> rechazar (fail-closed).
    with pytest.raises(SQLNoSeguro):
        validar("SELECT departamento, AVG(edad) AS edad_media FROM personas GROUP BY departamento")


# --- El porcentaje calculado sobre un CTE ya agregado -----------------------
# El modelo cuenta en un CTE y la consulta de salida solo arrastra la celda para
# sacar el porcentaje: la columna del corte no está en ningún GROUP BY externo
# porque la fila ya es una celda, no una persona. Antes se rechazaba.

def test_cte_agregado_permite_columna_suelta_en_la_salida():
    _sql, conteos = validar(
        "WITH t AS (SELECT departamento, COUNT(*) AS personas FROM personas GROUP BY departamento) "
        "SELECT departamento, personas, 100.0 * personas / (SELECT SUM(personas) FROM t) AS pct "
        "FROM t"
    )
    assert [c.lower() for c in conteos] == ["personas"]  # la supresión sigue teniendo con qué


def test_cte_sin_agregar_no_pasa():
    with pytest.raises(SQLNoSeguro):
        validar("WITH t AS (SELECT departamento, edad FROM personas) "
                "SELECT departamento, edad, COUNT(*) AS n FROM t")


def test_cte_con_columna_fuera_del_group_by_no_pasa():
    with pytest.raises(SQLNoSeguro):
        validar("WITH t AS (SELECT departamento, edad, COUNT(*) AS c FROM personas "
                "GROUP BY departamento) SELECT departamento, edad, c FROM t")


def test_cte_agregado_sin_conteo_en_la_salida_no_pasa():
    with pytest.raises(SQLNoSeguro):
        validar("WITH t AS (SELECT departamento, COUNT(*) AS c FROM personas GROUP BY departamento) "
                "SELECT departamento, 100.0 * c / (SELECT SUM(c) FROM t) AS pct FROM t")


def test_cte_no_habilita_tablas_fuera_del_whitelist():
    with pytest.raises(SQLNoSeguro):
        validar("WITH t AS (SELECT a FROM sqlite_master) SELECT a, COUNT(*) AS n FROM t GROUP BY a")


def test_hogar_key_sigue_prohibido_en_la_salida_via_cte():
    with pytest.raises(SQLNoSeguro):
        validar("WITH t AS (SELECT hogar_key, COUNT(*) AS c FROM personas GROUP BY hogar_key) "
                "SELECT hogar_key, c FROM t")


# --- GROUP BY por ordinal o por alias de salida -----------------------------
# Son SQL válido y el modelo alterna entre las tres formas; compararlas
# literalmente contra la proyección hacía aparecer rechazos intermitentes.

def test_group_by_ordinal():
    _ok("SELECT departamento, COUNT(*) AS personas FROM personas GROUP BY 1")


def test_group_by_alias_de_salida():
    _ok("SELECT CASE sexo WHEN 1 THEN 'H' ELSE 'M' END AS s, COUNT(*) AS personas "
        "FROM personas GROUP BY s")


# --- Las tres tasas del mercado de trabajo (2011) ---------------------------
# La codificación de 2011 NO es la de 2023: los desocupados son DOS códigos (3 y 4), así
# que la PEA es (2,3,4). PET = 12 y más (Carlos, 13-ago-2026): 'Menor de 12 años' quiere
# decir 11 o menos, así que el universo relevado empieza en los 12.

def _tasa(numerador, where=""):
    sql, _ = validar("SELECT 100.0 * SUM(CASE WHEN pobpcoac %s THEN 1 ELSE 0 END) / "
                     "COUNT(*) AS tasa, COUNT(*) AS n FROM personas%s"
                     % (numerador, (" WHERE " + where) if where else ""))
    return sql


def test_actividad_y_empleo_van_sobre_la_pet():
    for numerador in ("IN (2, 3, 4)", "= 2"):
        sql = _tasa(numerador)
        assert "edad >= 12" in sql
        assert "edad >= 14" not in sql


def test_la_desocupacion_va_sobre_la_pea_y_son_dos_codigos():
    """3 y 4 son ambos desocupados. Con el mapa de 2023 —donde desocupados es sólo el 3—
    la tasa habría contado nada más que a los que buscan por primera vez."""
    sql = _tasa("IN (3, 4)")
    assert "pobpcoac IN (2, 3, 4)" in sql
    assert "edad >= 12" not in sql


def test_la_pet_no_se_restringe_por_respuesta_valida():
    """El modelo escribe el denominador como 'los que tienen respuesta válida'
    (pobpcoac IN (2,3,4,5,6)), que saca a 97.967 personas de 12 y más con código 8 'No
    relevado' y da 59,90 % en vez de 57,75 %. La PET la define la EDAD."""
    sql = _tasa("IN (2, 3, 4)", where="pobpcoac IN (2, 3, 4, 5, 6)")
    assert "2, 3, 4, 5, 6" not in sql
    assert "edad >= 12" in sql


def test_la_tasa_conserva_el_ambito_geografico():
    """Neutralizar el filtro de pobpcoac no puede llevarse puesto el resto del WHERE."""
    sql = _tasa("IN (2, 3, 4)", where="dpto = 1 AND pobpcoac IN (2, 3, 4, 5, 6)")
    assert "dpto = 1" in sql and "edad >= 12" in sql


def test_el_desglose_por_condicion_conserva_a_los_inactivos():
    """Restringir acá borraría a los inactivos, que SON la respuesta."""
    sql, _ = validar("SELECT pobpcoac, COUNT(*) AS n FROM personas GROUP BY pobpcoac")
    assert "edad >= 12" not in sql


def test_el_where_no_queda_con_true_colgado():
    """Cosmético pero visible: el SQL se le muestra al usuario."""
    assert "TRUE" not in _tasa("IN (2, 3, 4)", where="pobpcoac IN (2, 3, 4, 5, 6)").upper()
