"""usage_log.py — registro de MÉTRICAS de tokens por llamada al LLM.

Cada llamada (SQL o redactor, censo 2011 o 2023) agrega una línea JSONL con
timestamp, censo, etapa y los tres contadores de tokens (prompt / completion /
cacheados). NUNCA se escribe la pregunta del usuario ni la respuesta: este log
es para MEDIR COSTO, no para auditar contenido. El logging jamás debe romper
una consulta del usuario: cualquier error se traga en silencio.
"""
import os
import json
import threading
import time
import uuid
import contextvars
from datetime import datetime, timezone

# Contexto de la consulta en curso. Se fija en app/main.py:_responder(), que es el
# punto único por donde pasan los cuatro censos, y lo leen registrar() y cerrar().
# Un hilo nuevo arranca con contexto vacío: por eso se fija DENTRO del hilo que
# corre el pipeline (el de /preguntar_stream), no en el que atiende el request.
_CONSULTA = contextvars.ContextVar("consulta_censo", default=None)

AQUI = os.path.dirname(os.path.abspath(__file__))
# Ruta configurable; por defecto en logs/ (gitignored, fuera del árbol servido).
RUTA = os.environ.get("CENSO_USAGE_LOG", os.path.join(AQUI, "logs", "usage.jsonl"))
_LOCK = threading.Lock()


def iniciar(censo):
    """Abre una consulta de usuario y le da un id. Devuelve el id."""
    _CONSULTA.set({"id": uuid.uuid4().hex, "censo": censo, "cache": [], "t0": time.time()})
    return _CONSULTA.get()["id"]


def marcar_cache(nivel):
    """Anota un acierto de caché ('A' o 'B') en la consulta en curso."""
    c = _CONSULTA.get()
    if c is not None and nivel not in c["cache"]:
        c["cache"].append(nivel)


def cache_de_la_consulta():
    """Niveles de caché que acertaron en la consulta en curso ('A', 'B', 'AB') o None."""
    c = _CONSULTA.get()
    return ("".join(c["cache"]) or None) if c else None


def cerrar(resultado, veredicto=None):
    """Cierra la consulta con una línea `etapa="fin"`, SIEMPRE, haya habido
    llamadas al modelo o no.

    Ésta es la línea que hace medible el costo por PREGUNTA DE USUARIO: un acierto
    de caché de nivel A no genera ninguna llamada al modelo y antes desaparecía del
    log entero, así que el denominador quedaba incompleto. Ahora
    costo por pregunta = suma de las líneas con tokens / cantidad de líneas "fin".
    """
    c = _CONSULTA.get()
    if c is None:
        return
    try:
        _escribir({
            "ts": datetime.now(timezone.utc).isoformat(),
            "consulta_id": c["id"],
            "censo": c["censo"],
            "etapa": "fin",
            "resultado": resultado,      # ok | cache_a | cache_b | rechazada | no_respondible | error
            "veredicto": veredicto,      # código de control tal cual lo dio el motor
            "cache": "".join(c["cache"]) or None,
            "seg": round(time.time() - c["t0"], 2),
        })
    finally:
        _CONSULTA.set(None)


def _escribir(linea):
    with _LOCK:
        os.makedirs(os.path.dirname(RUTA), exist_ok=True)
        with open(RUTA, "a", encoding="utf-8") as f:
            f.write(json.dumps(linea, ensure_ascii=False) + "\n")


def registrar(censo, etapa, usage, modelo=None, esfuerzo=None):
    """Agrega una línea con las métricas de una respuesta de la API OpenAI.

    `usage` es el objeto response.usage del SDK (o None). Solo métricas: NO se
    registra ningún texto de la pregunta ni de la respuesta. `modelo` y `esfuerzo`
    (reasoning_effort) quedan asentados para poder costear cada etapa por separado.
    """
    try:
        pt = getattr(usage, "prompt_tokens", None)
        ct = getattr(usage, "completion_tokens", None)
        det = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(det, "cached_tokens", None) if det is not None else None
        det_c = getattr(usage, "completion_tokens_details", None)
        razon = getattr(det_c, "reasoning_tokens", None) if det_c is not None else None
        c = _CONSULTA.get()
        linea = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "consulta_id": c["id"] if c else None,   # une las llamadas de UNA pregunta
            "censo": censo,          # "2011" | "2023"
            "etapa": etapa,          # "sql" | "redactor"
            "modelo": modelo,        # id del modelo usado en esta etapa
            "esfuerzo": esfuerzo,    # reasoning_effort de esta etapa
            "prompt_tokens": pt,
            "completion_tokens": ct,     # incluye los de razonamiento
            "reasoning_tokens": razon,   # subconjunto de completion_tokens
            "cached_tokens": cached,
        }
        _escribir(linea)
    except Exception:
        # El logging de métricas NUNCA debe interrumpir una consulta.
        pass
