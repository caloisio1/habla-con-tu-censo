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
Tabla de MICRODATOS: personas(departamento TEXT, sexo TEXT, edad INTEGER)
- Una fila = una persona censada (Censo 2011, INE Uruguay).
- departamento: los 19 departamentos, en mayúsculas (ej: 'MONTEVIDEO', 'SALTO')
- sexo: 'Hombres' | 'Mujeres'
- edad: edad en años cumplidos (0 a 110)
"""

PROMPT_SQL = f"""Sos un traductor de preguntas en español a SQL (dialecto SQLite).
Esquema disponible:
{ESQUEMA}
Reglas estrictas:
- Devolvé SOLO la consulta SQL, sin explicaciones ni markdown.
- Solo SELECT, y SIEMPRE agregado: usá COUNT(*) AS personas. Nunca devuelvas filas individuales.
- Podés usar WHERE sobre edad (ej: edad >= 75), GROUP BY y ORDER BY.
- Si la pregunta no puede responderse con este esquema, devolvé exactamente: NO_RESPONDIBLE
"""


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
