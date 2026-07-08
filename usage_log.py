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
from datetime import datetime, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
# Ruta configurable; por defecto en logs/ (gitignored, fuera del árbol servido).
RUTA = os.environ.get("CENSO_USAGE_LOG", os.path.join(AQUI, "logs", "usage.jsonl"))
_LOCK = threading.Lock()


def registrar(censo, etapa, usage):
    """Agrega una línea con las métricas de una respuesta de la API OpenAI.

    `usage` es el objeto response.usage del SDK (o None). Solo métricas: NO se
    registra ningún texto de la pregunta ni de la respuesta.
    """
    try:
        pt = getattr(usage, "prompt_tokens", None)
        ct = getattr(usage, "completion_tokens", None)
        det = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(det, "cached_tokens", None) if det is not None else None
        linea = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "censo": censo,          # "2011" | "2023"
            "etapa": etapa,          # "sql" | "redactor"
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "cached_tokens": cached,
        }
        with _LOCK:
            os.makedirs(os.path.dirname(RUTA), exist_ok=True)
            with open(RUTA, "a", encoding="utf-8") as f:
                f.write(json.dumps(linea, ensure_ascii=False) + "\n")
    except Exception:
        # El logging de métricas NUNCA debe interrumpir una consulta.
        pass
