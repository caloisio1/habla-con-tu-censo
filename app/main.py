"""
main.py — Habla con tu Censo: natural-language interface over census microdata.

Flow:  question (ES) → LLM generates SQL → sql_guard validates (aggregate-only)
       → SQLite executes over microdata → small cells suppressed
       → LLM writes the answer citing the actual numbers returned.

The LLM never answers from memory and the user never sees individual records:
if a query fails validation or a cell is too small, the system says so.
"""

import os
import re
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import dicc
from app.sql_guard import (
    validar, suprimir_celdas_chicas, SQLNoSeguro, UMBRAL_SUPRESION, LIMITE_MAXIMO,
)
import consultar_2023   # motor Censo 2023 (ponderado): interfaz preguntar(texto)
import consultar_1996   # motor Censo 1996 (completo, sin ponderar)
import consultar_2004   # motor Censo 2004 Fase 1 (conteo, sin ponderar)

# Censos históricos: misma interfaz preguntar(texto), sin ponderación ni mapa.
MOTORES_HISTORICOS = {"1996": consultar_1996, "2004": consultar_2004}
import usage_log         # registro de métricas de tokens (solo métricas, sin contenido)
import registro          # rastro de las consultas rechazadas (pregunta + SQL + motivo)
from comun import llm, perdidos, pipeline, precalentar, rechazos, sinonimos  # módulo compartido

DB_PATH = os.environ.get("CENSO_DB", "datos/censo.db")
MODELO = os.environ.get("CENSO_MODELO", llm.MODELO_POR_DEFECTO)
# Configuración por ETAPA (igual que el motor 2023): el SQL razona (esfuerzo alto),
# el redactor solo narra (esfuerzo bajo). Vocabulario de gpt-5.5: none|low|medium|high.
MODELO_SQL = os.environ.get("CENSO_MODELO_SQL", MODELO)
MODELO_REDACTOR = os.environ.get("CENSO_MODELO_REDACTOR", MODELO)
ESFUERZO_SQL = os.environ.get("CENSO_ESFUERZO_SQL", "high")
ESFUERZO_REDACTOR = os.environ.get("CENSO_ESFUERZO_REDACTOR", "low")
TOPE_SQL = int(os.environ.get("CENSO_TOPE_SQL", "4000"))
TOPE_REDACTOR = int(os.environ.get("CENSO_TOPE_REDACTOR", "2000"))

# Línea fija que acompaña las cifras de PERSONAS del Censo 2023 (estimaciones del
# censo ponderado). Se agrega SOLO cuando la métrica es SUM(W) — no en viviendas
# ni hogares, que son conteos exactos.
PONDERACION_2023 = (
    "Cifras de personas del Censo 2023: estimaciones basadas en el censo ponderado del INE."
)
_RX_SUMW = re.compile(r"\bsum\s*\(\s*[^)]*\bw\b", re.I)

# El cliente (proveedor, clave, timeout acotado y reintentos) vive en comun/llm.py,
# compartido por los cuatro censos.


@asynccontextmanager
async def ciclo_de_vida(_app):
    """Al arrancar, deja las preguntas de ejemplo listas en la caché.

    Corre en segundo plano y nunca bloquea: si falla, el servicio atiende igual,
    solo que el primer clic en un chip paga el tiempo completo.
    """
    precalentar.arrancar({
        "1996": consultar_1996.preguntar,
        "2004": consultar_2004.preguntar,
        "2011": responder_2011,
        "2023": consultar_2023.preguntar,
    })
    yield


app = FastAPI(title="Habla con tu Censo", lifespan=ciclo_de_vida)

# Sirve los geojson de mapas (relativo a la página, funciona tras nginx /censo/).
app.mount("/static", StaticFiles(directory="app/static"), name="static")


class Pregunta(BaseModel):
    texto: str
    censo: str = "2023"   # censo por defecto de la interfaz pública


