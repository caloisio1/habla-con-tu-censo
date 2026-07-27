"""comun/cache.py — Caché de respuestas, compartida por los cuatro motores.

Una consulta cuesta dos llamadas al modelo y entre 6 y 20 segundos. Buena parte
del tráfico de una demo pública es repetido: los cuatro chips de ejemplo de cada
censo, las preguntas que un evaluador repite para comparar, la misma pregunta
escrita por dos personas distintas. Todo eso hoy se paga entero cada vez.

DOS NIVELES, y el orden importa:

  A) pregunta -> SQL generado.   Ahorra la llamada CARA (la que razona, 3 a 35 s).
  B) SQL ejecutado -> filas + texto redactado.  Ahorra la base y la redacción.

La clave del nivel B NO es el texto que escribió el usuario: es el SQL final, o sea
DESPUÉS de que el resolver reemplazó los nombres por su forma canónica. Así
"Chamiso", "chamizo" y "CHAMIZO" terminan en la misma entrada, y dos preguntas
distintas que resolvieron a la misma consulta se sirven de la misma cifra. Es la
regla del encargo: la clave se construye con la entidad ya resuelta y el año censal,
nunca con el nombre escrito.

El nivel A sí se indexa por la pregunta (normalizada). Que dos formas de escribir el
mismo lugar caigan en claves distintas NO produce una cifra equivocada: produce un
fallo de caché, y el nivel B las vuelve a juntar.

LO QUE NUNCA SE SALTEA. Un acierto de nivel A devuelve el SQL, no la respuesta: el
post-paso de entidades, el guard y la supresión se ejecutan igual, siempre. La caché
acelera, no relaja ningún control.

QUÉ NO SE CACHEA. Las respuestas con `ok=False`, para que un fallo transitorio no
quede congelado, y nada que dependa de la hora.

INVALIDACIÓN. La caché vive en memoria del proceso y muere con él, así que un
`systemctl restart` la vacía: nunca puede sobrevivir a un cambio de base o de
prompt. Además tiene tope de entradas y vencimiento por tiempo.
"""
import hashlib
import os
import threading
import time

# Tope conservador: cada entrada son unas pocas decenas de KB (filas + texto).
MAX_ENTRADAS = int(os.environ.get("CENSO_CACHE_MAX", "500"))
VIGENCIA_S = int(os.environ.get("CENSO_CACHE_TTL", "21600"))   # 6 horas
HABILITADA = os.environ.get("CENSO_CACHE", "1") != "0"

_DATOS = {}          # clave -> (momento, valor)
_ORDEN = []          # claves por antigüedad de uso, para desalojar
_LOCK = threading.Lock()
_METRICAS = {"aciertos": 0, "fallos": 0, "desalojos": 0}


SQL_DE_PREGUNTA = "A"    # nivel A: pregunta -> SQL
RESULTADO_DE_SQL = "B"   # nivel B: SQL -> filas + texto


def clave(ambito, censo, texto):
    """Clave estable. El texto se normaliza para que el formato no la parta."""
    normalizado = " ".join((texto or "").split()).lower()
    return "%s|%s|%s" % (ambito, censo,
                         hashlib.sha256(normalizado.encode("utf-8")).hexdigest())


def obtener(ambito, censo, texto):
    """Valor cacheado, o None."""
    if not HABILITADA or not texto:
        return None
    k = clave(ambito, censo, texto)
    with _LOCK:
        entrada = _DATOS.get(k)
        if entrada is None:
            _METRICAS["fallos"] += 1
            return None
        momento, valor = entrada
        if time.time() - momento > VIGENCIA_S:
            _DATOS.pop(k, None)
            if k in _ORDEN:
                _ORDEN.remove(k)
            _METRICAS["fallos"] += 1
            return None
        # uso reciente: se va al final de la cola de desalojo
        if k in _ORDEN:
            _ORDEN.remove(k)
        _ORDEN.append(k)
        _METRICAS["aciertos"] += 1
        return valor


def guardar(ambito, censo, texto, valor):
    """Guarda un valor. Solo se llama con resultados exitosos."""
    if not HABILITADA or not texto or valor is None:
        return
    k = clave(ambito, censo, texto)
    with _LOCK:
        if k not in _DATOS and len(_DATOS) >= MAX_ENTRADAS:
            viejo = _ORDEN.pop(0) if _ORDEN else None
            if viejo is not None:
                _DATOS.pop(viejo, None)
                _METRICAS["desalojos"] += 1
        _DATOS[k] = (time.time(), valor)
        if k in _ORDEN:
            _ORDEN.remove(k)
        _ORDEN.append(k)


def metricas():
    """Aciertos, fallos y desalojos, para saber si la caché sirve de algo."""
    with _LOCK:
        total = _METRICAS["aciertos"] + _METRICAS["fallos"]
        return dict(_METRICAS, entradas=len(_DATOS),
                    tasa_acierto=round(_METRICAS["aciertos"] / total, 3) if total else 0.0)


def vaciar():
    with _LOCK:
        _DATOS.clear()
        del _ORDEN[:]
        for k in _METRICAS:
            _METRICAS[k] = 0
