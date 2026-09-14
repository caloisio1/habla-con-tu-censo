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

from comun import presupuesto

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


def completar(modelo: str, esfuerzo: str, tope: int, sistema: str, usuario: str,
              cache_key: str = None) -> Respuesta:
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

    `cache_key` (prompt_cache_key) es un valor FIJO por etapa y censo. No cambia
    lo que se cobra ni lo que se cachea: mejora el enrutamiento al servidor que ya
    tiene el prefijo de esa etapa, que es de donde sale el 90 % de caché del SQL.
    """
    presupuesto.verificar()          # fail-closed: si el período se agotó, no se llama
    ultimo = None
    for intento in range(REINTENTOS + 1):
        try:
            r = cliente().chat.completions.create(
                model=modelo,
                reasoning_effort=esfuerzo,
                max_completion_tokens=tope,
                messages=[{"role": "system", "content": sistema},
                          {"role": "user", "content": usuario}],
                **({"prompt_cache_key": cache_key} if cache_key else {}),
            )
            presupuesto.sumar(modelo, getattr(r, "usage", None))
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


def completar_stream(modelo: str, esfuerzo: str, tope: int, sistema: str,
                     usuario: str, emitir, cache_key: str = None) -> Respuesta:
    """Igual que completar(), pero llama a emitir(fragmento) a medida que llega.

    Devuelve la misma Respuesta que completar() —texto completo, uso y motivo de
    corte— para que quien la use no tenga que cambiar nada más: el streaming es
    un efecto lateral, no otro contrato. El uso viene en el último chunk y hay
    que pedirlo con stream_options; sin eso el registro de tokens queda en cero.

    UN REINTENTO SOLO SI NO SE EMITIÓ NADA. Con la llamada de una sola vez,
    reintentar es transparente. Acá no: si el corte llega después de haber
    emitido texto, el reintento vuelve a empezar desde el principio y el usuario
    ve la respuesta duplicada. Por eso, una vez que salió el primer fragmento, el
    error se propaga en vez de reintentarse.
    """
    presupuesto.verificar()          # fail-closed: si el período se agotó, no se llama
    ultimo = None
    for intento in range(REINTENTOS + 1):
        partes, uso, corte, emitido = [], None, None, False
        try:
            flujo = cliente().chat.completions.create(
                model=modelo,
                reasoning_effort=esfuerzo,
                max_completion_tokens=tope,
                messages=[{"role": "system", "content": sistema},
                          {"role": "user", "content": usuario}],
                stream=True,
                stream_options={"include_usage": True},
                **({"prompt_cache_key": cache_key} if cache_key else {}),
            )
            for chunk in flujo:
                if getattr(chunk, "usage", None):
                    uso = chunk.usage
                if not chunk.choices:
                    continue
                opcion = chunk.choices[0]
                fragmento = getattr(opcion.delta, "content", None)
                if fragmento:
                    partes.append(fragmento)
                    emitido = True
                    emitir(fragmento)
                if getattr(opcion, "finish_reason", None):
                    corte = opcion.finish_reason
            presupuesto.sumar(modelo, uso)
            return Respuesta(texto="".join(partes).strip(), uso=uso, motivo_corte=corte)
        except Exception as e:
            if emitido or "Timeout" in type(e).__name__ or intento == REINTENTOS:
                raise
            ultimo = e
            time.sleep(0.5 * (2 ** intento))
    raise ultimo