# Capa 1: columnas DERIVADAS legibles (semántica v3). El LLM las prefiere sobre
# las crudas equivalentes. Capa 2: las 145 variables crudas (dicc.esquema_variables()).
ESQUEMA_LEGIBLE = """Una fila = una persona censada (Censo 2011, INE Uruguay).

TABLA personas: 145 variables CRUDAS del INE (listadas abajo) + estas columnas
DERIVADAS, legibles, que DEBÉS PREFERIR sobre sus equivalentes crudas:
- departamento TEXT: 19 valores exactos, MAYÚSCULAS y SIN tildes:
  'MONTEVIDEO','ARTIGAS','CANELONES','CERRO LARGO','COLONIA','DURAZNO','FLORES',
  'FLORIDA','LAVALLEJA','MALDONADO','PAYSANDU','RIO NEGRO','RIVERA','ROCHA',
  'SALTO','SAN JOSE','SORIANO','TACUAREMBO','TREINTA Y TRES'. (Usá departamento, no DPTO.)
- sexo TEXT: 'Hombres' | 'Mujeres'. (Usá sexo, no PERPH02.)
- edad INTEGER: años cumplidos, 0 a 111. (Usá edad, no PERNA01.)
- asc_afro TEXT: 'Si' | 'No' | NULL. MENCIÓN de ascendencia afro o negra.
  "afrodescendiente"/"afro" = asc_afro='Si'. NULL = perdido. (Preferí sobre PERER01_1.)
- asc_principal TEXT: 'Afro o Negra','Asiática o Amarilla','Blanca','Indígena',
  'Otra','Ninguna' o NULL. Es la ascendencia que la persona eligió como PRINCIPAL:
  la responden TODAS las que declararon al menos una ascendencia (NULL = no declaró
  ninguna; 'Ninguna' = declaró varias y no eligió una principal). Es DISTINTA de la
  mención: NO la uses para contar afrodescendientes, para eso va asc_afro.
- nbi INTEGER: cantidad de NBI del HOGAR, 0..3 (3 = "3 o MÁS"; topeada, no hay 4).
  Se repite en cada integrante del hogar. NULL = perdido. (Preferí sobre NBI_CANTIDAD.)
- hogar_key TEXT: identificador de hogar (= ID_VIVIENDA || '-' || HOGID).
- vivienda_key TEXT: identificador de vivienda (= ID_VIVIENDA).
- codsec INTEGER: código de sección censal (departamento*100 + SECC). Válido en salida.
- codloc INTEGER: código de localidad (departamento*1000 + LOC). Para el JOIN con localidades.
- BARRIO85 TEXT: nombre del barrio, SOLO Montevideo ('Ciudad Vieja'). Válido en salida.
- CCZ INTEGER: centro comunal zonal, SOLO Montevideo. Válido en salida.

TABLA localidades(codloc INTEGER, nombre TEXT, departamento TEXT): referencia.
  nombre en MAYÚSCULAS y SIN tilde ('PASO DE LOS TOROS','BELLA UNION'). Preguntas por
  una localidad -> JOIN localidades ON personas.codloc = localidades.codloc y filtro
  por localidades.nombre.

TABLA paises(codigo INTEGER, nombre TEXT, nombre_oficial TEXT, alfa3 TEXT): nomenclátor
  de países del INE. nombre en MAYÚSCULAS sin acento ('PARAGUAY','VENEZUELA','ESPAÑA',
  'ARGENTINA','BRASIL','ESTADOS UNIDOS','ITALIA'). Se usa para las personas nacidas en el
  exterior (ver LUGAR DE NACIMIENTO).

LUGAR DE NACIMIENTO Y MIGRACIÓN INTERNA (bloque PERMI; el censo SÍ relevó lugar de nacimiento):
- PERMI01 = lugar de nacimiento: 1=en esta localidad, 2=en otra localidad del mismo
  departamento, 3=en otro departamento (del país), 4=en otro país, 8=no relevado.
- Nacidos en el EXTERIOR (otro país): WHERE PERMI01=4. El país está en el CÓDIGO PERMI01_4;
  para filtrar o desglosar por país hacé JOIN paises ON personas.PERMI01_4 = paises.codigo y
  usá paises.nombre (ej.: nacidos en Paraguay -> WHERE PERMI01=4 AND paises.nombre='PARAGUAY').
- DEPARTAMENTO de nacimiento: si PERMI01 IN (1,2) la persona nació en su departamento ACTUAL
  de residencia (columna departamento); si PERMI01=3 nació en OTRO departamento, cuyo código
  está en PERMI01_2 (texto con cero inicial). Códigos: 01=MONTEVIDEO 02=ARTIGAS 03=CANELONES
  04=CERRO LARGO 05=COLONIA 06=DURAZNO 07=FLORES 08=FLORIDA 09=LAVALLEJA 10=MALDONADO
  11=PAYSANDU 12=RIO NEGRO 13=RIVERA 14=ROCHA 15=SALTO 16=SAN JOSE 17=SORIANO 18=TACUAREMBO
  19=TREINTA Y TRES.
- PERMI06 = lugar de residencia anterior; PERMI07 = residencia 5 años antes (misma
  codificación 1/2/3/4; el código de país está en PERMI06_4 / PERMI07_4)."""

