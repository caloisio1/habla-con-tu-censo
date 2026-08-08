"""comun/llm.py — Acceso único al LLM para los cuatro censos.

Los cuatro motores (1996, 2004, 2011, 2023) hacen exactamente la misma llamada en
dos etapas: el SQL RAZONA (esfuerzo alto, porque traducir la pregunta al esquema
es el paso difícil) y el redactor solo NARRA los resultados ya calculados
(esfuerzo bajo). Este módulo concentra esa llamada para que no esté repetida en
seis lugares.

Proveedor: OpenAI directo, modelo gpt-5.5. El esfuerzo de cada etapa se fija por
entorno (CENSO_ESFUERZO_SQL, CENSO_ESFUERZO_REDACTOR) con el vocabulario propio
de gpt-5.5: none|low|medium|high.

EL TOPE DE SALIDA INCLUYE EL RAZONAMIENTO. `max_completion_tokens` topea tokens
de razonamiento MÁS texto en el mismo presupuesto: si el modelo gasta el cupo
pensando, devuelve finish_reason='length' y texto VACÍO (incidente 2026-07-06).
Por eso los topes de los motores son holgados —4000 el SQL, 1600 el redactor— y
no hay que bajarlos sin volver a medir. El tope es un techo, no un gasto: solo se
factura lo realmente generado.

La clave sale del entorno (OPENAI_API_KEY); nunca se escribe en ningún archivo
del repo.
"""
import os
import time
from dataclasses import dataclass

# Timeout ACOTADO: sin él una respuesta colgada (CLOSE-WAIT) deja el hilo worker
# clavado y wedge toda la app (incidente 2026-07-06).
TIMEOUT = float(os.environ.get("CENSO_LLM_TIMEOUT", "60"))
REINTENTOS = int(os.environ.get("CENSO_LLM_REINTENTOS", "2"))

MODELO_POR_DEFECTO = os.environ.get("CENSO_MODELO_BASE", "gpt-5.5")

_cliente = None


@dataclass
class Respuesta:
    texto: str
    uso: object = None          # el objeto usage de la API, tal cual
    motivo_corte: str | None = None


def cliente():
    """Cliente de OpenAI, instanciado una sola vez."""
    global _cliente
    if _cliente is None:
        from openai import OpenAI
        # max_retries=0 A PROPÓSITO: los reintentos los hace completar(), que
        # distingue el tipo de error. El SDK reintenta TODO, incluido el timeout,
        # y eso multiplica la espera por 3 (ver completar()).
        _cliente = OpenAI(timeout=TIMEOUT, max_retries=0)
    return _cliente


def completar(modelo: str, esfuerzo: str, tope: int, sistema: str, usuario: str) -> Respuesta:
    """Una llamada de una etapa (SQL o redactor). Devuelve texto + métricas.

    UN TIMEOUT NO SE REINTENTA. Si la llamada se pasó de TIMEOUT segundos es
    porque esa pregunta hace razonar mucho al modelo, y el reintento va a tardar
    exactamente lo mismo: reintentar solo multiplica la espera. Con los
    reintentos del SDK, "Cuántas personas hay en Montevideo por segmento censal"
    tardaba 183 s (3 x 60) en los cuatro censos, muy por encima del tope de 90 s
    del navegador, así que el usuario nunca llegaba a ver el motivo y le quedaba
    el aviso genérico de "problema de conexión". Fallando en el primer timeout
    la respuesta llega en ~60 s y el frontend puede mostrar qué pasó.

    Lo que SÍ se reintenta es el error transitorio: conexión cortada, 429, 5xx.
    """
    ultimo = None
    for intento in range(REINTENTOS + 1):
        try:
            r = cliente().chat.completions.create(
                model=modelo,
                reasoning_effort=esfuerzo,
                max_completion_tokens=tope,
                messages=[{"role": "system", "content": sistema},
                          {"role": "user", "content": usuario}],
            )
            return Respuesta(
                texto=(r.choices[0].message.content or "").strip(),
                uso=getattr(r, "usage", None),
                motivo_corte=getattr(r.choices[0], "finish_reason", None),
            )
        except Exception as e:
            if "Timeout" in type(e).__name__ or intento == REINTENTOS:
                raise
            ultimo = e
            time.sleep(0.5 * (2 ** intento))
    raise ultimo
