"""consultar_1996.py — Motor de consultas del Censo 1996 (INE Uruguay).

Interfaz `preguntar(texto)`, la misma que consultar_2023, para que el servicio
unificado despache según el selector de censo. La lógica vive en
motor_historico.py, compartida con 2004.
"""
import json
import sys

from motor_historico import Motor
from sql_guard_historicos import GUARD_1996

REGLAS = """Reglas propias del Censo 1996:
- PERSONAS: COUNT(*) sobre personas_1996. Es censo completo, sin ponderador: NO hay
  columna W ni nada que sumar; el conteo ES la cifra publicada.
- HOGARES: COUNT(DISTINCT hogar_key) sobre personas_1996. NUNCA rearmes la clave desde
  dpto/secc/segm/loc/vivienda/hogarviv: usá hogar_key, ya materializada.
- VIVIENDAS: COUNT(*) sobre viviendas_1996. Para viviendas ocupadas, WHERE condocup='1'.
- PROHIBIDO unir personas_1996 con viviendas_1996. Las variables de vivienda y de hogar
  (tipviv, tenencia, higienico, calefaccio, los artefactos, NBI) ya están copiadas dentro
  de personas_1996: usalas de ahí.
- Las variables de la sección C de vivienda están informadas SOLO en viviendas particulares
  ocupadas con moradores presentes: al calcular porcentajes, el denominador son esas.
- edad=99 es el TOPE ('99 y más'), no un perdido: no lo excluyas de los conteos de edad.
- aestudio son años de estudio y su código 99 es NS/NC: excluilo SIEMPRE de promedios.
- Para narrar con nombres de departamento o localidad, unir al nomenclátor:
    JOIN cod_departamentos d ON personas_1996.dpto = d.dpto            (d.nombre)
    JOIN cod_localidades_1963_2023 c ON c.dpto = personas_1996.dpto AND c.cod_1996 = personas_1996.loc  (c.nom_1996)
  Los nombres del nomenclátor están en MAYÚSCULAS y SIN tildes.
- Para narrar ocupaciones, unir cota70(codigo, descripcion) o cnuo96(codigo, descripcion)."""

_motor = Motor(
    censo="1996",
    db_env="CENSO1996_DB",
    db_default="censo1996.db",
    esquema="esquema_llm_1996.txt",
    guard=GUARD_1996,
    reglas=REGLAS,
    fuente="Censo 1996, INE Uruguay",
)


def preguntar(texto, verbose=False, avisar=None):
    return _motor.preguntar(texto, avisar=avisar)


if __name__ == "__main__":
    pregunta = " ".join(sys.argv[1:]) or "¿Cuántas personas había en Uruguay en 1996?"
    res = preguntar(pregunta)
    print("PREGUNTA :", pregunta)
    print("SQL      :", res.get("sql"))
    print("VEREDICTO:", res.get("veredicto"))
    print("SUPRIMIDAS:", res.get("celdas_suprimidas", 0))
    if res.get("datos") is not None:
        print("DATOS    :", json.dumps(res["datos"][:12], ensure_ascii=False, default=str))
    print("RESPUESTA:", res.get("respuesta"))