REGLAS = """Reglas estrictas:
- Devolvé SOLO la consulta SQL (dialecto SQLite), sin explicaciones ni markdown.
- Solo SELECT y SIEMPRE agregado: cada columna del SELECT externo es un agregado
  (COUNT/SUM/…) o una columna del GROUP BY. Nunca devuelvas filas individuales.
- Toda consulta debe incluir al menos un COUNT.

UNIDADES DE CONTEO (elegí el alias según lo que se pregunta):
- personas  -> COUNT(*) AS personas
- hogares   -> COUNT(DISTINCT hogar_key) AS hogares
- viviendas -> COUNT(DISTINCT vivienda_key) AS viviendas
Las variables de HOGAR (HOG*, nbi) y de VIVIENDA (VIV*) se REPITEN en cada integrante;
para contar hogares o viviendas es OBLIGATORIO COUNT(DISTINCT ...). hogar_key y
vivienda_key en el SELECT externo SOLO dentro de COUNT(DISTINCT ...); en subconsultas,
WHERE, JOIN y GROUP BY internos son libres.

PERDIDOS: los códigos anotados con NR/NC/IG/SD/SE (ver leyenda arriba) y los NULL son
PERDIDOS: EXCLUILOS SIEMPRE de conteos, totales y denominadores (filtrá esos códigos o
IS NOT NULL). Un código NO anotado con esas siglas es una categoría VÁLIDA aunque sea 8
o 9. Excepción que vale para TODAS las variables crudas de este censo, esté o no listada
abajo: el código 5555 es SECRETO ESTADÍSTICO y por lo tanto perdido — son 53 personas
con el cuestionario protegido, de las que solo valen departamento, sexo y el hogar al
que pertenecen; suman a la población total pero se excluyen de cualquier otro corte. En un porcentaje, el denominador debe excluir los perdidos de esa variable. NUNCA
uses la población total como denominador de una variable con perdidos.

PORCENTAJES: la métrica es el porcentaje (primera métrica del SELECT). Si querés que la
celda sea suprimible, agregá el conteo válido como columna aparte con alias AS n_validos
(no 'personas'/'hogares', para no confundir el mapa).

LOCALIDADES: preguntas por una localidad -> JOIN localidades por codloc y filtro por
localidades.nombre en MAYÚSCULAS y SIN tilde.

MIGRACIÓN Y LUGAR DE NACIMIENTO (usá el bloque PERMI y la tabla paises):
- "nacidos en <PAÍS>" -> JOIN paises ON personas.PERMI01_4 = paises.codigo
    WHERE PERMI01=4 AND paises.nombre='<PAÍS>' (nombre en MAYÚSCULAS sin acento).
- "nacidos en el exterior" (total) -> COUNT(*) WHERE PERMI01=4.
- "nacidos en el exterior por país" -> JOIN paises ... GROUP BY paises.nombre.
- "nacidos en el exterior por departamento" (de residencia) -> WHERE PERMI01=4 GROUP BY departamento.
- "nacidos en el departamento X que viven en Y" (matriz de migración interna):
    WHERE departamento='Y' AND ( (PERMI01 IN (1,2) AND 'Y'='X')
                                 OR (PERMI01=3 AND PERMI01_2='<código de X con cero inicial>') ).
    Ej. Rivera->Montevideo: WHERE departamento='MONTEVIDEO' AND PERMI01=3 AND PERMI01_2='13'.
- "personas que viven en un departamento distinto al que nacieron" (nacional) -> WHERE PERMI01=3.
  Estas preguntas SÍ se pueden responder: NO devuelvas NO_RESPONDIBLE.

PATRONES DE MAPA (desglose geográfico): la clave geográfica va como PRIMERA columna y la
métrica como segunda.
- "... por departamento"   -> GROUP BY departamento
- "... por sección censal" -> GROUP BY codsec
- "... por barrio"         -> GROUP BY BARRIO85 con WHERE departamento='MONTEVIDEO'
- "... por CCZ"            -> GROUP BY CCZ con WHERE departamento='MONTEVIDEO'
BARRIO85 y CCZ existen SOLO en Montevideo: barrio/CCZ de otro departamento -> NO_RESPONDIBLE.

FRECUENCIAS de una variable de hogar o vivienda:
- hogares por categoría -> COUNT(DISTINCT hogar_key) ... GROUP BY <var>
- personas que viven en hogares con esa característica -> COUNT(*)

PERID (número de persona dentro del hogar): "una fila por hogar" -> WHERE PERID=1.
Tamaño del hogar: preferí HOGPR01 (cantidad de personas en el hogar).

CONSULTAS JERÁRQUICAS (condición sobre OTROS miembros del hogar):
- "personas en hogares donde AL MENOS UN miembro cumple X" ->
    SELECT COUNT(*) AS personas FROM personas
    WHERE hogar_key IN (SELECT hogar_key FROM personas WHERE X)
- "hogares con AL MENOS N miembros que cumplen X" ->
    SELECT COUNT(DISTINCT hogar_key) AS hogares FROM personas WHERE hogar_key IN (
      SELECT hogar_key FROM personas WHERE X GROUP BY hogar_key HAVING COUNT(*) >= N)

Otras aclaraciones:
- nbi está topeada en 3 ("3 o más"); "más de 3 NBI" NO es respondible.
- afrodescendiente = asc_afro='Si'.
- Las variables crudas con nombre no ASCII (p. ej. "Años_estudio", "NBI_EDUCACIÓN")
  van entre comillas dobles.
- VIVIENDAS DESOCUPADAS: esta base son los microdatos de PERSONAS y solo contiene
  viviendas OCUPADAS (VIVVO03 = 1 o 2). Preguntas por viviendas DESOCUPADAS, vacantes,
  vacías o "para alquilar/vender" (VIVVO03 3-7) NO son respondibles con estos datos ->
  devolvé exactamente: NO_RESPONDIBLE_VIVIENDAS
- Si la pregunta no puede responderse con este esquema, devolvé exactamente: NO_RESPONDIBLE"""

