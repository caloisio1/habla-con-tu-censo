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
from pydantic import BaseModel
from openai import OpenAI

from app.sql_guard import (
    validar, suprimir_celdas_chicas, SQLNoSeguro, UMBRAL_SUPRESION,
)

DB_PATH = os.environ.get("CENSO_DB", "datos/censo.db")
MODELO = os.environ.get("CENSO_MODELO", "gpt-5.5")

client = OpenAI()  # requires OPENAI_API_KEY in environment
app = FastAPI(title="Habla con tu Censo")


class Pregunta(BaseModel):
    texto: str


ESQUEMA = """
Tabla de MICRODATOS: personas(departamento TEXT, sexo TEXT, edad INTEGER,
                              asc_afro TEXT, asc_principal TEXT, nbi INTEGER,
                              hogar_key TEXT, SECC TEXT, LOC TEXT,
                              BARRIO85 TEXT, CCZ INTEGER,
                              codsec INTEGER, codloc INTEGER)
Tabla de REFERENCIA: localidades(codloc INTEGER, nombre TEXT, departamento TEXT)
- Una fila = una persona censada (Censo 2011, INE Uruguay).
- departamento: uno de estos 19 valores exactos (siempre en mayúsculas y SIN tildes):
  'MONTEVIDEO','ARTIGAS','CANELONES','CERRO LARGO','COLONIA','DURAZNO',
  'FLORES','FLORIDA','LAVALLEJA','MALDONADO','PAYSANDU','RIO NEGRO',
  'RIVERA','ROCHA','SALTO','SAN JOSE','SORIANO','TACUAREMBO',
  'TREINTA Y TRES'
- sexo: 'Hombres' | 'Mujeres'
- edad: edad en años cumplidos (0 a 110)
- asc_afro: 'Si' | 'No' | NULL. Es la MENCIÓN de ascendencia afro o negra.
  NULL = no relevado o imputado. "Afrodescendiente"/"afro" a secas = asc_afro='Si'.
- asc_principal: ascendencia principal declarada. Valores: 'Afro o Negra',
  'Asiática o Amarilla','Blanca','Indígena','Otra','Ninguna', o NULL.
  Solo la responden quienes declararon MÁS DE UNA ascendencia; es una pregunta
  DISTINTA de la mención (asc_afro). No la uses para contar afrodescendientes.
- nbi: cantidad de Necesidades Básicas Insatisfechas del HOGAR (se repite en
  cada integrante del hogar). Valores 0,1,2,3 donde 3 = "3 o MÁS" (variable
  topeada: NO existe 4 ni más). NULL = no relevado / vivienda colectiva /
  secreto estadístico del INE. Preguntas por "más de 3 NBI" NO son respondibles.
- hogar_key: identificador de hogar. SOLO puede aparecer dentro de
  COUNT(DISTINCT hogar_key). No lo pongas en SELECT, WHERE, GROUP BY ni ORDER BY.
- codsec: código de sección censal (departamento*100 + sección). Válido en salida.
- codloc: código de localidad (departamento*1000 + localidad). Se usa para el
  JOIN con localidades: personas.codloc = localidades.codloc.
- BARRIO85: nombre del barrio, SOLO Montevideo (capitalización tipo
  'Ciudad Vieja'). Válido en salida.
- CCZ: número de centro comunal zonal, SOLO Montevideo. Válido en salida.
- localidades.nombre: nombre de la localidad en MAYÚSCULAS y SIN tilde
  (ej: 'PASO DE LOS TOROS', 'BELLA UNION'). Preguntas por una localidad se
  responden con JOIN localidades ON personas.codloc = localidades.codloc y
  filtro por localidades.nombre.
"""

