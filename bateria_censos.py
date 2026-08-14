#!/usr/bin/env python3
"""bateria_censos.py — Batería de regresión de los CUATRO censos.

Antes de este trabajo solo el Censo 2023 tenía una batería de punta a punta; 1996
y 2004 tenían verificación de la base pero no del motor, y 2011 no tenía ninguna.
Justamente por eso el fan-out del nomenclátor de 1996 —que inflaba el total del
país un 416 %— pudo llegar a producción sin que nada lo detectara.

Dos capas:

  CAPA A (determinista, sin modelo, segundos): las cifras ancla de cada censo por
         SQL directo, más los controles del módulo compartido. Es la que tiene que
         estar VERDE sí o sí antes de cualquier despliegue.

  CAPA B (extremo a extremo, con modelo): las preguntas de los tres bugs del INE
         en los cuatro censos. Cuesta tokens y tarda; se corre antes de desplegar.

Uso:  bateria_censos.py [A|B|AB]     (por defecto A)
"""
import json
import os
import sys
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
os.chdir(AQUI)
sys.path.insert(0, AQUI)

from comun import edad, ejecutor, indicadores, nomenclator as nom, rechazos, supresion
from comun.resolver import AMBIGUO, FRAGMENTADO, NO_ENCONTRADO, OTRO_CENSO, UNICO, resolver
from comun.sql_entidades import EntidadNoResuelta, preparar_1996, resolver_en_sql

MODO = (sys.argv[1] if len(sys.argv) > 1 else "A").upper()
CENSOS = ("1996", "2004", "2011", "2023")

# ── cifras ancla, verificadas ────────────────────────────────────────────
# Las de 2023 son las publicadas por el INE y ya estaban verificadas contra ellos.
ANCLAS = {
    "1996": [("personas", "SELECT COUNT(*) FROM personas_1996", 3163763),
             ("viviendas", "SELECT COUNT(*) FROM viviendas_1996", 1126502),
             ("hogares", "SELECT COUNT(DISTINCT hogar_key) FROM personas_1996", 975056)],
    "2004": [("personas", "SELECT COUNT(*) FROM censo2004 WHERE per=1", 3241003),
             ("viviendas", "SELECT COUNT(*) FROM censo2004 WHERE viv=1", 1279741),
             ("hogares", "SELECT COUNT(*) FROM censo2004 WHERE hog=1", 1065677)],
    # 28-jul-2026: +53 personas, +19 hogares y +19 viviendas. Se dejaron de descartar
    # las 53 personas con el cuestionario bajo secreto estadístico (19 hogares
    # completos): cuentan en la población aunque no aporten a ningún otro corte.
    # Ver datos/NOTAS_CALIDAD.md.
    "2011": [("personas", "SELECT COUNT(*) FROM personas", 3285877),
             ("hogares", "SELECT COUNT(DISTINCT hogar_key) FROM personas", 1166270),
             ("viviendas", "SELECT COUNT(DISTINCT vivienda_key) FROM personas", 1136432)],
    # 12-ago-2026: los hogares 2023 llevan DOS controles y no hay que confundirlos. El
    # crudo (1.255.062) verifica que la base cargó bien; el PONDERADO (1.376.921) es la
    # cifra que el INE publica y la única que la app puede mostrar. Tenerlos separados es
    # justamente lo que faltaba: durante meses el único control era el crudo, así que el
    # motor podía contestar el crudo y la batería daba verde.
    "2023": [("personas (ponderadas)", "SELECT ROUND(SUM(W)) FROM personas_2023", 3499451),
             ("hogares censados (control de carga, NO se publica)",
              "SELECT COUNT(DISTINCT hogar_key) FROM personas_2023 "
              "WHERE hogar_key IS NOT NULL", 1255062),
             ("hogares ponderados (cifra publicada)",
              "SELECT ROUND(SUM(w)) FROM (SELECT hogar_key, MAX(W) AS w FROM personas_2023 "
              "WHERE hogar_key IS NOT NULL GROUP BY hogar_key)", 1376921),
             ("viviendas", "SELECT COUNT(*) FROM viviendas_2023", 1659044)],
}

SQL_1996_FANOUT = (
    "SELECT c.nom_1996 AS geo_nombre, COUNT(*) AS personas FROM personas_1996 p "
    "JOIN cod_localidades_1963_2023 c ON c.dpto=p.dpto AND c.cod_1996=p.loc "
    "GROUP BY c.nom_1996")

resultados = []