PROMPT_SQL = f"""Sos un traductor de preguntas en español a SQL (dialecto SQLite) sobre \
el Censo 2011 de Uruguay.
{ESQUEMA_LEGIBLE}

VARIABLES CRUDAS DEL INE (nombre | etiqueta | códigos). Usá el CÓDIGO, no la etiqueta
(ej.: WHERE VIVVO03=3). Preferí las columnas derivadas de arriba cuando exista una equivalente.
{dicc.LEYENDA_PERDIDOS}
{dicc.esquema_variables()}

{REGLAS}

GLOSARIO (sinónimos de uso corriente, compartido por los cuatro censos): {sinonimos.glosario_para_prompt()}

{perdidos.bloque_para_prompt("2011")}
"""


MENSAJE_VIVIENDAS_DESOCUPADAS = (
    "Esta base contiene los microdatos de PERSONAS del Censo 2011, que solo "
    "incluyen viviendas ocupadas: no puedo contar viviendas desocupadas ni "
    "vacantes. El stock de viviendas desocupadas está en la base de VIVIENDAS "
    "del censo, que todavía no está cargada en este sistema. Sí puedo responder, "
    "en cambio, sobre viviendas ocupadas por departamento o por localidad."
)


_DEPTOS_CON_TILDE = {
    "PAYSANDÚ": "PAYSANDU",
    "RÍO NEGRO": "RIO NEGRO",
    "SAN JOSÉ": "SAN JOSE",
    "TACUAREMBÓ": "TACUAREMBO",
}


def normalizar_departamentos(sql: str) -> str:
    """Reemplaza nombres de departamento con tilde por su forma sin tilde
    (tal como estan en la base), en mayusculas y minusculas. Funcion pura:
    no altera un SQL que ya venga sin tildes."""
    for con_tilde, sin_tilde in _DEPTOS_CON_TILDE.items():
        sql = sql.replace(con_tilde, sin_tilde)
        sql = sql.replace(con_tilde.lower(), sin_tilde.lower())
    return sql


def generar_sql(pregunta: str, contexto: dict | None = None) -> str:
    r = llm.completar(
        modelo=MODELO_SQL,
        esfuerzo=ESFUERZO_SQL,
        tope=TOPE_SQL,
        sistema=PROMPT_SQL,
        usuario=pipeline.mensaje_usuario(pregunta, contexto),
    )
    usage_log.registrar("2011", "sql", r.uso, MODELO_SQL, ESFUERZO_SQL)
    return pipeline.sql_generado(r.texto)


