"""Tests del mapa RESUMEN (comun/mapa_resumen.py).

Lo que se prueba acá no es que el mapa se dibuje, sino que la cifra que dibuja sea
la correcta. El resumen se calcula RECALCULANDO la consulta un nivel más arriba y
no sumando las filas ya publicadas, y la diferencia entre las dos cosas es
exactamente lo que la supresión por confidencialidad se llevó: en el desglose por
segmento de la población afro de 2023 son 447 celdas. Un mapa que las pierda muestra
menos gente que la tabla que tiene al lado.

La agregación se ejecuta contra DuckDB de verdad —el motor real de los cuatro
censos— y no contra un doble: el SQL se arma por concatenación, y que parsee es
parte de lo que hay que probar.

Run: pytest tests/test_mapa_resumen.py -q
"""
import os
import sys

import duckdb
import pytest

AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AQUI)

from comun import mapa_resumen   # noqa: E402


# --------------------------------------------------------------- aditividad

SQL_2023 = ("SELECT DEPARTAMENTO || SECCION || SEGMENTO AS geo_codigo, "
            "ROUND(SUM(w)) AS personas, COUNT(*) AS n_crudo "
            "FROM personas_2023 GROUP BY 1 LIMIT 5000")
SQL_PCT = ("SELECT DEPARTAMENTO || SECCION AS geo_codigo, "
           "100.0 * SUM(CASE WHEN asc_afro = 'Si' THEN 1 END) / COUNT(*) AS porcentaje, "
           "COUNT(*) AS n_crudo FROM personas_2023 GROUP BY 1")


def test_suma_ponderada_es_aditiva():
    """ROUND(SUM(w)): el ROUND envuelve, no cambia que por debajo haya una suma."""
    assert mapa_resumen.es_aditiva(SQL_2023, "personas")
    assert mapa_resumen.es_aditiva(SQL_2023, "n_crudo")


def test_porcentaje_no_es_aditivo():
    """Sumar los porcentajes de los segmentos de una sección da un número sin
    significado. Es el caso que obliga a decidir por la EXPRESIÓN y no por el
    nombre de la columna."""
    assert not mapa_resumen.es_aditiva(SQL_PCT, "porcentaje")


def test_promedio_no_es_aditivo():
    assert not mapa_resumen.es_aditiva(
        "SELECT dpto AS geo_codigo, AVG(edad) AS edad_media FROM t GROUP BY 1",
        "edad_media")


def test_alias_ausente_o_sql_ilegible_no_es_aditivo():
    """Fail-closed: sin certeza, no hay resumen."""
    assert not mapa_resumen.es_aditiva(SQL_2023, "no_existe")
    assert not mapa_resumen.es_aditiva("esto no es SQL {{", "personas")
    assert not mapa_resumen.es_aditiva(SQL_2023, "")


# ------------------------------------------------------------------ envoltura

def test_envoltura_quita_el_limit():
    """El resumen tiene que ver TODAS las unidades: si heredara el LIMIT del
    desglose sumaría un recorte y volveríamos al problema de origen."""
    sql = mapa_resumen.envolver(SQL_2023, "geo_codigo", "personas", ["n_crudo"], 4, False)
    assert "LIMIT" not in sql.upper()


def test_envoltura_no_repite_la_columna_cuando_metrica_y_conteo_coinciden():
    """COUNT(*) AS personas: la métrica ES el conteo. Emitirla dos veces daría dos
    columnas con el mismo nombre."""
    sql = mapa_resumen.envolver("SELECT d AS geo_codigo, COUNT(*) AS personas FROM t GROUP BY 1",
                                "geo_codigo", "personas", ["personas"], 2, False)
    assert sql.count('SUM("personas")') == 1


def test_envoltura_rechaza_un_alias_que_no_sea_identificador():
    """Se arma SQL concatenando: lo único que se agrega son nombres de columna, y
    se comprueba que lo sean."""
    assert mapa_resumen.envolver(SQL_2023, 'geo"; DROP TABLE personas_2023; --',
                                 "personas", ["n_crudo"], 4, False) is None
    assert mapa_resumen.envolver(SQL_2023, "geo_codigo", "personas",
                                 ["n_crudo); --"], 4, False) is None