def control(seccion, nombre, ok, obtenido="", esperado=""):
    resultados.append({"capa": seccion, "control": nombre, "ok": bool(ok),
                       "obtenido": obtenido, "esperado": esperado})
    print("  %s %-58s %s" % ("✔" if ok else "✘", nombre[:58],
                             "" if ok else "obtenido=%r esperado=%r" % (obtenido, esperado)))
    return ok


def _q(censo, sql):
    """Escalar por el MISMO camino que usa producción.

    Antes abría el .db con sqlite3 directo. Eso hacía que el control de regresión
    midiera una base que la app ya no usa -y que borrar las bases dejara ciego al
    instrumento con el que se verifica que nada se rompió-."""
    return ejecutor.escalar(nom.BASES[censo], sql)


# ══ CAPA A ═══════════════════════════════════════════════════════════════
def capa_a():
    print("\n=== CAPA A · anclas de los cuatro censos ===")
    for censo in CENSOS:
        for etiqueta, sql, esperado in ANCLAS[censo]:
            obtenido = _q(censo, sql)
            control("A/anclas", "%s · %s" % (censo, etiqueta),
                    obtenido == esperado, obtenido, esperado)

    print("\n=== CAPA A · nomenclátor cruzado de 1996 (fan-out) ===")
    total_malo = _q("1996", "SELECT SUM(personas) FROM (%s)" % SQL_1996_FANOUT)
    control("A/1996", "el JOIN sin deduplicar sigue inflando (el defecto existe)",
            total_malo == 16321955, total_malo, 16321955)
    corregido, hubo = preparar_1996(SQL_1996_FANOUT)
    control("A/1996", "el post-paso sustituye el nomenclátor cruzado", hubo, hubo, True)
    control("A/1996", "usa LEFT JOIN (no pierde a nadie)", "LEFT JOIN" in corregido.upper())
    total_bueno = _q("1996", "SELECT SUM(personas) FROM (%s)" % corregido)
    control("A/1996", "el total por localidad cuadra con el país",
            total_bueno == 3163763, total_bueno, 3163763)

    print("\n=== CAPA A · supresión (el cero no es confidencialidad) ===")
    _, sup, vac = supresion.suprimir_celdas_chicas([{"n_crudo": 0}], ["n_crudo"])
    control("A/supresion", "conteo 0 -> vacío, no suprimido", (sup, vac) == (0, 1),
            (sup, vac), (0, 1))
    _, sup, vac = supresion.suprimir_celdas_chicas([{"n_crudo": 3}], ["n_crudo"])
    control("A/supresion", "conteo 3 -> suprimido", (sup, vac) == (1, 0), (sup, vac), (1, 0))
    filas, sup, _ = supresion.suprimir_celdas_chicas([{"n_crudo": 5}], ["n_crudo"])
    control("A/supresion", "conteo 5 -> publicable", len(filas) == 1 and sup == 0)

    print("\n=== CAPA A · convención etaria, idéntica en los cuatro motores ===")
    from comun.pipeline import COLUMNA_EDAD
    for censo in CENSOS:
        instr = edad.instruccion("mayores de 65 años", COLUMNA_EDAD[censo])
        control("A/edad", "%s · 'mayores de 65' -> >= 65" % censo,
                "%s >= 65" % COLUMNA_EDAD[censo] in instr)
    a = edad.detectar("mayores de 65 años")[0]
    b = edad.detectar("de 65 años y más")[0]
    control("A/edad", "las dos formulaciones dan el mismo criterio",
            (a.operador, a.limite) == (b.operador, b.limite))
    m = edad.detectar("menores de 5 años")[0]
    h = edad.detectar("5 años o menos")[0]
    control("A/edad", "'menores de 5' y '5 o menos' son criterios DISTINTOS",
            m.operador != h.operador)

    print("\n=== CAPA A · resolución de entidades en los cuatro censos ===")
    for censo in CENSOS:
        r = resolver("Maldonado", None, censo)
        control("A/entidades", "%s · 'Maldonado' pide aclaración" % censo,
                r.estado == AMBIGUO, r.estado, AMBIGUO)
        r = resolver("Cerro Chato", nom.LOCALIDAD, censo)
        control("A/entidades", "%s · 'Cerro Chato' es fragmentación" % censo,
                r.estado == FRAGMENTADO, r.estado, FRAGMENTADO)
        r = resolver("Xyzabc", nom.LOCALIDAD, censo)
        control("A/entidades", "%s · nombre inexistente -> sugerencias" % censo,
                r.estado == NO_ENCONTRADO and r.sugerencias)
    r = resolver("Chamiso", nom.LOCALIDAD, "2023")
    control("A/entidades", "2023 · 'Chamiso' -> Chamizo, declarándolo",
            r.estado == UNICO and r.entidad.nombre == "CHAMIZO" and r.interpretacion)
    for censo, escrito, esperado in (("2023", "veinticinco de agosto", "25 DE AGOSTO"),
                                     ("1996", "25 de agosto", "VEINTICINCO DE AGOSTO")):
        r = resolver(escrito, nom.LOCALIDAD, censo)
        control("A/entidades", "%s · %r -> %s" % (censo, escrito, esperado),
                r.estado == UNICO and r.entidad.nombre == esperado,
                r.entidad.nombre if r.entidad else r.estado, esperado)
    r = resolver("Abayuba", nom.LOCALIDAD, "2023")
    control("A/entidades", "2023 · 'Abayuba' dice en qué censos sí está",
            r.estado == OTRO_CENSO and "1996" in r.censos_alternativos)

    print("\n=== CAPA A · desambiguación de indicadores ===")
    for censo in ("1996", "2011", "2023"):
        r = indicadores.desambiguar("¿Cuál es el departamento más educado?", censo)
        control("A/indicadores", "%s · 'más educado' devuelve opciones, no un número" % censo,
                r is not None and r["sql"] is None and len(r["opciones"]) >= 3)
    r = indicadores.desambiguar("¿Cuál es el departamento más educado?", "2004")
    control("A/indicadores", "2004 · dice que no se relevó y en qué censos sí",
            r["motivo"] == rechazos.NO_RELEVADA and "2023" in r["respuesta"])

    print("\n=== CAPA A · etiquetas de rechazo ===")
    casos = [rechazos.supresion(1), rechazos.no_encontrada("Xyz", ["Abc"]),
             rechazos.ambigua("Maldonado", []), rechazos.no_relevada("educación", "2004"),
             rechazos.procesamiento()]
    control("A/rechazos", "cada motivo tiene su propio código",
            len({c.codigo for c in casos}) == len(casos))
    conf = [c for c in casos if "confidencialidad" in c.mensaje.lower()]
    control("A/rechazos", "solo la supresión real habla de confidencialidad",
            len(conf) == 1 and conf[0].codigo == rechazos.SUPRESION)