# Semántica de las columnas DERIVADAS cuya codificación NO es obvia (topeadas,
# ascendencia). Es la MISMA información que ya usa el generador de SQL (ver
# ESQUEMA_LEGIBLE); se la damos al redactor para que narre sin malinterpretar los
# códigos ni "auditar" un SQL correcto. El diccionario crudo no la trae porque
# estas columnas son derivadas.
_SEMANTICA_DERIVADAS = {
    "nbi": ('nbi = cantidad de NBI del hogar, TOPEADA en 3: el valor 3 significa '
            '"3 o MÁS" (no existe 4). Es del hogar y se repite en cada integrante.'),
    "asc_afro": ("asc_afro = mención de ascendencia afro ('Si'/'No'/NULL); "
                 "afrodescendiente = 'Si'. NULL = perdido, no se cuenta."),
    "asc_principal": ("asc_principal = ascendencia que la persona eligió como principal; "
                      "la declaran todas las que mencionaron al menos una ascendencia "
                      "('Ninguna' = mencionó varias y no eligió una principal). NO sirve "
                      "para contar afrodescendientes: para eso va asc_afro, la mención."),
}


def unidad_conteo(columnas_conteo: list) -> str:
    """Unidad de análisis de la consulta según el alias de la columna de conteo
    (personas / hogares / viviendas). Para nombrar la nota de supresión y la
    narración con la unidad correcta, no 'personas' por defecto."""
    cols = {c.lower() for c in columnas_conteo}
    if "hogares" in cols:
        return "hogares"
    if "viviendas" in cols:
        return "viviendas"
    return "personas"


def leyenda_codificaciones(sql: str, columnas_conteo: list) -> str:
    """Leyenda COMPACTA de codificaciones para el redactor: SOLO las variables
    (derivadas y crudas) presentes en el SQL ejecutado. Filtrar por consulta
    mantiene acotado el costo en tokens (no se inyectan las 145 variables)."""
    sql_low = sql.lower()
    lineas = [
        "Unidad de conteo: personas=COUNT(*); hogares=COUNT(DISTINCT hogar_key); "
        "viviendas=COUNT(DISTINCT vivienda_key). Alias de conteo de esta consulta: "
        + (", ".join(columnas_conteo) or "n/d") + "."
    ]
    for col, txt in _SEMANTICA_DERIVADAS.items():
        if re.search(rf"\b{col}\b", sql_low):
            lineas.append(txt)
    presentes = [v["nombre"] for v in dicc.variables()
                 if re.search(rf"\b{re.escape(v['nombre'].lower())}\b", sql_low)]
    crudas = dicc.leyenda_de(presentes)
    if crudas:
        lineas.append(crudas)
    return "\n".join(lineas)