# ------------------------------------------------- agregación contra DuckDB

def _base_2023():
    """Dos secciones del departamento 01. La sección 0101 tiene un segmento chico
    (3 personas) que la supresión saca del desglose fino; la 0102, ninguno."""
    con = duckdb.connect()
    con.execute("CREATE TABLE fino(geo_codigo VARCHAR, personas BIGINT, n_crudo BIGINT)")
    con.execute("INSERT INTO fino VALUES "
                "('0101001', 1000, 1000), ('0101002', 500, 500), ('0101003', 3, 3), "
                "('0102001', 200, 200), ('0102002', 300, 300)")
    return con


def _ejecutar(con):
    def correr(sql):
        res = con.execute(sql)
        cols = [d[0] for d in res.description]
        return [dict(zip(cols, f)) for f in res.fetchall()]
    return correr


SQL_FINO = ("SELECT geo_codigo, SUM(personas) AS personas, SUM(n_crudo) AS n_crudo "
            "FROM fino GROUP BY 1 LIMIT 5000")


def test_resumen_2023_incluye_lo_que_la_supresion_saco_del_desglose():
    """El corazón del asunto. La sección 0101 tiene 1.503 personas: 1.000 + 500 + el
    segmento de 3 que NO se publica en la tabla por confidencialidad. Sumar las
    filas publicadas daría 1.500; recalcular da 1.503, que es la cifra de la
    sección. La supresión sigue valiendo donde corresponde: el segmento de 3 no se
    publica, la sección de 1.503 sí."""
    con = _base_2023()
    mapa, _supr = mapa_resumen.resumir(
        SQL_FINO, "segmento_2023", "geo_codigo", "personas", ["n_crudo"],
        _ejecutar(con), limite=3, numerico=False)
    assert mapa["nivel"] == "seccion_2023"
    valores = {d["clave"]: d["valor"] for d in mapa["datos"]}
    assert valores == {"0101": 1503, "0102": 500}


def test_resumen_conserva_los_ceros_a_la_izquierda():
    """El código de 2023 es TEXTO y el corte es un prefijo justamente por esto: el
    departamento 01 no puede convertirse en el 1, que es otro código."""
    con = _base_2023()
    mapa, _ = mapa_resumen.resumir(
        SQL_FINO, "segmento_2023", "geo_codigo", "personas", ["n_crudo"],
        _ejecutar(con), limite=3, numerico=False)
    assert all(len(d["clave"]) == 4 for d in mapa["datos"])


def test_resumen_sube_hasta_el_nivel_que_entra():
    """Con un tope de 1 unidad, la sección tampoco alcanza y el resumen sigue
    subiendo hasta departamento. El corte se aplica sobre el código ORIGINAL: del
    segmento al departamento son los 2 primeros dígitos, no los 2 del prefijo de
    sección."""
    con = _base_2023()
    mapa, _ = mapa_resumen.resumir(
        SQL_FINO, "segmento_2023", "geo_codigo", "personas", ["n_crudo"],
        _ejecutar(con), limite=1, numerico=False)
    assert mapa["nivel"] == "depto_2023"
    assert mapa["datos"] == [{"clave": "01", "valor": 2003}]


def test_resumen_declara_de_donde_viene():
    """El frontend marca el mapa como resumen con esto; si no viniera, el mapa se
    leería como si fuera el desglose."""
    con = _base_2023()
    mapa, _ = mapa_resumen.resumir(
        SQL_FINO, "segmento_2023", "geo_codigo", "personas", ["n_crudo"],
        _ejecutar(con), limite=3, numerico=False)
    assert mapa["resumen"] == {"de": "segmento censal", "a": "sección censal"}


def test_no_hay_resumen_de_un_porcentaje():
    """Antes ningún mapa que uno con cifras inventadas."""
    con = _base_2023()
    con.execute("CREATE TABLE pct AS SELECT geo_codigo, "
                "100.0 * personas / 2000 AS porcentaje, n_crudo FROM fino")
    sql = ("SELECT geo_codigo, AVG(porcentaje) AS porcentaje, SUM(n_crudo) AS n_crudo "
           "FROM pct GROUP BY 1")
    mapa, supr = mapa_resumen.resumir(
        sql, "segmento_2023", "geo_codigo", "porcentaje", ["n_crudo"],
        _ejecutar(con), limite=3, numerico=False)
    assert mapa is None and supr == 0


