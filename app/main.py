"""
main.py — Habla con tu Censo: natural-language interface over census microdata.

Flow:  question (ES) → LLM generates SQL → sql_guard validates (aggregate-only)
       → SQLite executes over microdata → small cells suppressed
       → LLM writes the answer citing the actual numbers returned.

The LLM never answers from memory and the user never sees individual records:
if a query fails validation or a cell is too small, the system says so.
"""

import os
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
                              hogar_key TEXT)
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
- Podés usar WHERE (edad, departamento, sexo, asc_afro, asc_principal, nbi),
  GROUP BY y ORDER BY.
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

    return {
        "ok": True,
        "respuesta": redactar_respuesta(p.texto, sql_seguro, filas, suprimidas),
        "sql": sql_seguro,   # transparency: the executed SQL is always shown
        "datos": filas,
        "celdas_suprimidas": suprimidas,
    }


@app.get("/")
def home():
    return FileResponse("app/static/index.html")
