"""Tests del módulo compartido. Run: pytest tests/test_comun.py

Cubren los tres bugs reportados por el INE y los dos hallazgos del diagnóstico.
Todo lo que se prueba acá es DETERMINISTA: no hay llamadas al modelo, así que la
suite corre en segundos y no cuesta tokens.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from comun import edad, ejecutor, indicadores, nomenclator as nom, perdidos, rechazos, supresion
from comun.resolver import (AMBIGUO, FRAGMENTADO, NO_ENCONTRADO, OTRO_CENSO, UNICO,
                            resolver)
from comun.sql_entidades import EntidadNoResuelta, preparar_1996, resolver_en_sql
from comun.texto import (distancia, fonetico, normalizar, numero_a_palabra,
                         sin_tildes, variantes_numero)

CENSOS = ("1996", "2004", "2011", "2023")


# ── texto ────────────────────────────────────────────────────────────────
def test_normalizar_quita_tildes_y_puntuacion_pero_conserva_la_enie():
    assert normalizar("  Paso   de los Toros, ") == "PASO DE LOS TOROS"
    assert normalizar("Paysandú") == "PAYSANDU"
    assert normalizar("Bañados de Carrasco") == "BAÑADOS DE CARRASCO"
    assert sin_tildes("años") == "años"


def test_normalizar_expande_abreviaturas():
    assert normalizar("Cnel. Brandzen") == "CORONEL BRANDZEN"
    assert normalizar("Gral. Artigas") == "GENERAL ARTIGAS"
    assert normalizar("Sta. Lucía") == "SANTA LUCIA"


@pytest.mark.parametrize("a,b", [
    ("Chamiso", "Chamizo"), ("Balle", "Valle"), ("Sarandi", "Zarandi"),
    ("Banados", "Bañados"), ("Casupa", "Cazupa"), ("Quebracho", "Kebracho"),
])
def test_fonetico_colapsa_las_confusiones_del_espanol(a, b):
    assert fonetico(a) == fonetico(b)


def test_numeros_en_los_dos_sentidos():
    assert numero_a_palabra(25) == "VEINTICINCO"
    assert numero_a_palabra(33) == "TREINTA Y TRES"
    assert numero_a_palabra(18) == "DIECIOCHO"
    assert "VEINTICINCO DE AGOSTO" in variantes_numero("25 de Agosto")
    assert "25 DE AGOSTO" in variantes_numero("veinticinco de agosto")
    assert "33" in variantes_numero("Treinta y Tres")


def test_distancia_cuenta_la_transposicion_como_un_error():
    assert distancia("CHAMIZO", "CHAMIZO") == 0
    assert distancia("CHAMIZO", "CHAMIOZ") == 1     # transposición
    assert distancia("CHAMIZO", "CHAMISO") == 1     # sustitución


# ── BUG 3c: convención etaria, idéntica en los cuatro motores ────────────
def test_mayores_de_x_incluye_a_los_de_x():
    (c,) = edad.detectar("¿Cuántas personas mayores de 65 años hay?")
    assert (c.operador, c.limite) == (">=", 65)


def test_las_tres_formulaciones_dan_el_mismo_criterio():
    formas = ["mayores de 65 años", "personas de 65 años y más", "de 65 años o más"]
    criterios = {edad.detectar(f)[0].operador + str(edad.detectar(f)[0].limite)
                 for f in formas}
    assert criterios == {">=65"}


def test_menores_y_hasta_son_criterios_distintos_y_ambos_se_declaran():
    (menor,) = edad.detectar("menores de 5 años")
    (hasta,) = edad.detectar("5 años o menos")
    assert menor.operador == "<" and hasta.operador == "<="
    assert menor.declaracion != hasta.declaracion
    assert "no incluye" in menor.declaracion


def test_entre_x_e_y_es_inclusivo_en_los_dos_extremos():
    (c,) = edad.detectar("personas entre 20 y 30 años")
    assert (c.operador, c.limite, c.limite2) == ("entre", 20, 30)
    assert ">= 20" in c.expresion and "<= 30" in c.expresion


@pytest.mark.parametrize("censo", CENSOS)
def test_el_criterio_etario_es_el_mismo_en_los_cuatro_motores(censo):
    instr = edad.instruccion("mayores de 65 años", edad.__dict__.get("x", "edad"))
    assert ">= 65" in instr
    from comun.pipeline import COLUMNA_EDAD
    propia = edad.instruccion("mayores de 65 años", COLUMNA_EDAD[censo])
    assert "%s >= 65" % COLUMNA_EDAD[censo] in propia
    assert "población de 65 años y más" in propia


def test_una_pregunta_sin_edad_no_inventa_criterio():
    assert edad.detectar("¿Cuántas mujeres hay en Salto?") == []


# ── BUG 2: supresión — el cero no es confidencialidad ────────────────────
def test_el_conteo_cero_no_se_rotula_como_confidencialidad():
    filas = [{"personas": None, "n_crudo": 0}]
    quedan, suprimidas, vacias = supresion.suprimir_celdas_chicas(filas, ["n_crudo"])
    assert (quedan, suprimidas, vacias) == ([], 0, 1)


def test_una_celda_chica_de_verdad_si_se_suprime():
    filas = [{"personas": 3, "n_crudo": 3}]
    quedan, suprimidas, vacias = supresion.suprimir_celdas_chicas(filas, ["n_crudo"])
    assert (quedan, suprimidas, vacias) == ([], 1, 0)


def test_el_umbral_es_cinco_y_el_cinco_se_publica():
    filas = [{"n_crudo": 5}, {"n_crudo": 4}]
    quedan, suprimidas, _ = supresion.suprimir_celdas_chicas(filas, ["n_crudo"])
    assert len(quedan) == 1 and quedan[0]["n_crudo"] == 5 and suprimidas == 1


# ── BUG 2: resolución de entidades ───────────────────────────────────────
def test_errata_resuelve_y_declara_la_interpretacion():
    r = resolver("Chamiso", nom.LOCALIDAD, "2023")
    assert r.estado == UNICO and r.entidad.nombre == "CHAMIZO"
    assert "Chamizo" in r.interpretacion and "Florida" in r.interpretacion


def test_minusculas_y_sin_tildes_resuelven():
    assert resolver("paso de los toros", nom.LOCALIDAD, "2023").entidad.nombre == \
        "PASO DE LOS TOROS"
    assert resolver("banados de carrasco", nom.BARRIO, "2011").entidad.nombre == \
        "Bañados de Carrasco"


@pytest.mark.parametrize("censo,escrito,esperado", [
    ("2023", "veinticinco de agosto", "25 DE AGOSTO"),
    ("2011", "veinticinco de agosto", "25 DE AGOSTO"),
    ("1996", "25 de agosto", "VEINTICINCO DE AGOSTO"),
    ("2004", "25 de agosto", "VEINTICINCO DE AGOSTO"),
])
def test_numero_en_la_otra_forma_resuelve_en_cada_censo(censo, escrito, esperado):
    r = resolver(escrito, nom.LOCALIDAD, censo)
    assert r.estado == UNICO and r.entidad.nombre == esperado


@pytest.mark.parametrize("censo", CENSOS)
def test_colision_departamento_ciudad_pide_aclaracion(censo):
    r = resolver("Maldonado", None, censo)
    assert r.estado == AMBIGUO
    assert {e.tipo for e in r.candidatos} == {nom.LOCALIDAD, nom.DEPARTAMENTO}


@pytest.mark.parametrize("censo", CENSOS)
def test_treinta_y_tres_escrito_33_pide_aclaracion(censo):
    r = resolver("33", None, censo)
    assert r.estado == AMBIGUO


@pytest.mark.parametrize("censo", CENSOS)
def test_cerro_chato_es_fragmentacion_no_homonimia(censo):
    r = resolver("Cerro Chato", nom.LOCALIDAD, censo)
    assert r.estado == FRAGMENTADO
    assert len({e.departamento for e in r.candidatos}) == len(r.candidatos) > 1


def test_calificador_de_departamento_desambigua_sin_preguntar():
    r = resolver("Santa Lucía, Canelones", nom.LOCALIDAD, "2023")
    assert r.estado == UNICO and r.entidad.departamento == "CANELONES"


def test_nombre_inexistente_devuelve_sugerencias_no_un_error_seco():
    r = resolver("Xyzabc", nom.LOCALIDAD, "2023")
    assert r.estado == NO_ENCONTRADO and 0 < len(r.sugerencias) <= 5


def test_entidad_de_otro_censo_se_dice_explicitamente():
    r = resolver("Abayuba", nom.LOCALIDAD, "2023")
    assert r.estado == OTRO_CENSO and "1996" in r.censos_alternativos


def test_etiquetas_de_valor_resuelven_con_errata_y_sinonimo():
    r = resolver("afrodesendiente", nom.ETIQUETA, "2023")
    assert r.estado == UNICO and r.entidad.nombre == "Afro o Negra"
    r2 = resolver("unión libre", nom.ETIQUETA, "2011", variable="PEREC03")
    assert r2.estado == UNICO and len(r2.candidatos) == 2   # los dos códigos


@pytest.mark.parametrize("censo", CENSOS)
def test_ningun_fallo_de_resolucion_se_rotula_como_confidencialidad(censo):
    for texto in ("Xyzabc", "Chamiso", "Maldonado", "Cerro Chato", "Abayuba"):
        r = resolver(texto, None, censo)
        assert r.estado in (UNICO, AMBIGUO, FRAGMENTADO, NO_ENCONTRADO, OTRO_CENSO)


# ── BUG 2: post-paso sobre el SQL ────────────────────────────────────────
def test_el_sql_con_errata_se_reescribe_al_nombre_canonico():
    sql = ("SELECT SUM(W) AS personas, COUNT(*) AS n_crudo FROM personas_2023 p "
           "JOIN localidades_2023 l ON (p.DEPARTAMENTO||p.LOCALIDAD)=l.codloc "
           "WHERE l.nombre='CHAMISO'")
    nuevo, interp, alternativas = resolver_en_sql(sql, "2023")
    assert "CHAMIZO" in nuevo and "CHAMISO" not in nuevo
    assert interp and "Florida" in interp[0]
    assert alternativas == []


def test_el_sql_ambiguo_no_se_ejecuta_y_ofrece_opciones():
    sql = ("SELECT SUM(W) AS personas, COUNT(*) AS n_crudo FROM personas_2023 p "
           "JOIN localidades_2023 l ON (p.DEPARTAMENTO||p.LOCALIDAD)=l.codloc "
           "WHERE l.nombre='CERRO CHATO'")
    with pytest.raises(EntidadNoResuelta) as exc:
        resolver_en_sql(sql, "2023")
    assert exc.value.rechazo.codigo == rechazos.FRAGMENTADA
    assert len(exc.value.rechazo.opciones) == 4


def test_el_post_paso_no_toca_literales_que_no_son_del_nomenclator():
    sql = ("SELECT COUNT(*) FROM personas_2023 p JOIN paises q "
           "ON p.PERMI01_4=q.codigo WHERE q.nombre='PARAGUAY'")
    nuevo, interp, alternativas = resolver_en_sql(sql, "2023")
    assert nuevo == sql and interp == [] and alternativas == []


# ── hallazgo crítico: fan-out del nomenclátor cruzado de 1996 ────────────
SQL_1996_LOCALIDAD = (
    "SELECT c.nom_1996 AS geo_nombre, COUNT(*) AS personas FROM personas_1996 p "
    "JOIN cod_localidades_1963_2023 c ON c.dpto=p.dpto AND c.cod_1996=p.loc "
    "GROUP BY c.nom_1996")


def _total(sql):
    return ejecutor.escalar(nom.BASES["1996"],
                            "SELECT SUM(personas) FROM (%s)" % sql)


def test_el_join_al_cruzado_sin_deduplicar_infla_las_cifras():
    """Deja constancia del defecto: si esto dejara de fallar, el fix sobra."""
    assert _total(SQL_1996_LOCALIDAD) == 16321955


def test_el_nomenclator_deduplicado_devuelve_el_total_real_de_1996():
    corregido, hubo = preparar_1996(SQL_1996_LOCALIDAD)
    assert hubo
    assert _total(corregido) == 3163763        # total verificado del Censo 1996


def test_la_deduplicacion_usa_left_join_para_no_perder_a_nadie():
    corregido, _ = preparar_1996(SQL_1996_LOCALIDAD)
    assert "LEFT JOIN" in corregido.upper()


# ── BUG 3a: desambiguación de indicadores ────────────────────────────────
@pytest.mark.parametrize("censo", ("1996", "2011", "2023"))
def test_departamento_mas_educado_ofrece_opciones_y_no_un_numero(censo):
    r = indicadores.desambiguar("¿Cuál es el departamento más educado?", censo)
    assert r is not None and r["ok"] is False and r["sql"] is None
    assert len(r["opciones"]) >= 3
    for o in r["opciones"]:
        assert o["detalle"]            # universo y tratamiento de perdidos, siempre


def test_en_2004_dice_que_no_se_relevo_y_en_que_censos_si():
    r = indicadores.desambiguar("¿Cuál es el departamento más educado?", "2004")
    assert r["motivo"] == rechazos.NO_RELEVADA
    assert "1996" in r["respuesta"] and "2023" in r["respuesta"]


def test_una_pregunta_concreta_no_se_desambigua():
    assert indicadores.desambiguar("¿Cuántas personas viven en Salto?", "2023") is None


def test_indicadores_ambiguos_sin_tabla_igual_piden_precision():
    r = indicadores.desambiguar("¿Cuál es el departamento más pobre?", "2011")
    assert r is not None and r["motivo"] == rechazos.AMBIGUA


# ── etiquetas de rechazo: cada motivo con su rótulo ──────────────────────
def test_cada_motivo_tiene_su_propia_etiqueta_y_solo_una_habla_de_confidencialidad():
    casos = [rechazos.supresion(2), rechazos.no_encontrada("Xyz", ["Abc"]),
             rechazos.ambigua("Maldonado", []), rechazos.no_relevada("educación", "2004"),
             rechazos.procesamiento("timeout")]
    codigos = [c.codigo for c in casos]
    assert len(set(codigos)) == len(codigos)
    conf = [c for c in casos if "confidencialidad" in c.mensaje.lower()]
    assert len(conf) == 1 and conf[0].codigo == rechazos.SUPRESION


def test_formulaciones_equivalentes_dan_el_mismo_motivo():
    a = indicadores.desambiguar("¿Cuál es el departamento más educado?", "2023")
    b = indicadores.desambiguar("¿Qué departamento tiene mayor nivel educativo?", "2023")
    assert a["motivo"] == b["motivo"] == rechazos.AMBIGUA


# ── centinelas no declarados de 2011 ─────────────────────────────────────
def test_los_centinelas_de_2011_estan_declarados_y_van_al_prompt():
    assert 88 in perdidos.de("2011", "Años_estudio")
    # 5555 = secreto estadístico: desde que las 53 personas protegidas se cargan
    # (28-jul-2026), el código está en las columnas crudas y contamina promedios.
    assert 5555 in perdidos.de("2011", "Años_estudio")
    bloque = perdidos.bloque_para_prompt("2011")
    assert "Años_estudio" in bloque and "NOT IN (88, 5555)" in bloque
    assert len(perdidos.variables("2011")) == 11


def test_excluir_el_centinela_cambia_el_promedio_de_2011():
    """La cifra que el INE vio (13,80) contra la correcta (8,60).

    Excluir SOLO el 88 ya no alcanza: quedan 12 filas de Montevideo con 5555 que
    llevan el promedio a 8,66. Hay que excluir los dos códigos declarados.
    """
    db = nom.BASES["2011"]
    mvd = "FROM personas WHERE departamento='MONTEVIDEO'"
    crudo = ejecutor.escalar(db, 'SELECT ROUND(AVG("Años_estudio"),2) ' + mvd)
    solo_88 = ejecutor.escalar(db, 'SELECT ROUND(AVG("Años_estudio"),2) ' + mvd +
                               ' AND "Años_estudio" NOT IN (88)')
    limpio = ejecutor.escalar(db, 'SELECT ROUND(AVG("Años_estudio"),2) ' + mvd +
                              ' AND "Años_estudio" NOT IN (88, 5555)')
    assert crudo == 13.8 and solo_88 == 8.66 and limpio == 8.6


# ── el nomenclátor de los cuatro censos carga ────────────────────────────
@pytest.mark.parametrize("censo", CENSOS)
def test_el_catalogo_de_cada_censo_tiene_las_cuatro_familias(censo):
    tipos = {e.tipo for e in nom.catalogo(censo)}
    assert {nom.LOCALIDAD, nom.DEPARTAMENTO, nom.BARRIO, nom.ETIQUETA} <= tipos
    assert len(nom.catalogo(censo, nom.DEPARTAMENTO)) == 19


# ── casos que encontró la batería extremo a extremo ──────────────────────
SQL_MALDONADO = ("SELECT SUM(W) AS personas, COUNT(*) AS n_crudo FROM personas_2023 p "
                 "JOIN departamentos_2023 d ON p.DEPARTAMENTO=d.codigo "
                 "WHERE d.nombre='MALDONADO'")


def test_colision_departamento_ciudad_se_declara_y_ofrece_la_otra_lectura():
    """Caso 'Maldonado': el modelo eligió el departamento al escribir el SQL. No se
    bloquea la respuesta, pero la elección deja de ser silenciosa —se declara— y la
    ciudad (100.985 personas contra 212.954) se ofrece como alternativa."""
    _, interp, alternativas = resolver_en_sql(
        SQL_MALDONADO, "2023", "¿Cuántas personas viven en Maldonado?")
    assert "Maldonado (departamento)" in interp
    assert len(alternativas) == 1
    assert "100.985" in alternativas[0]["detalle"]


def test_si_la_pregunta_ya_aclara_el_tipo_no_se_ofrece_alternativa():
    nuevo, interp, alternativas = resolver_en_sql(
        SQL_MALDONADO, "2023", "¿Cuántas personas viven en el departamento de Maldonado?")
    assert nuevo == SQL_MALDONADO and interp == [] and alternativas == []


def test_la_cifra_del_chip_respeta_la_supresion():
    """El chip lleva la población de la lectura alternativa; si esa entidad tuviera
    menos de 5 registros crudos, iría sin cifra."""
    from comun import nomenclator as n2
    chica = n2.Entidad("2023", n2.LOCALIDAD, "99999", "INEXISTENTE", "X", None)
    assert n2.poblacion(chica) is None


@pytest.mark.parametrize("censo,esperado", [
    ("1996", "NO lo excluyas"), ("2023", "99 es una edad VÁLIDA")])
def test_el_dominio_de_la_edad_esta_declarado_para_que_el_modelo_no_improvise(censo, esperado):
    """El modelo excluía el código 99 en una corrida y no en otra: 442 personas de
    diferencia en la misma pregunta del Censo 2023."""
    instr = edad.instruccion("mayores de 65 años", edad.DOMINIO[censo]["columna"], censo)
    assert esperado in instr


def test_el_criterio_no_se_pide_como_columna_del_select():
    instr = edad.instruccion("mayores de 65 años", "PERNA01", "2023")
    assert "NO agregues una columna al SELECT" in instr


# ── la declaración no puede depender de que el modelo obedezca ────────────
def test_si_el_redactor_declara_la_interpretacion_no_se_duplica():
    from comun import pipeline
    texto = "En Salto (departamento) viven 136.195 personas."
    assert pipeline.asegurar_declaracion(texto, ["Salto (departamento)"], {}) == texto


def test_si_el_redactor_la_omite_se_antepone():
    from comun import pipeline
    salida = pipeline.asegurar_declaracion(
        "En Salto viven 136.195 personas.", ["Salto (departamento)"], {})
    assert salida.startswith("Se interpretó como Salto (departamento).")
    assert "136.195" in salida


def test_el_criterio_etario_tambien_se_garantiza():
    from comun import pipeline
    contexto = {"declaraciones_edad": ["población de 65 años y más"]}
    salida = pipeline.asegurar_declaracion("Hay 545.299 personas.", [], contexto)
    assert "población de 65 años y más" in salida


def test_sin_interpretacion_ni_criterio_el_texto_queda_igual():
    from comun import pipeline
    assert pipeline.asegurar_declaracion("Hay 100 personas.", [], {}) == "Hay 100 personas."


# ── caché de respuestas ──────────────────────────────────────────────────
def test_la_cache_ignora_mayusculas_y_espacios_pero_no_el_censo():
    from comun import cache
    cache.vaciar()
    cache.guardar(cache.SQL_DE_PREGUNTA, "2023", "¿cuánta gente vive en salto?", "SELECT 1")
    assert cache.obtener(cache.SQL_DE_PREGUNTA, "2023", "¿Cuánta gente vive en Salto?  ") == "SELECT 1"
    assert cache.obtener(cache.SQL_DE_PREGUNTA, "2011", "¿cuánta gente vive en salto?") is None


def test_la_clave_del_nivel_B_es_el_sql_resuelto_no_el_texto_escrito():
    """'Chamiso' y 'Chamizo' producen el MISMO SQL después del resolver, así que
    comparten entrada de caché. Es la regla que pedía el encargo."""
    from comun import cache
    cache.vaciar()
    base = ("SELECT SUM(W) AS personas, COUNT(*) AS n_crudo FROM personas_2023 p "
            "JOIN localidades_2023 l ON (p.DEPARTAMENTO||p.LOCALIDAD)=l.codloc "
            "WHERE l.nombre='%s'")
    from comun.sql_entidades import canonizar
    sql_a, _, _ = resolver_en_sql(base % "CHAMISO", "2023")
    sql_b, _, _ = resolver_en_sql(base % "CHAMIZO", "2023")
    assert sql_a != sql_b                      # llegan con distinto formato
    assert canonizar(sql_a) == canonizar(sql_b)
    assert cache.clave(cache.RESULTADO_DE_SQL, "2023", canonizar(sql_a)) == \
        cache.clave(cache.RESULTADO_DE_SQL, "2023", canonizar(sql_b))


def test_la_cache_desaloja_al_llegar_al_tope():
    from comun import cache
    cache.vaciar()
    tope = cache.MAX_ENTRADAS
    for i in range(tope + 5):
        cache.guardar(cache.SQL_DE_PREGUNTA, "2023", "pregunta %d" % i, "SELECT %d" % i)
    assert cache.metricas()["entradas"] == tope
    assert cache.metricas()["desalojos"] == 5


def test_no_se_cachea_un_no_respondible():
    from comun import cache, pipeline
    cache.vaciar()
    pipeline.recordar_sql("¿algo raro?", "2023", {}, "NO_RESPONDIBLE")
    assert pipeline.sql_cacheado("¿algo raro?", "2023", {}) is None


# ── precalentado de la caché ─────────────────────────────────────────────
def test_los_chips_del_backend_y_del_frontend_no_pueden_divergir():
    """Precalentar preguntas que la interfaz no muestra no sirve de nada, y al
    revés deja chips fríos. Las dos listas tienen que ser la misma."""
    import json as _json
    import re as _re
    from comun import precalentar

    ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "app", "static", "index.html")
    with open(ruta, encoding="utf-8") as fh:
        html = fh.read()
    bloque = _re.search(r"const CHIPS = (\{.*?\n\});", html, _re.S).group(1)
    # el literal JS usa comillas simples y una coma final: se normaliza a JSON
    bloque = bloque.replace("'", '"')
    bloque = _re.sub(r",(\s*[}\]])", r"\1", bloque)
    del_frontend = _json.loads(bloque)

    assert del_frontend == precalentar.CHIPS


def test_el_precalentado_cubre_los_cuatro_censos_y_empieza_por_2023():
    from comun import precalentar
    pares = precalentar.preguntas()
    assert len(pares) == 16
    assert {c for c, _ in pares} == {"1996", "2004", "2011", "2023"}
    assert pares[0][0] == "2023"      # el censo por defecto de la interfaz


def test_la_cache_no_guarda_las_opciones_porque_dependen_de_la_pregunta():
    """Dos preguntas distintas ejecutan el MISMO SQL para Salto: una debe ofrecer
    el chip de la ciudad y la otra no, porque ya aclaró que quería el
    departamento. Cachear las opciones bajo la clave del SQL le daría a una las
    de la otra, según cuál llegara primero."""
    from comun import cache, pipeline
    cache.vaciar()
    sql = "SELECT 1 AS personas"
    pipeline.recordar_resultado(sql, "2023",
                                {"ok": True, "respuesta": "x", "opciones": [{"texto": "chip"}]})
    sin = pipeline.resultado_cacheado(sql, "2023")
    assert "opciones" not in sin
    con = pipeline.resultado_cacheado(sql, "2023", [{"texto": "el mío"}])
    assert con["opciones"] == [{"texto": "el mío"}]


@pytest.mark.parametrize("censo,sql", [
    ("2004", "SELECT COUNT(*) AS personas FROM censo2004 WHERE per = 1 AND dpto = '15'"),
    ("1996", "SELECT COUNT(*) AS personas FROM personas_1996 WHERE dpto = '15'"),
    ("2023", "SELECT ROUND(SUM(W)) AS personas FROM personas_2023 WHERE DEPARTAMENTO = '15'"),
])
def test_la_colision_se_detecta_aunque_el_sql_filtre_por_codigo(censo, sql):
    """El modelo a veces filtra por código en vez de por nombre ('dpto = 15'), y
    entonces no hay literal de nombre que resolver. La entidad hay que
    reconocerla igual, o la lectura elegida vuelve a ser silenciosa."""
    nuevo, interp, alternativas = resolver_en_sql(sql, censo, "¿Cuántas personas hay en Salto?")
    assert nuevo == sql                       # un código no se reescribe
    assert "Salto (departamento)" in interp
    assert len(alternativas) == 1


def test_un_codigo_sin_colision_no_declara_ni_ofrece_nada():
    sql = "SELECT ROUND(SUM(W)) AS personas FROM personas_2023 WHERE DEPARTAMENTO = '09'"
    nuevo, interp, alternativas = resolver_en_sql(sql, "2023", "¿Cuántas personas hay?")
    assert (nuevo, interp, alternativas) == (sql, [], [])
