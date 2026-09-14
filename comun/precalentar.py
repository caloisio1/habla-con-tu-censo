"""comun/precalentar.py — Deja la caché caliente con las preguntas de ejemplo.

Los cuatro chips de cada censo son, de lejos, lo más consultado de una demo
pública: el visitante llega, hace clic en uno y espera. Sin precalentado paga los
6 a 20 segundos completos, dos llamadas al modelo incluidas, aunque la misma
pregunta se haya respondido mil veces antes.

Al arrancar el servicio se corren las dieciséis, en segundo plano, y quedan
guardadas en la caché de dos niveles. A partir de ahí un clic responde en
centésimas.

CUÁNDO CORRE. Solo al iniciar el proceso. Un `systemctl restart` vacía la caché
(vive en memoria) y vuelve a calentarla, que es exactamente lo que se quiere: si
se cambió el prompt, el esquema o la base, lo cacheado se descarta con el
proceso y se regenera contra el código nuevo.

QUÉ CUESTA. Dieciséis preguntas, dos llamadas cada una: unos US$ 0,40 por
reinicio. Se apaga con CENSO_PRECALENTAR=0.

NUNCA BLOQUEA. Corre en un hilo aparte, después de que el servicio ya acepta
consultas, y con una pausa entre preguntas para no competir con un usuario real.
Cualquier error se traga: si el precalentado falla, el servicio sigue
funcionando exactamente igual, solo que sin la ventaja.
"""
import logging
import os
import threading

import usage_log   # telemetría: el precalentado se marca, no se cuenta como pregunta
import time

log = logging.getLogger("censo")

HABILITADO = os.environ.get("CENSO_PRECALENTAR", "1") != "0"
DEMORA_INICIAL = float(os.environ.get("CENSO_PRECALENTAR_DEMORA", "5"))
PAUSA_ENTRE = float(os.environ.get("CENSO_PRECALENTAR_PAUSA", "1.5"))

# Las preguntas de ejemplo de la interfaz. Esta es la lista CANÓNICA: el
# frontend tiene su propia copia en index.html y un test verifica que las dos
# coincidan, porque precalentar preguntas que nadie ve no sirve de nada.
CHIPS = {
    "1996": [
        "¿Cómo se distribuye la población por nivel educativo?",
        "¿Qué porcentaje de hogares tenía computadora?",
        "¿Cuántas viviendas estaban desocupadas?",
        "¿Cuántas personas vivían en cada departamento?",
    ],
    "2004": [
        "¿Cuántas personas vivían en cada departamento?",
        "¿Cuántas personas vivían en asentamientos irregulares?",
        "¿Cuántas viviendas estaban desocupadas y por qué motivo?",
        "¿Cuál es la distribución de la población por sexo?",
    ],
    "2011": [
        "¿Cuánta gente vive en Paso de los Toros?",
        "Porcentaje de afrodescendientes por departamento",
        "Porcentaje de niños de 5 años o menos por barrio de Montevideo",
        "Cantidad de hogares con 2 o más NBI por sección censal",
    ],
    "2023": [
        "¿Cuántas personas viven en Paso de los Toros?",
        "¿Qué porcentaje de la población es afrodescendiente?",
        "¿Cuántas viviendas desocupadas hay?",
        "Porcentaje de afrodescendientes por barrio de Montevideo",
    ],
}


def preguntas():
    """[(censo, pregunta)] en el orden en que se van a precalentar.

    Se empieza por 2023, que es el censo seleccionado por defecto y por lo tanto
    el que más probablemente reciba el primer clic.
    """
    orden = ["2023", "2011", "2004", "1996"]
    return [(c, p) for c in orden for p in CHIPS.get(c, [])]


def _correr(motores):
    time.sleep(DEMORA_INICIAL)   # que el servicio empiece a atender primero
    t0 = time.time()
    listas = fallidas = 0
    for censo, pregunta in preguntas():
        try:
            # El precalentado NO es una pregunta de usuario: se marca como tal para
            # que no ensucie el costo por pregunta del piloto (antes sus llamadas
            # quedaban sueltas, sin consulta_id).
            usage_log.iniciar(censo)
            r = motores[censo](pregunta)
            if r.get("ok"):
                listas += 1
            else:
                fallidas += 1
                log.info("PRECALENTADO sin resultado censo=%s motivo=%s | %r",
                         censo, r.get("motivo") or r.get("veredicto"), pregunta)
        except Exception as exc:                              # noqa: BLE001
            fallidas += 1
            log.warning("PRECALENTADO error censo=%s %s | %r", censo, exc, pregunta)
        finally:
            usage_log.cerrar("precalentado")
        time.sleep(PAUSA_ENTRE)
    log.info("PRECALENTADO listo: %d de %d preguntas en caché (%.0fs)",
             listas, listas + fallidas, time.time() - t0)
    # Recién acá se tocaron los cuatro censos, así que las conexiones perezosas de
    # comun/ejecutor.py ya existen todas: es el único momento en que el estado del
    # motor está completo. Sin esta línea, una degradación a SQLite sería invisible.
    try:
        from comun import ejecutor
        log.info("MOTOR SQL %s", ejecutor.estado())
    except Exception:                                         # noqa: BLE001
        pass


def arrancar(motores):
    """Lanza el precalentado en segundo plano. No bloquea ni puede romper nada."""
    if not HABILITADO:
        log.info("PRECALENTADO deshabilitado (CENSO_PRECALENTAR=0)")
        return None
    hilo = threading.Thread(target=_correr, args=(motores,), name="precalentar",
                            daemon=True)
    hilo.start()
    log.info("PRECALENTADO lanzado: %d preguntas de ejemplo", len(preguntas()))
    return hilo
