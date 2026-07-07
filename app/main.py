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

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from openai import OpenAI

from app import dicc
from app.sql_guard import (
    validar, suprimir_celdas_chicas, SQLNoSeguro, UMBRAL_SUPRESION, LIMITE_MAXIMO,
)
import consultar_2023   # motor Censo 2023 (ponderado): interfaz preguntar(texto)

DB_PATH = os.environ.get("CENSO_DB", "datos/censo.db")
MODELO = os.environ.get("CENSO_MODELO", "gpt-5.5")

# Línea fija que acompaña las cifras de PERSONAS del Censo 2023 (estimaciones del
# censo ponderado). Se agrega SOLO cuando la métrica es SUM(W) — no en viviendas
# ni hogares, que son conteos exactos.
PONDERACION_2023 = (
    "Cifras de personas del Censo 2023: estimaciones basadas en el censo ponderado del INE."
)
_RX_SUMW = re.compile(r"\bsum\s*\(\s*[^)]*\bw\b", re.I)

# Bounded timeout: without it, a hung/half-closed LLM response wedges the worker
# thread indefinitely and freezes the app (incident 2026-07-06). 60s per request
# (connect/read/write/pool) + bounded retries.
client = OpenAI(timeout=60.0, max_retries=2)  # requires OPENAI_API_KEY in environment
app = FastAPI(title="Habla con tu Censo")

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
- edad INTEGER: años cumplidos, 0 a 110. (Usá edad, no PERNA01.)
- asc_afro TEXT: 'Si' | 'No' | NULL. MENCIÓN de ascendencia afro o negra.
  "afrodescendiente"/"afro" = asc_afro='Si'. NULL = perdido. (Preferí sobre PERER01_1.)
- asc_principal TEXT: 'Afro o Negra','Asiática o Amarilla','Blanca','Indígena',
  'Otra','Ninguna' o NULL. Solo la responden quienes declararon MÁS DE UNA
  ascendencia; es DISTINTA de la mención: NO la uses para contar afrodescendientes.
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
  por localidades.nombre."""

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
o 9. En un porcentaje, el denominador debe excluir los perdidos de esa variable. NUNCA
uses la población total como denominador de una variable con perdidos.

PORCENTAJES: la métrica es el porcentaje (primera métrica del SELECT). Si querés que la
celda sea suprimible, agregá el conteo válido como columna aparte con alias AS n_validos
(no 'personas'/'hogares', para no confundir el mapa).

LOCALIDADES: preguntas por una localidad -> JOIN localidades por codloc y filtro por
localidades.nombre en MAYÚSCULAS y SIN tilde.

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


def generar_sql(pregunta: str) -> str:
    r = client.chat.completions.create(
        model=MODELO,
        max_completion_tokens=1000,
        messages=[
            {"role": "system", "content": PROMPT_SQL},
            {"role": "user", "content": pregunta},
        ],
    )
    return r.choices[0].message.content.strip()


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
    "asc_principal": ("asc_principal = ascendencia principal; SOLO la declaran quienes "
                      "mencionaron más de una ascendencia; NO sirve para contar afro."),
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
                       columnas_conteo: list, truncado: bool = False) -> str:
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
    r = client.chat.completions.create(
        model=MODELO,
        # El redactor solo NARRA (no razona): con el razonamiento por defecto de
        # gpt-5.5 las preguntas de mapa consumían todo el presupuesto y devolvían
        # respuesta VACÍA (finish=length). reasoning_effort='low' lo evita de raíz
        # (y baja costo/latencia); el tope holgado es margen, el modelo corta al terminar.
        reasoning_effort="low",
        max_completion_tokens=2000,
        messages=[
            {
                "role": "system",
                "content": (
                    "Respondé la pregunta del usuario usando EXCLUSIVAMENTE los datos "
                    "provistos. Si los datos no alcanzan, decilo. Citá la fuente: "
                    "'Censo 2011, INE Uruguay'. Sé breve y preciso.\n"
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
                    "Contexto metodológico (usalo solo si es pertinente): el Censo 2011 "
                    "fue el primer censo de derecho de Uruguay (cuenta a las personas en "
                    "su residencia habitual), con fecha de referencia 4 de octubre de 2011. "
                    "Población censada: 3.252.091; contabilizada (incluye 34.223 personas "
                    "imputadas en viviendas con moradores ausentes): 3.286.314; total "
                    "residente estimada (omisión 3,06%): 3.390.077. Los datos consultados "
                    "son los microdatos publicados, que pueden incluir personas imputadas."
                ),
            },
            {
                "role": "user",
                "content": f"Pregunta: {pregunta}\nSQL ejecutado: {sql}\nResultados: {filas}",
            },
        ],
    )
    return r.choices[0].message.content.strip() + nota


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
    sql_crudo = generar_sql(texto)

    if sql_crudo == "NO_RESPONDIBLE_VIVIENDAS":
        return {"ok": False, "respuesta": MENSAJE_VIVIENDAS_DESOCUPADAS}

    if sql_crudo == "NO_RESPONDIBLE":
        return {
            "ok": False,
            "respuesta": "Esa pregunta no puede responderse con las variables disponibles.",
        }

    sql_crudo = normalizar_departamentos(sql_crudo)

    try:
        sql_seguro, columnas_conteo = validar(sql_crudo)
    except SQLNoSeguro as e:
        # The guardrail fired: we do NOT execute, we do NOT improvise an answer.
        return {"ok": False, "respuesta": f"Consulta rechazada por seguridad: {e}"}

    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        filas = [dict(f) for f in con.execute(sql_seguro).fetchall()]

    # Si las filas devueltas alcanzan el tope del LIMIT, el resultado puede estar
    # recortado -> se avisa al redactor para que no narre extremos como universales (d).
    truncado = len(filas) >= LIMITE_MAXIMO

    filas, suprimidas = suprimir_celdas_chicas(filas, columnas_conteo)

    if not filas:
        return {
            "ok": False,
            "respuesta": (
                "La consulta no devolvió resultados publicables"
                + (" (celdas suprimidas por confidencialidad)." if suprimidas else ".")
            ),
            "sql": sql_seguro,
        }

    respuesta = {
        "ok": True,
        "respuesta": redactar_respuesta(texto, sql_seguro, filas, suprimidas, columnas_conteo, truncado),
        "sql": sql_seguro,   # transparency: the executed SQL is always shown
        "datos": filas,
        "celdas_suprimidas": suprimidas,
    }

    mapa = construir_mapa(sql_seguro, filas)
    if mapa:
        mapa["suprimidas"] = suprimidas   # suprimidas ya no están en datos
        respuesta["mapa"] = mapa

    return respuesta


@app.post("/preguntar")
def preguntar(p: Pregunta):
    """Interfaz pública única. Despacha al motor según el censo elegido en el
    selector del frontend (por defecto 2023)."""
    if p.censo == "2011":
        return responder_2011(p.texto)

    # Censo 2023 (ponderado). La línea de ponderación se agrega SOLO cuando la
    # métrica es SUM(W) (personas); viviendas y hogares son conteos exactos (regla c).
    r = consultar_2023.preguntar(p.texto)
    r.pop("veredicto", None)
    if r.get("ok") and _RX_SUMW.search(r.get("sql") or ""):
        r["respuesta"] = r.get("respuesta", "") + "\n\n_" + PONDERACION_2023 + "_"
    return r


@app.get("/")
def home():
    return FileResponse("app/static/index.html")