# ══ CAPA B ═══════════════════════════════════════════════════════════════
PREGUNTAS_B = [
    # (id, censo, pregunta, comprobación)
    ("bug1-a1", None, "¿Cuántas mujeres hay en Salto?", "ok"),
    ("bug1-a2", None, "¿Cuántas personas hay en Salto?", "sin_filtro_sexo"),
    ("bug2-typo", "2023", "¿Cuántas personas viven en Chamiso?", "no_confidencialidad"),
    ("bug2-colision", "2023", "¿Cuántas personas viven en Maldonado?", "responde_y_ofrece"),
    ("bug2-colision-salto", None, "¿Cuántas personas hay en Salto?", "responde_y_ofrece"),
    ("bug2-frag", "2023", "¿Cuántas personas viven en Cerro Chato?", "pide_opciones"),
    ("bug2-inexistente", "2023", "¿Cuántas personas viven en Xyzabc?", "no_confidencialidad"),
    ("bug3-ambigua", None, "¿Cuál es el departamento más educado?", "pide_opciones"),
    ("edad-a", None, "¿Cuántas personas mayores de 65 años hay en Uruguay?", "ok"),
    ("edad-b", None, "¿Cuántas personas de 65 años y más hay en Uruguay?", "ok"),
    ("ancla", None, "¿Cuántas personas hay en Uruguay?", "ok"),
    # 1996: "desocupadas" son condocup 3..6 = 140.476. El error a atajar es contestar
    # 182.708 (total menos condocup='1'), que mete adentro las 42.232 viviendas
    # OCUPADAS con moradores ausentes, o contestar el total de viviendas sin filtrar.
    ("viv-desocupadas", "1996", "¿Cuántas viviendas estaban desocupadas?", "desocupadas_1996"),
    # 12-ago-2026 · las tres observaciones del muestrista del INE. Van acá, extremo a
    # extremo con el modelo, porque las tres fallaban en la traducción pregunta->SQL y
    # ninguna capa de abajo las veía: la base estaba bien en los tres casos.
    ("ine-hogares", "2023", "¿Cuántos hogares hay en el país?", "cifra:1376921"),
    ("ine-tamano", "2023", "¿Cuál es el tamaño medio del hogar en Uruguay?", "cifra:2.54"),
    # Montevideo ponderado = 523.829 hogares (crudo 491.206). Controla que el desglose
    # también salga ponderado y no sólo el total del país.
    ("ine-hogares-depto", "2023", "¿Cuántos hogares hay en cada departamento?", "cifra:523829"),
    # Se controla con POSTGRADO y no con "universitario": el universo (el denominador) es
    # el mismo en las dos, pero "universitario" es ambiguo en el NUMERADOR —el modelo lo
    # lee unas veces como 'Universidad o similar' (13,61 %) y otras como universidad más
    # postgrado (16,48 %)—, y un control que acepta dos respuestas no controla nada. La
    # ambigüedad es de definición, es anterior a este arreglo y está anotada aparte.
    ("ine-universo-edu", "2023", "¿Qué porcentaje de la población tiene nivel de postgrado?",
     "cifra:2.88"),
    ("ine-universo-disc", "2023", "¿Qué porcentaje de personas tiene alguna discapacidad?",
     "cifra:6.71"),
    # La desocupación se mide sobre la PEA (decisión de Carlos, 12-ago). Se pregunta con
    # la formulación AMBIGUA a propósito —"porcentaje de la población"—, que es la que
    # antes devolvía 5,84 % o 9,35 % según la corrida.
    ("ine-desocupacion", "2023", "¿Qué porcentaje de la población está desocupada?",
     "cifra:9.35"),
    # "Universitario" no se contesta solo: con posgrado 16,48 %, sin posgrado 13,61 %.
    # Se controla en 2023 y 2011, que comparten la estructura del diccionario; en 1996
    # NO debe pedir opciones porque no hay ambigüedad (una sola categoría universitaria).
    ("ine-universitario", "2023", "¿Qué porcentaje de la población tiene nivel universitario?",
     "pide_opciones"),
    ("ine-universitario-11", "2011", "¿Cuántas personas tienen educación universitaria?",
     "pide_opciones"),
    ("ine-universitario-96", "1996", "¿Cuántas personas tienen educación universitaria?", "ok"),
    # Las dos tasas sobre la PET, con el piso en 12 (Carlos, 13-ago): 'Menor de 12 años'
    # es 11 o menos, así que el universo que relevó el INE empieza en los 12. Con el piso
    # viejo en 14 daban 62,82 % y 56,95 %.
    ("ine-actividad", "2023", "¿Cuál es la tasa de actividad?", "cifra:60.85"),
    ("ine-empleo", "2023", "¿Cuál es la tasa de empleo?", "cifra:55.17"),
    # Las mismas tres tasas en 2011, donde antes NO estaban implementadas: el denominador
    # era "los que tienen respuesta válida" (59,90 % y 56,10 %) en vez de la PET por edad.
    # La codificación de 2011 es OTRA: los desocupados son DOS códigos (3 y 4), así que la
    # PEA es (2,3,4); con el mapa de 2023 la desocupación habría dado 1,35 %.
    ("ine-actividad-11", "2011", "¿Cuál es la tasa de actividad?", "cifra:57.75"),
    ("ine-empleo-11", "2011", "¿Cuál es la tasa de empleo?", "cifra:54.08"),
    ("ine-desocupacion-11", "2011", "¿Qué porcentaje de la población está desocupada?",
     "cifra:6.35"),
    ("ine-segmentos", "2023", "¿Cuántas personas hay en cada segmento censal del país?",
     "tabla_completa"),
    # La Sección Censal 99 de Montevideo es real (75.855 ponderadas). El modelo la
    # descartaba como si '99' fuera un código de no respuesta, y el desglose por sección
    # de Montevideo salía con 24 secciones en vez de 25.
    ("ine-seccion-99", "2023", "¿Cuántas personas hay en cada sección censal de Montevideo?",
     "cifra:75855"),
]