def test_no_hay_resumen_de_un_nivel_que_ya_es_el_mas_grueso():
    con = _base_2023()
    mapa, _ = mapa_resumen.resumir(
        SQL_FINO, "depto_2023", "geo_codigo", "personas", ["n_crudo"],
        _ejecutar(con), limite=1, numerico=False)
    assert mapa is None


def test_resumen_descarta_la_zona_contestada():
    """Rincón de Artigas no se mapea en ningún nivel, y el prefijo de un código
    excluido sigue siendo un código excluido ('0200000' -> '0200')."""
    con = _base_2023()
    con.execute("INSERT INTO fino VALUES ('0200000', 50, 50)")
    mapa, _ = mapa_resumen.resumir(
        SQL_FINO, "segmento_2023", "geo_codigo", "personas", ["n_crudo"],
        _ejecutar(con), limite=3, numerico=False, excluir={"0200", "0200000", "02000"})
    assert "0200" not in {d["clave"] for d in mapa["datos"]}


def test_la_supresion_se_aplica_sobre_el_total_agregado():
    """Una sección entera con menos de 5 casos crudos no se publica tampoco
    agregada: la regla del INE vale en todos los niveles."""
    con = duckdb.connect()
    con.execute("CREATE TABLE fino(geo_codigo VARCHAR, personas BIGINT, n_crudo BIGINT)")
    con.execute("INSERT INTO fino VALUES ('0101001', 2, 2), ('0101002', 1, 1), "
                "('0102001', 900, 900), ('0102002', 100, 100)")
    mapa, supr = mapa_resumen.resumir(
        SQL_FINO, "segmento_2023", "geo_codigo", "personas", ["n_crudo"],
        _ejecutar(con), limite=3, numerico=False)
    assert {d["clave"] for d in mapa["datos"]} == {"0102"}
    assert supr == 1


# ------------------------------------------------- históricos (código numérico)

def test_resumen_historico_compone_los_divisores():
    """El código histórico es dpto*100000 + secc*1000 + segm. Del segmento a la
    sección es /1.000; de ahí al departamento, otro /100. Del segmento AL
    DEPARTAMENTO son /100.000, y confundirlo pintaría la sección 10 como si fuera
    el departamento 10."""
    con = duckdb.connect()
    con.execute("CREATE TABLE fino(geo_codigo BIGINT, personas BIGINT)")
    #            dpto 1, secc 0, segms 1 y 2      dpto 1, secc 1, segm 1
    con.execute("INSERT INTO fino VALUES (100001, 10), (100002, 20), (101001, 30)")
    sql = "SELECT geo_codigo, SUM(personas) AS personas FROM fino GROUP BY 1"
    correr = _ejecutar(con)

    a_seccion, _ = mapa_resumen.resumir(
        sql, "segmento_1996", "geo_codigo", "personas", ["personas"], correr,
        limite=2, numerico=True, clave_de=lambda c, n: int(c))
    assert a_seccion["nivel"] == "seccion"
    assert {d["clave"]: d["valor"] for d in a_seccion["datos"]} == {100: 30, 101: 30}

    a_depto, _ = mapa_resumen.resumir(
        sql, "segmento_2004", "geo_codigo", "personas", ["personas"], correr,
        limite=1, numerico=True, clave_de=lambda c, n: int(c))
    assert a_depto["nivel"] == "departamento"
    assert a_depto["datos"] == [{"clave": 1, "valor": 60}]


def test_sin_poligono_no_se_dibuja_nada():
    """Misma regla que el mapa fino: media geografía pintada se lee como la
    geografía entera."""
    con = duckdb.connect()
    con.execute("CREATE TABLE fino(geo_codigo BIGINT, personas BIGINT)")
    con.execute("INSERT INTO fino VALUES (100001, 10), (999001, 20)")
    mapa, _ = mapa_resumen.resumir(
        "SELECT geo_codigo, SUM(personas) AS personas FROM fino GROUP BY 1",
        "segmento_1996", "geo_codigo", "personas", ["personas"], _ejecutar(con),
        limite=1, numerico=True,
        clave_de=lambda c, n: int(c) if int(c) == 100 else None)
    assert mapa is None


