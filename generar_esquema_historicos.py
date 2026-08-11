#!/usr/bin/env python3
"""generar_esquema_historicos.py — Genera los artefactos de esquema de los motores
1996 y 2004 a partir de las propias bases.

Produce, para cada censo:
  esquema_llm_XXXX.txt  — el esquema que ve el LLM (una línea por variable:
                          "- NOMBRE | etiqueta | códigos"), mismo formato que 2023.
  cols_XXXX.json        — whitelist de columnas por tabla, que consume el guard.

Se genera en vez de escribirse a mano para que esquema y base no se puedan
desincronizar: las etiquetas salen de las tablas diccionario_variables /
diccionario_valores de cada base, que son las que se cargaron y verificaron.

Uso:  python3 generar_esquema_historicos.py
"""
import json
import os
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
DATOS = os.path.join(AQUI, "datos")
sys.path.insert(0, AQUI)

from comun import ejecutor  # noqa: E402  (después de fijar el sys.path)

# Variables cuyos códigos NO se enumeran en el esquema: son de alta cardinalidad
# (geografía, clasificadores de ocupación y de estudios). Listarlas gastaría miles
# de tokens por consulta y el LLM las resuelve por JOIN al nomenclátor.
NO_ENUMERAR = {
    "dpto", "secc", "segm", "loc", "barrio", "ccz", "vivienda", "hogarviv",
    "habdepc", "hablocc", "hac5depc", "hac5loc", "nacdepc", "naclocc",
    "cota70", "ciuo88", "ciur2", "ciur3", "carrerc", "tecnsc",
    "edad", "totalper", "tothombres", "totmujeres", "llegada",
    "id_viv", "nro_person", "nhogar", "nom_dpto", "nom_loc", "nom_barrio",
    "vivienda_key", "hogar_key",
}
# Tablas de metadatos: no se exponen al LLM como consultables.
METADATOS = {"dominios_observados", "perfil_columnas",
             "diccionario_variables", "diccionario_valores"}


def columnas_de(cx, tabla):
    return [r[1] for r in cx.execute("PRAGMA table_info(%s)" % tabla)]


def tablas_de(cx):
    # information_schema y no sqlite_master: es lo que corresponde ahora que la
    # base se lee con DuckDB, y además evita el LIKE del filtro viejo, que el
    # ejecutor rechaza por resolverse distinto en cada motor.
    return [r[0] for r in cx.execute(
        "SELECT table_name FROM information_schema.tables ORDER BY table_name")]


def _linea(nombre, etiqueta, codigos, perdidos):
    partes = [nombre, (etiqueta or "").strip()]
    cods = "; ".join("%s=%s" % (c, e) for c, e in codigos)
    if perdidos:
        cods += ("; " if cods else "") + "PERDIDOS: " + ", ".join(perdidos)
    partes.append(cods)
    return "- " + " | ".join(partes)


def bloque_tabla(cx, tabla, dicc_var, dicc_val, perdidos_por_var):
    out = []
    for col in columnas_de(cx, tabla):
        clave = (tabla, col)
        etiqueta = dicc_var.get(clave, "")
        if col in NO_ENUMERAR:
            codigos = []
        else:
            codigos = dicc_val.get(clave, [])
        out.append(_linea(col, etiqueta, codigos, perdidos_por_var.get(clave, [])))
    return out


def cargar_diccionario_1996(cx):
    var = {(t, v): d for t, v, d in cx.execute(
        "SELECT tabla, variable, descripcion FROM diccionario_variables")}
    val, perd = {}, {}
    for t, v, c, e, p in cx.execute(
            "SELECT tabla, variable, codigo, etiqueta, es_perdido FROM diccionario_valores "
            "ORDER BY tabla, variable, CAST(orden AS INTEGER), codigo"):
        if p:
            perd.setdefault((t, v), []).append("%s=%s" % (c, e))
        else:
            val.setdefault((t, v), []).append((c, e))
    return var, val, perd