def redactar_respuesta(pregunta: str, sql: str, filas: list, suprimidas: int,
                       columnas_conteo: list, truncado: bool = False,
                       interpretaciones=(), contexto: dict | None = None) -> str:
    unidad = unidad_conteo(columnas_conteo)
    nota = (
        f"\nNota: {suprimidas} celda(s) con menos de {UMBRAL_SUPRESION} {unidad} "
        "fueron suprimidas por confidencialidad estadística."
        if suprimidas else ""
    )
    # (d) Si el resultado quedó recortado por el LIMIT, el redactor NO debe presentar
    # extremos (máximo/mínimo/único) como si fueran del universo completo.
    aviso_trunc = (
        "\nATENCIÓN: los resultados están RECORTADOS por un límite de filas (cláusula "
        "LIMIT): NO son el universo completo. No afirmes que un valor es el máximo, el "
        "mínimo, el mayor, el menor ni el único; describí solo lo que muestran las filas."
        if truncado else ""
    )
    r = llm.completar(
        modelo=MODELO_REDACTOR,
        # El redactor solo NARRA (no razona): con el razonamiento activado las
        # preguntas de mapa consumían todo el presupuesto y devolvían respuesta
        # VACÍA (corte por tope). Esfuerzo 'none' lo evita de raíz (y baja
        # costo/latencia); el tope holgado es margen, el modelo corta al terminar.
        esfuerzo=ESFUERZO_REDACTOR,
        tope=TOPE_REDACTOR,
        sistema=(
            "Respondé la pregunta del usuario usando EXCLUSIVAMENTE los datos "
            "provistos. Si los datos no alcanzan, decilo. Citá la fuente: "
            "'Censo 2011, INE Uruguay'. Sé breve y preciso.\n"
            "CÓDIGOS: cuando venga la leyenda de codificaciones, nombrá SIEMPRE la "
            "etiqueta y nunca el número pelado. Esa leyenda es el diccionario de la "
            "variable: ya lo tenés acá. NO inventes limitaciones —no digas que no "
            "revisaste el diccionario ni que te falta el codebook— y no ofrezcas buscar "
            "archivos ni 'lanzar otra consulta': no ejecutás nada, solo narrás lo que ya "
            "está en este mensaje.\n"
            "Tu función es NARRAR los resultados. NO auditás, corregís ni "
            "critiques la consulta SQL: asumila correcta y contá lo que devolvió.\n"
            "MAPAS: si la consulta agrupa por una unidad geográfica (departamento, "
            "sección censal, barrio, CCZ), el frontend DIBUJA el mapa coroplético "
            "automáticamente. NUNCA digas que no podés mostrar un mapa ni que faltan "
            "geometrías: el mapa se muestra solo.\n"
            f"UNIDAD DE ANÁLISIS de esta consulta: {unidad}. Nombrá esa unidad "
            "(personas, hogares o viviendas) al narrar; no digas 'personas' por defecto.\n"
            "NO escribas ninguna nota, aclaración ni frase sobre celdas suprimidas, "
            "confidencialidad o secreto estadístico: el sistema agrega esa nota "
            "automáticamente al final; no la escribas vos ni la repitas.\n"
            "Formato de las cifras (español rioplatense): separador de miles con PUNTO —escribí 323.114, nunca 323114— y decimales con coma. NO le pongas separador a los años ('Censo 2023', no 'Censo 2.023') ni a los códigos de sección, localidad o barrio.\n"
            "PRESENTACIÓN: si los resultados traen MÁS DE UNA FILA, presentalos SIEMPRE en una TABLA markdown (encabezado + una fila por categoría), NUNCA como lista con viñetas ni enumerados en prosa. Con una sola fila, narrala en una oración.\n"
            "NOMBRES PROPIOS: en la base los departamentos, localidades y barrios están en MAYÚSCULAS y sin tildes; escribilos con mayúscula inicial y acentuación correcta —Montevideo, Paysandú, Río Negro, San José, Tacuarembó, Treinta y Tres, Cerro Largo, Paso de los Toros, Bella Unión—, nunca en mayúsculas sostenidas. Las preposiciones y artículos internos van en minúscula (Paso de los Toros, Treinta y Tres).\n"
            "Las cifras del Censo 2011 son CONTEOS EXACTOS de los microdatos: NO uses "
            "'aproximadamente', 'alrededor de', 'unos/unas' ni 'estimación' para "
            "presentarlas (fuera del contexto metodológico general de omisión censal).\n"
            f"{aviso_trunc}\n"
            "Si el universo de la consulta excluye perdidos (valores NULL: "
            "no relevado, viviendas colectivas o secreto estadístico), aclaralo "
            "explícitamente (ej.: 'sobre N personas con respuesta válida'). "
            "Nunca presentes un porcentaje como si el denominador fuera toda la "
            "población cuando la variable tiene perdidos.\n"
            "CODIFICACIONES de esta consulta (respetalas al narrar; p. ej. una "
            "variable topeada en 3 significa '3 o más'):\n"
            + leyenda_codificaciones(sql, columnas_conteo) + "\n"
            + perdidos.leyenda_para_redactor("2011", sql) + "\n"
            "Contexto metodológico (usalo solo si es pertinente): el Censo 2011 "
            "fue el primer censo de derecho de Uruguay (cuenta a las personas en "
            "su residencia habitual), con fecha de referencia 4 de octubre de 2011. "
            "Población censada: 3.252.091; contabilizada (incluye 34.223 personas "
            "imputadas en viviendas con moradores ausentes): 3.286.314; total "
            "residente estimada (omisión 3,06%): 3.390.077. Los datos consultados "
            "son los microdatos publicados, que pueden incluir personas imputadas."
            + pipeline.instruccion_redactor(interpretaciones, contexto)
        ),
        usuario=f"Pregunta: {pregunta}\nSQL ejecutado: {sql}\nResultados: {filas}"
                + pipeline.totales_para_redactor(filas, columnas_conteo),
    )
    usage_log.registrar("2011", "redactor", r.uso, MODELO_REDACTOR, ESFUERZO_REDACTOR)
    texto = pipeline.asegurar_declaracion(r.texto, interpretaciones, contexto)
    return texto + nota


# Columna geográfica del GROUP BY -> nivel de mapa. Orden = prioridad.
NIVELES_MAPA = [
    ("departamento", "departamento"),
    ("codsec", "seccion"),
    ("barrio85", "barrio"),
    ("ccz", "ccz"),
]