def _cifra_esperada(r, esperado):
    """¿Alguna cifra de la primera fila es la esperada? Se controla el NÚMERO y no el
    SQL: hay más de una forma correcta de escribirlo y una sola respuesta correcta."""
    tolerancia = 1.0 if abs(esperado) >= 1000 else 0.011
    for fila in (r.get("datos") or []):
        for v in fila.values():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if abs(float(v) - esperado) <= tolerancia:
                    return True
    return False


def capa_b():
    import consultar_1996, consultar_2004, consultar_2023
    from app import main as m2011
    motores = {"1996": consultar_1996.preguntar, "2004": consultar_2004.preguntar,
               "2011": m2011.responder_2011, "2023": consultar_2023.preguntar}
    print("\n=== CAPA B · extremo a extremo (con modelo) ===")
    respuestas = {}
    for ident, censo_fijo, pregunta, comprobacion in PREGUNTAS_B:
        for censo in ([censo_fijo] if censo_fijo else CENSOS):
            t0 = time.time()
            try:
                r = motores[censo](pregunta)
            except Exception as exc:                      # noqa: BLE001
                r = {"ok": False, "respuesta": "EXCEPCIÓN: %s" % exc, "motivo": "excepcion"}
            respuestas[(ident, censo)] = r
            texto = (r.get("respuesta") or "").lower()
            sql = (r.get("sql") or "").lower()

            if comprobacion == "no_confidencialidad":
                ok = "confidencialidad" not in texto
            elif comprobacion == "pide_opciones":
                ok = bool(r.get("opciones")) or r.get("motivo") in (
                    rechazos.AMBIGUA, rechazos.FRAGMENTADA, rechazos.NO_RELEVADA)
            elif comprobacion == "responde_y_ofrece":
                # La colisión departamento/ciudad no bloquea: responde, declara cuál
                # leyó y ofrece la otra lectura como chip.
                ok = bool(r.get("ok")) and bool(r.get("opciones"))
            elif comprobacion == "sin_filtro_sexo":
                ok = not any(p in sql for p in ("perph02", "sexo ="))
            elif comprobacion.startswith("cifra:"):
                ok = bool(r.get("ok")) and _cifra_esperada(r, float(comprobacion.split(":")[1]))
            elif comprobacion == "tabla_completa":
                # TRES cosas juntas: la tabla llega entera (4.297 segmentos, antes 300),
                # el redactor NO se queda mudo por el tamaño del resultado, y el mapa
                # nacional POR SEGMENTO se dibuja, con una unidad por fila. Lo tercero
                # es lo que se rompería en silencio: un desglose sin mapa sigue
                # contestando bien, así que nadie lo notaría hasta mirar la pantalla.
                filas = r.get("datos") or []
                mapa = r.get("mapa") or {}
                ok = (bool(r.get("ok")) and len(filas) >= 4000 and len(texto.strip()) > 80
                      and mapa.get("nivel") == "segmento_2023"
                      and len(mapa.get("datos") or []) == len(filas))
            elif comprobacion == "desocupadas_1996":
                # Se controla la CIFRA, no el SQL: hay más de una forma correcta de
                # escribir el filtro, y una sola respuesta correcta.
                vals = [v for f in (r.get("datos") or [{}]) for v in f.values()
                        if isinstance(v, (int, float))]
                ok = bool(r.get("ok")) and vals[:1] == [140476]
            else:
                ok = bool(r.get("ok"))
            control("B/%s" % ident, "%s · %s" % (censo, pregunta[:40]), ok,
                    (r.get("motivo") or r.get("veredicto") or "")[:40], comprobacion)
            print("      (%.1fs) %s" % (time.time() - t0, (r.get("respuesta") or "")[:110]))

    # controles cruzados
    for censo in CENSOS:
        a = respuestas.get(("edad-a", censo), {}).get("datos")
        b = respuestas.get(("edad-b", censo), {}).get("datos")
        if a and b:
            va = [v for v in a[0].values() if isinstance(v, (int, float))]
            vb = [v for v in b[0].values() if isinstance(v, (int, float))]
            control("B/edad", "%s · 'mayores de 65' == '65 y más'" % censo,
                    va and vb and va[0] == vb[0], va[:1], vb[:1])


if __name__ == "__main__":
    if "A" in MODO:
        capa_a()
    if "B" in MODO:
        capa_b()
    fallos = [r for r in resultados if not r["ok"]]
    print("\n%s  %d controles, %d fallos"
          % ("VERDE" if not fallos else "ROJO", len(resultados), len(fallos)))
    for f in fallos:
        print("   ✘ [%s] %s | obtenido=%r esperado=%r"
              % (f["capa"], f["control"], f["obtenido"], f["esperado"]))
    with open(os.environ.get("BATERIA_SALIDA", "logs/bateria_censos.json"), "w",
              encoding="utf-8") as fh:
        json.dump(resultados, fh, ensure_ascii=False, indent=1, default=str)
    sys.exit(1 if fallos else 0)