def cargar_diccionario_2004(cx, tabla):
    var = {(tabla, v): d for v, d in cx.execute(
        "SELECT variable, descripcion FROM diccionario_variables")}
    val = {}
    for v, c, e in cx.execute(
            "SELECT variable, codigo, etiqueta FROM diccionario_valores "
            "ORDER BY variable, orden"):
        val.setdefault((tabla, v), []).append((c, e))
    return var, val, {}


CABECERA_1996 = """CENSO 1996 (VII Censo General de Población, III de Hogares y V de Viviendas, INE Uruguay).
Censo COMPLETO, SIN ponderación: todas las cifras son conteos EXACTOS.
(a) PERSONAS: COUNT(*) sobre personas_1996 (3.163.763 registros = la población del censo).
(b) HOGARES: COUNT(DISTINCT hogar_key) sobre personas_1996 (975.056). NUNCA rearmes la clave
    desde dpto/secc/segm/loc/vivienda/hogarviv: usá hogar_key, que ya viene materializada.
(c) VIVIENDAS: COUNT(*) sobre viviendas_1996 (1.126.502 en total). La condición de ocupación
    es condocup y tiene TRES grupos, no dos; no los mezcles:
      - OCUPADAS: condocup IN ('1','2') = 986.026. Son 943.794 con moradores presentes
        ('1') más 42.232 con moradores AUSENTES ('2'), que están ocupadas igual.
      - DESOCUPADAS: condocup IN ('3','4','5','6') = 140.476 (de temporada, en construcción,
        para alquiler o venta, y otra razón). ESTE es el filtro de "viviendas desocupadas".
      - No hay perdidos en condocup: los seis códigos suman el total.
    OJO: 1.126.502 - 943.794 = 182.708 NO son las desocupadas, son las viviendas SIN MORADORES
    PRESENTES (desocupadas + las ocupadas con moradores ausentes). Si te preguntan por
    desocupadas, la respuesta es 140.476; 182.708 solo vale si preguntan explícitamente por
    viviendas sin moradores presentes.
(d) PROHIBIDO unir personas_1996 con viviendas_1996. Si necesitás una variable de vivienda al
    analizar personas, usá la copia que ya está en personas_1996 (tipviv, tenencia, higienico,
    los artefactos del hogar, etc. viven en la tabla de personas).
(e) Los códigos PERDIDOS de cada variable (marcados abajo) van SIEMPRE excluidos de conteos,
    totales y denominadores. Los NULL también.
(f) UNIVERSOS: muchas variables sólo se preguntaron a un subconjunto (3 años o más, 12 años o
    más, mujeres de 15 o más, viviendas particulares ocupadas con moradores presentes). El
    universo está escrito en la etiqueta de cada variable: respetalo en el denominador.
(g) La geografía es por CÓDIGO TEXTO con ceros a la izquierda ('01', '020'). Para responder por
    NOMBRE, unir al nomenclátor: personas_1996.dpto = cod_departamentos.dpto ; para localidad,
    cod_localidades_1963_2023 ON (dpto, cod_1996 = loc).
(h) OCUPACIÓN: cota70 y ciuo88 son códigos; para narrar con descripción unir a las tablas
    cota70(codigo, descripcion) y cnuo96(codigo, descripcion). ciuo88='99999' es ocupación
    ignorada: excluilo.
Supresión de confidencialidad: SIEMPRE agregá COUNT(*) AS n_crudo por celda.
"""