def _es_numero(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def construir_mapa(sql: str, filas: list) -> dict | None:
    """Si el SQL agrupa por una unidad geográfica mapeable, devuelve
    {"nivel", "datos": [{"clave", "valor"}]} a partir de las filas YA
    suprimidas. 'valor' es la columna numérica principal (conteo o %)."""
    if not filas:
        return None
    m = re.search(r"group\s+by\s+(.+?)(?:\s+order\s+by\b|\s+limit\b|$)",
                  sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    clausula = m.group(1).lower()
    col = nivel = None
    for c, n in NIVELES_MAPA:
        if re.search(rf"\b{c}\b", clausula):
            col, nivel = c, n
            break
    if not col:
        return None

    ejemplo = filas[0]
    clave_key = next((k for k in ejemplo if k.lower() == col), None)
    if clave_key is None:
        return None
    valor_key = None
    for pref in ("personas", "hogares"):
        if pref in ejemplo and _es_numero(ejemplo[pref]):
            valor_key = pref
            break
    if valor_key is None:
        valor_key = next(
            (k for k, v in ejemplo.items() if k != clave_key and _es_numero(v)),
            None,
        )
    if valor_key is None:
        return None

    datos = [{"clave": f[clave_key], "valor": f[valor_key]} for f in filas]
    return {"nivel": nivel, "datos": datos}


def responder_2011(texto: str) -> dict:
    """Pipeline del motor 2011 (lo usa el servicio unificado cuando el selector elige 2011).
    Abre censo.db en SOLO LECTURA (la app nunca escribe la base)."""
    # Indicador ambiguo o variable no relevada: respuesta barata, sin llamar al modelo.
    corta, contexto = pipeline.antes(texto, "2011")
    if corta is not None:
        registro.no_respondible("2011", texto, corta.get("motivo", "ambigua"))
        return corta

    # Nivel A de caché: ahorra la llamada que razona. El guard corre igual.
    sql_crudo = pipeline.sql_cacheado(texto, "2011", contexto)
    if sql_crudo is None:
        sql_crudo = generar_sql(texto, contexto)
        pipeline.recordar_sql(texto, "2011", contexto, sql_crudo)

    if sql_crudo == "NO_RESPONDIBLE_VIVIENDAS":
        registro.no_respondible("2011", texto, "viviendas desocupadas")
        return {"ok": False, "respuesta": MENSAJE_VIVIENDAS_DESOCUPADAS}

    if sql_crudo == "NO_RESPONDIBLE":
        registro.no_respondible("2011", texto)
        return {
            "ok": False,
            "respuesta": "Esa pregunta no puede responderse con las variables disponibles.",
        }

    sql_crudo = normalizar_departamentos(sql_crudo)

    # Entidades nombradas: resuelve, reescribe o pregunta (nunca "confidencialidad").
    try:
        sql_crudo, interpretaciones, alternativas = pipeline.sobre_sql(
            sql_crudo, "2011", texto)
    except pipeline.EntidadNoResuelta as e:
        registro.no_respondible("2011", texto, e.rechazo.codigo)
        return rechazos.a_respuesta(e.rechazo, sql=None)

    try:
        sql_seguro, columnas_conteo = validar(sql_crudo)
    except SQLNoSeguro as e:
        # The guardrail fired: we do NOT execute, we do NOT improvise an answer.
        registro.rechazo("2011", texto, e, sql_crudo)
        return {"ok": False, "respuesta": f"Consulta rechazada por seguridad: {e}"}

    # Nivel B: mismo SQL ya ejecutado y redactado -> ni base ni redactor.
    listo = pipeline.resultado_cacheado(sql_seguro, "2011", alternativas)
    if listo is not None:
        return dict(listo, sql=sql_seguro)

    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        filas = [dict(f) for f in con.execute(sql_seguro).fetchall()]

    # Si las filas devueltas alcanzan el tope del LIMIT, el resultado puede estar
    # recortado -> se avisa al redactor para que no narre extremos como universales (d).
    truncado = len(filas) >= LIMITE_MAXIMO

    # Supresión con la regla corregida: el conteo CERO no es confidencialidad.
    filas, suprimidas, vacias, rechazo = pipeline.sobre_filas(
        filas, columnas_conteo, unidad_conteo(columnas_conteo))
    if rechazo is not None:
        return dict(rechazos.a_respuesta(rechazo, sql=sql_seguro),
                    celdas_suprimidas=suprimidas)

    respuesta = {
        "ok": True,
        "respuesta": redactar_respuesta(texto, sql_seguro, filas, suprimidas, columnas_conteo,
                                        truncado, interpretaciones, contexto),
        "sql": sql_seguro,   # transparency: the executed SQL is always shown
        "datos": filas,
        "celdas_suprimidas": suprimidas,
    }

    # Otras lecturas posibles del nombre consultado: se ofrecen junto a la respuesta.
    if alternativas:
        respuesta["opciones"] = alternativas

    mapa = construir_mapa(sql_seguro, filas)
    if mapa:
        mapa["suprimidas"] = suprimidas   # suprimidas ya no están en datos
        respuesta["mapa"] = mapa

    pipeline.recordar_resultado(sql_seguro, "2011", respuesta)
    return respuesta


@app.post("/preguntar")
def preguntar(p: Pregunta):
    """Interfaz pública única. Despacha al motor según el censo elegido en el
    selector del frontend (por defecto 2023).

    Cualquier excepción se traduce a una respuesta JSON con ok=False. Si sube tal
    cual, FastAPI devuelve 500 con un cuerpo de TEXTO plano, el fetch del
    frontend no lo puede parsear y cae en su catch genérico: el usuario ve
    "Hubo un problema de conexión" para cosas que no son de conexión (un timeout
    del modelo, una consulta inválida). Perdíamos el motivo real, que además
    quedaba solo en el journal.
    """
    try:
        return _responder(p)
    except Exception as e:                      # noqa: BLE001 - la frontera pública
        nombre = type(e).__name__
        if "Timeout" in nombre or "timed out" in str(e).lower():
            texto = ("La consulta tardó demasiado y se cortó. Suele pasar con "
                     "preguntas muy abiertas: probá acotarla (un departamento, "
                     "una localidad, un año) y volvé a intentar.")
        else:
            texto = ("No se pudo completar la consulta. Probá reformular la "
                     "pregunta o intentar de nuevo en unos segundos.")
        try:
            registro.rechazo(p.censo, p.texto, "excepcion:%s" % nombre, "")
        except Exception:
            pass
        print("ERROR /preguntar [%s] %s: %s" % (p.censo, nombre, e), flush=True)
        return {"ok": False, "respuesta": texto, "motivo": nombre}


def _responder(p: Pregunta):
    if p.censo == "2011":
        return responder_2011(p.texto)

    # 1996 y 2004: censos completos sin ponderación, conteos exactos. Sí devuelven
    # mapa: departamento, barrio de Montevideo, sección censal y —desde el
    # 29-jul-2026, con cartografía propia reconstruida de las planchas del INE y
    # acotada a las localidades completas— segmento censal (ver motor_historico).
    if p.censo in MOTORES_HISTORICOS:
        r = MOTORES_HISTORICOS[p.censo].preguntar(p.texto)
        r.pop("veredicto", None)
        return r

    # Censo 2023 (ponderado). La línea de ponderación se agrega SOLO cuando la
    # métrica es SUM(W) (personas); viviendas y hogares son conteos exactos (regla c).
    r = consultar_2023.preguntar(p.texto)
    r.pop("veredicto", None)
    if r.get("ok") and _RX_SUMW.search(r.get("sql") or ""):
        r["respuesta"] = r.get("respuesta", "") + "\n\n_" + PONDERACION_2023 + "_"
    return r


INDEX = "app/static/index.html"
VERSIONADOS = (
    "app/static/censo.css",
    "app/static/dicc/dicc_2023.json",   # si cambia el diccionario, cambia el ?v=
    "app/static/logo_ine.png",
    "app/static/logo_censo.png",
    "app/static/logo_censo_dark.png",
)


def _version_estaticos() -> str:
    """Sello de versión de los estáticos: el mtime más reciente del CSS y del logo.

    Sin esto el navegador aplica frescura heurística (no hay Cache-Control) y
    puede servir una hoja de estilos vieja durante días después de un cambio de
    diseño. El sello viaja como ?v= en el HTML, así una versión nueva es una URL
    nueva y el navegador la pide sí o sí.
    """
    marcas = []
    for f in VERSIONADOS:
        try:
            marcas.append(int(os.path.getmtime(f)))
        except OSError:
            pass
    return str(max(marcas)) if marcas else "0"


@app.get("/")
def home():
    with open(INDEX, encoding="utf-8") as fh:
        html = fh.read().replace("__V__", _version_estaticos())
    # no-cache = el navegador puede guardarlo, pero revalida siempre (304 barato).
    # El HTML es el índice del diseño: si queda pegado, no hay ?v= que lo salve.
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
