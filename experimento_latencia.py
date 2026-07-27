#!/usr/bin/env python3
"""experimento_latencia.py — dónde se va el tiempo de una consulta, y qué se gana bajando
el esfuerzo de razonamiento de la etapa SQL.

Solo lectura sobre el motor real: no cambia configuración de producción, la varía por
entorno dentro del proceso. Mide, por consulta y por etapa, latencia y tokens, y compara
las CIFRAS obtenidas para que una mejora de velocidad no se pague con un número distinto.

Uso:  experimento_latencia.py [censo]      (por defecto 2023)
"""
import json, os, statistics, sys, time

AQUI = os.path.dirname(os.path.abspath(__file__))
os.chdir(AQUI); sys.path.insert(0, AQUI)

CENSO = sys.argv[1] if len(sys.argv) > 1 else "2023"
SALIDA = os.environ.get("SALIDA_LAT", "logs/experimento_latencia.json")
ESFUERZOS = ["high", "medium", "low", "none"]
REPETICIONES = int(os.environ.get("REPETICIONES", "2"))

PREGUNTAS = [
    ("simple", "¿Cuántas personas viven en Uruguay?"),
    ("localidad", "¿Cuántas personas viven en Paso de los Toros?"),
    ("porcentaje", "¿Qué porcentaje de la población es afrodescendiente?"),
    ("mapa", "Porcentaje de afrodescendientes por departamento"),
    ("jerarquica", "¿Cuántas personas viven en hogares donde al menos un miembro es afrodescendiente?"),
    ("edad", "¿Cuántas personas mayores de 65 años hay en Rivera?"),
]

etapas = []   # una fila por llamada al modelo


def envolver(modulo, etiqueta):
    orig = modulo.client.chat.completions.create

    def wrap(**kw):
        t0 = time.time()
        r = orig(**kw)
        u = getattr(r, "usage", None)
        dc = getattr(u, "completion_tokens_details", None)
        sysm = next((m["content"] for m in kw["messages"] if m["role"] == "system"), "")
        etapas.append({
            "motor": etiqueta,
            "etapa": "sql" if sysm.lstrip().startswith("Sos un traductor") else "redactor",
            "esfuerzo": kw.get("reasoning_effort"),
            "segundos": round(time.time() - t0, 2),
            "salida": getattr(u, "completion_tokens", None),
            "razonamiento": getattr(dc, "reasoning_tokens", 0) or 0,
        })
        return r
    modulo.client.chat.completions.create = wrap
    return orig


def cifras(r):
    """Las métricas numéricas de la respuesta, para comparar entre configuraciones."""
    datos = r.get("datos") or []
    return [tuple(round(v, 2) for v in f.values() if isinstance(v, (int, float)))
            for f in datos[:25]]


def main():
    import consultar_1996, consultar_2004, consultar_2023, motor_historico
    from app import main as m2011
    for mod, et in ((consultar_2023, "2023"), (motor_historico, "hist"), (m2011, "2011")):
        envolver(mod, et)
    motores = {"1996": consultar_1996.preguntar, "2004": consultar_2004.preguntar,
               "2011": m2011.responder_2011, "2023": consultar_2023.preguntar}

    resultados = []
    for esfuerzo in ESFUERZOS:
        # el esfuerzo de la etapa SQL se toma del módulo en cada llamada
        for mod in (consultar_2023, motor_historico, m2011):
            mod.ESFUERZO_SQL = esfuerzo
        for clase, pregunta in PREGUNTAS:
            for i in range(REPETICIONES):
                del etapas[:]
                t0 = time.time()
                try:
                    r = motores[CENSO](pregunta)
                except Exception as exc:                    # noqa: BLE001
                    r = {"ok": False, "respuesta": "EXCEPCIÓN: %s" % exc}
                total = round(time.time() - t0, 2)
                por_etapa = {e["etapa"]: e for e in etapas}
                resultados.append({
                    "esfuerzo": esfuerzo, "clase": clase, "intento": i, "total": total,
                    "sql_seg": por_etapa.get("sql", {}).get("segundos"),
                    "sql_razonamiento": por_etapa.get("sql", {}).get("razonamiento"),
                    "red_seg": por_etapa.get("redactor", {}).get("segundos"),
                    "ok": r.get("ok"), "cifras": cifras(r),
                    "sql": " ".join((r.get("sql") or "").split())[:300],
                })
                print("%-6s %-11s #%d  total=%5.1fs  sql=%5.1fs (razona %4d tok)  red=%4.1fs  ok=%s"
                      % (esfuerzo, clase, i, total, resultados[-1]["sql_seg"] or 0,
                         resultados[-1]["sql_razonamiento"] or 0,
                         resultados[-1]["red_seg"] or 0, r.get("ok")), flush=True)
                with open(SALIDA, "w", encoding="utf-8") as fh:
                    json.dump(resultados, fh, ensure_ascii=False, indent=1, default=str)

    # ── resumen ──────────────────────────────────────────────────────────
    print("\n=== RESUMEN (censo %s) ===" % CENSO)
    print("%-8s %8s %8s %8s %8s %10s" % ("esfuerzo", "total", "sql", "redactor", "razona", "ok"))
    base = {}
    for esfuerzo in ESFUERZOS:
        fs = [r for r in resultados if r["esfuerzo"] == esfuerzo]
        if not fs:
            continue
        med = lambda k: statistics.median([f[k] for f in fs if f.get(k)] or [0])
        print("%-8s %7.1fs %7.1fs %7.1fs %8.0f %9s/%d"
              % (esfuerzo, med("total"), med("sql_seg"), med("red_seg"),
                 med("sql_razonamiento"), sum(1 for f in fs if f["ok"]), len(fs)))
        if esfuerzo == "high":
            base = {f["clase"]: f["cifras"] for f in fs if f["cifras"]}

    print("\n=== ¿CAMBIAN LAS CIFRAS respecto de 'high'? ===")
    for esfuerzo in ESFUERZOS[1:]:
        difs = []
        for f in [r for r in resultados if r["esfuerzo"] == esfuerzo and r["cifras"]]:
            esperado = base.get(f["clase"])
            if esperado and f["cifras"] != esperado:
                difs.append(f["clase"])
        print("  %-8s %s" % (esfuerzo, "IGUALES" if not difs
                             else "DISTINTAS en: " + ", ".join(sorted(set(difs)))))


if __name__ == "__main__":
    main()