CABECERA_2004 = """CENSO 2004 FASE 1 (Conteo de Población y Viviendas, INE Uruguay).
Censo de CONTEO, SIN ponderación: todas las cifras son conteos EXACTOS.
Releva SOLO geografía, vivienda, hogar, sexo y edad. NO tiene educación, ocupación,
migración ni ascendencia: si la pregunta requiere alguna de esas, devolvé NO_RESPONDIBLE.
Hay UNA sola tabla, censo2004, con una fila por PERSONA y banderas de registro:
(a) PERSONAS: COUNT(*) ... WHERE per=1   (3.241.003)
(b) HOGARES:  COUNT(*) ... WHERE hog=1   (1.065.677)
(c) VIVIENDAS:COUNT(*) ... WHERE viv=1   (1.279.741)
    Las tres banderas marcan exactamente una fila por unidad: NUNCA cuentes personas sin
    filtrar per=1, ni viviendas sin viv=1, porque la tabla repite los datos de la vivienda
    en cada persona.
(d) El 0 significa NO APLICA, no una categoría: tipviv_p=0 en viviendas colectivas,
    tipviv_c=0 en particulares, sexo=0 en filas que no son de persona. Excluilo.
(e) El código 9 en tipviv_p y desocupada es un centinela no documentado (sin dato): excluilo.
(f) La geografía es por CÓDIGO TEXTO con ceros a la izquierda ('01', '020'). Los nombres ya
    vienen en la tabla (nom_dpto, nom_loc, nom_barrio) en MAYÚSCULAS y SIN tildes.
Supresión de confidencialidad: SIEMPRE agregá COUNT(*) AS n_crudo por celda.
"""


class _Conexion:
    """Adaptador mínimo: expone `.execute()` como sqlite3 pero ejecuta por el
    ejecutor, o sea por DuckDB.

    Se hace así, y no reescribiendo cada función, porque las de este archivo
    reciben una conexión y hacen `cx.execute(...).__iter__()`. El adaptador
    devuelve tuplas, que es lo que ya esperaban. `PRAGMA table_info` sigue
    funcionando: DuckDB lo soporta igual."""

    def __init__(self, db):
        self.db = db

    def execute(self, sql):
        return ejecutor.tuplas(self.db, sql)

    def close(self):
        pass


def generar(censo, db, cabecera, tablas_hechos, nomenclator):
    cx = _Conexion(db)
    if censo == "1996":
        dv, dval, dperd = cargar_diccionario_1996(cx)
    else:
        dv, dval, dperd = cargar_diccionario_2004(cx, "censo2004")

    lineas = [cabecera]
    for t in tablas_hechos:
        lineas.append("\nTABLA %s:" % t)
        lineas += bloque_tabla(cx, t, dv, dval, dperd)
    lineas.append("\nNOMENCLÁTOR (solo para JOIN por nombre, nunca como tabla de hechos):")
    for t in nomenclator:
        lineas.append("- %s(%s)" % (t, ", ".join(columnas_de(cx, t))))

    ruta_esq = os.path.join(AQUI, "esquema_llm_%s.txt" % censo)
    with open(ruta_esq, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lineas) + "\n")

    cols = {t: columnas_de(cx, t) for t in list(tablas_hechos) + list(nomenclator)}
    ruta_cols = os.path.join(AQUI, "cols_%s.json" % censo)
    with open(ruta_cols, "w", encoding="utf-8") as fh:
        json.dump(cols, fh, ensure_ascii=False, indent=1)
    cx.close()

    n_cols = sum(len(v) for v in cols.values())
    print("%s: %s (%d líneas) y %s (%d columnas en %d tablas)"
          % (censo, os.path.basename(ruta_esq), len(lineas),
             os.path.basename(ruta_cols), n_cols, len(cols)))


def main():
    generar("1996", os.path.join(DATOS, "censo1996.db"), CABECERA_1996,
            ["personas_1996", "viviendas_1996"],
            ["cod_departamentos", "cod_localidades_1963_2023", "cod_localidad_1996",
             "cod_barrios_mvd", "cnuo96", "cota70"])
    generar("2004", os.path.join(DATOS, "censo2004.db"), CABECERA_2004,
            ["censo2004"],
            ["cod_departamentos", "cod_localidades_1963_2023", "ref_localidades_2004",
             "cod_barrios_mvd"])


if __name__ == "__main__":
    main()