PROMPT_SQL = f"""Sos un traductor de preguntas en español a SQL (dialecto SQLite).
Esquema disponible:
{ESQUEMA}
Reglas estrictas:
- Devolvé SOLO la consulta SQL, sin explicaciones ni markdown.
- Solo SELECT, y SIEMPRE agregado. Nunca devuelvas filas individuales.
- UNIDADES y ALIAS OBLIGATORIOS:
  * Preguntas sobre PERSONAS -> COUNT(*) AS personas
  * Preguntas sobre HOGARES  -> COUNT(DISTINCT hogar_key) AS hogares
    (obligatorio para hogares porque nbi se repite en cada integrante del hogar).
- PERDIDOS: los NULL se excluyen SIEMPRE de conteos, totales y denominadores.
  Al calcular proporciones de una variable con perdidos, el denominador debe
  filtrar sus NULL. Ej: % afro = personas con asc_afro='Si' sobre personas con
  asc_afro IS NOT NULL. NUNCA uses la población total como denominador de una
  variable que tiene perdidos.
- Podés usar WHERE (edad, departamento, sexo, asc_afro, asc_principal, nbi,
  codsec, BARRIO85, CCZ, codloc), GROUP BY y ORDER BY.
- LOCALIDADES: preguntas por una localidad (ej. "Paso de los Toros", "Bella
  Unión") -> JOIN localidades ON personas.codloc = localidades.codloc y filtrá
  por localidades.nombre en MAYÚSCULAS y SIN tilde. Las localidades SÍ son
  respondibles.
- PATRONES DE MAPA (cuando la pregunta pida un desglose geográfico):
  * "... por departamento"    -> GROUP BY departamento
  * "... por sección censal"  -> GROUP BY codsec
  * "... por barrio"          -> GROUP BY BARRIO85 con WHERE departamento='MONTEVIDEO'
  * "... por CCZ"             -> GROUP BY CCZ con WHERE departamento='MONTEVIDEO'
  En estos casos poné la clave geográfica (departamento / codsec / BARRIO85 / CCZ)
  como PRIMERA columna del SELECT y la métrica (conteo o porcentaje) como segunda.
- BARRIO85 y CCZ existen SOLO en Montevideo: preguntas de barrio o CCZ para otro
  departamento -> devolvé exactamente NO_RESPONDIBLE.
- Si la pregunta no puede responderse con este esquema, devolvé exactamente: NO_RESPONDIBLE
"""


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


def redactar_respuesta(pregunta: str, sql: str, filas: list, suprimidas: int) -> str:
    nota = (
        f"\nNota: {suprimidas} celda(s) con menos de {UMBRAL_SUPRESION} personas "
        "fueron suprimidas por confidencialidad estadística."
        if suprimidas else ""
    )
    r = client.chat.completions.create(
        model=MODELO,
        max_completion_tokens=1000,
        messages=[
            {
                "role": "system",
                "content": (
                    "Respondé la pregunta del usuario usando EXCLUSIVAMENTE los datos "
                    "provistos. Si los datos no alcanzan, decilo. Citá la fuente: "
                    "'Censo 2011, INE Uruguay'. Sé breve y preciso.\n"
                    "Si el universo de la consulta excluye perdidos (valores NULL: "
                    "no relevado, viviendas colectivas o secreto estadístico), aclaralo "
                    "explícitamente (ej.: 'sobre N personas con respuesta válida'). "
                    "Nunca presentes un porcentaje como si el denominador fuera toda la "
                    "población cuando la variable tiene perdidos.\n"
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
                "content": f"Pregunta: {pregunta}\nSQL ejecutado: {sql}\nResultados: {filas}{nota}",
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


@app.post("/preguntar")
def preguntar(p: Pregunta):
    sql_crudo = generar_sql(p.texto)

    if sql_crudo == "NO_RESPONDIBLE":
        return {
            "ok": False,
            "respuesta": "Esa pregunta no puede responderse con las variables disponibles.",
        }

    sql_crudo = normalizar_departamentos(sql_crudo)

    try:
        sql_seguro = validar(sql_crudo)
    except SQLNoSeguro as e:
        # The guardrail fired: we do NOT execute, we do NOT improvise an answer.
        return {"ok": False, "respuesta": f"Consulta rechazada por seguridad: {e}"}

    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        filas = [dict(f) for f in con.execute(sql_seguro).fetchall()]

    filas, suprimidas = suprimir_celdas_chicas(filas)

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
        "respuesta": redactar_respuesta(p.texto, sql_seguro, filas, suprimidas),
        "sql": sql_seguro,   # transparency: the executed SQL is always shown
        "datos": filas,
        "celdas_suprimidas": suprimidas,
    }

    mapa = construir_mapa(sql_seguro, filas)
    if mapa:
        mapa["suprimidas"] = suprimidas   # suprimidas ya no están en datos
        respuesta["mapa"] = mapa

    return respuesta


@app.get("/")
def home():
    return FileResponse("app/static/index.html")