# ------------------------------------ el mapa fino que no se puede dibujar

def test_segmento_sin_poligono_queda_marcado_como_resumible():
    """La cartografía de segmentos de 1996 y 2004 no cubre todo el país, y hasta
    ahora un desglose que tocara un segmento sin polígono se quedaba SIN NINGÚN
    mapa. Ahora se marca como resumible: la sección censal sí tiene cartografía
    completa y su total se recalcula sobre todos los segmentos, tengan polígono o
    no. Eso no es un mapa con agujeros, es otro nivel entero."""
    from motor_historico import Motor
    sql = ("SELECT CAST(t.dpto AS INTEGER)*100000 + CAST(t.secc AS INTEGER)*1000 + "
           "CAST(t.segm AS INTEGER) AS geo_codigo, COUNT(*) AS personas FROM censo2004 t "
           "GROUP BY CAST(t.dpto AS INTEGER), CAST(t.secc AS INTEGER), CAST(t.segm AS INTEGER)")
    # 99999999 no es un segmento con polígono en ningún censo.
    filas = [{"geo_codigo": 99999999, "personas": 10}]
    m = Motor.construir_mapa(sql, filas, 0, "2004")
    assert m["_sin_poligono"] and m["datos"] == [] and m["_unidades"] == 1
    assert m["nivel"] == "segmento_2004"


def test_un_nivel_que_no_es_segmento_sigue_sin_dibujarse():
    """El resumen NO afloja la regla vieja donde seguía teniendo razón: un barrio o
    una sección sin polígono siguen sin mapa, porque ahí no hay un nivel más grueso
    que sea la respuesta correcta."""
    from motor_historico import Motor
    sql = ("SELECT CAST(t.barrio AS INTEGER) AS geo_codigo, COUNT(*) AS personas "
           "FROM censo2004 t GROUP BY CAST(t.barrio AS INTEGER)")
    assert Motor.construir_mapa(sql, [{"geo_codigo": 777, "personas": 10}], 0, "2004") is None


# ------------------------------------------------------------------- el aviso

def test_el_aviso_no_menciona_ningun_tope():
    """El aviso viejo decía 'supera el máximo de 300', y ese número se leía como el
    tope de FILAS que era un bug y ya no existe. Lo que se explica es la escala."""
    con_mapa = mapa_resumen.aviso(3756, "segmento_2023", "seccion_2023")
    sin_mapa = mapa_resumen.aviso(3756, "segmento_2023")
    for texto in (con_mapa, sin_mapa):
        assert "300" not in texto
        assert "3.756" in texto
        assert "segmento censal" in texto
    assert "sección censal" in con_mapa
    assert "no se dibuja ninguno" in sin_mapa
    assert "segmentos censales" in con_mapa   # plural bien formado, no 'segmento censals'


def test_el_aviso_distingue_la_escala_de_la_cartografia():
    """Son dos razones distintas para no dibujar el desglose y se explican distinto:
    en 2023 no entra por escala; en 1996 y 2004 la cartografía de segmentos —
    reconstruida desde las planchas del INE— no los cubre a todos."""
    escala = mapa_resumen.aviso(3756, "segmento_2023", "seccion_2023")
    carto = mapa_resumen.aviso(1034, "segmento_2004", "seccion", motivo="cartografia")
    assert "escala" in escala and "planchas del INE" not in escala
    assert "planchas del INE" in carto and "1.034" in carto


def test_el_aviso_avisa_que_las_cifras_del_resumen_se_recalculan():
    """El mapa por sección no da lo mismo que sumar la tabla —la tabla no publica las
    celdas de menos de cinco casos y el total de la sección sí las incluye—, y quien
    sume la tabla tiene que poder entender la diferencia."""
    texto = mapa_resumen.aviso(3756, "segmento_2023", "seccion_2023")
    assert "recalculado" in texto and "confidencialidad" in texto
    assert "detalle está en la tabla" in mapa_resumen.aviso(3756, "segmento_2023")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
