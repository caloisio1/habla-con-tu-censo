"""registro.py — Rastro de las consultas que no llegan a ejecutarse.

Un rechazo del guard (o un NO_RESPONDIBLE) va tal cual a la pantalla del usuario
y hasta ahora no dejaba ninguna huella: ni la pregunta, ni el SQL que el modelo
generó, ni el motivo. Reproducir un caso reportado era a ciegas, a fuerza de
repetir la pregunta hasta que el modelo volviera a escribir el mismo SQL (el
falso positivo del CTE, 2026-07-27, apareció 1 de cada N veces).

Escribe por stderr, que es lo que ya recoge journald en el VPS:

    journalctl -u censo-query-uy | grep RECHAZO
    journalctl -u censo-query-uy --since today | grep -c NO_RESPONDIBLE

El SQL se aplana a una línea para que cada evento sea grepeable de una pieza.
No se registran respuestas ni filas: solo la pregunta, el SQL y el motivo.
"""

import logging

log = logging.getLogger("censo")

if not log.handlers:
    # Handler propio: bajo uvicorn el logger raíz puede no tener ninguno, y sin
    # esto los eventos se perderían o saldrían sin fecha.
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [censo] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


def _plano(sql):
    return " ".join((sql or "").split())


def rechazo(censo, pregunta, motivo, sql):
    """Consulta bloqueada por el guard: no se ejecutó."""
    log.warning("RECHAZO censo=%s motivo=%s | pregunta=%r | sql=%s",
                censo, motivo, pregunta, _plano(sql))


def no_respondible(censo, pregunta, detalle=""):
    """El modelo declinó generar SQL para esa pregunta."""
    log.info("NO_RESPONDIBLE censo=%s%s | pregunta=%r",
             censo, (" (%s)" % detalle) if detalle else "", pregunta)
