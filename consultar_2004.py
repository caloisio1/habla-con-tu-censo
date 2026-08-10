"""consultar_2004.py — Motor de consultas del Censo 2004 Fase 1 (INE Uruguay).

Interfaz `preguntar(texto)`, la misma que consultar_2023. La lógica vive en
motor_historico.py, compartida con 1996.
"""
import json
import sys

from motor_historico import Motor
from sql_guard_historicos import GUARD_2004

REGLAS = """Reglas propias del Censo 2004 Fase 1:
- Es un censo de CONTEO: solo geografía, vivienda, hogar, sexo y edad. Si la pregunta pide
  educación, ocupación, ingresos, migración, ascendencia o cualquier variable que no esté
  en el esquema, devolvé NO_RESPONDIBLE.
- Hay UNA sola tabla, censo2004, con una fila por persona y los datos de la vivienda y del
  hogar repetidos en cada fila. Por eso hay que filtrar SIEMPRE por la bandera de registro:
    PERSONAS  -> WHERE per=1     HOGARES -> WHERE hog=1     VIVIENDAS -> WHERE viv=1
  Contar sin ese filtro multiplica viviendas y hogares por la cantidad de integrantes.
- El 0 significa NO APLICA, no una categoría: tipviv_p=0 son colectivas, tipviv_c=0 son
  particulares, sexo=0 son filas que no son de persona. Excluilo de los cruces.
- tipviv_p=9 y desocupada=9 son centinelas sin dato: excluilos.
- Los nombres de departamento, localidad y barrio ya están en la tabla (nom_dpto, nom_loc,
  nom_barrio), en MAYÚSCULAS y sin tildes: no hace falta unir al nomenclátor para narrar."""

_motor = Motor(
    censo="2004",
    db_env="CENSO2004_DB",
    db_default="censo2004.db",
    esquema="esquema_llm_2004.txt",
    guard=GUARD_2004,
    reglas=REGLAS,
    fuente="Censo 2004 Fase 1, INE Uruguay",
)


def preguntar(texto, verbose=False, avisar=None):
    return _motor.preguntar(texto, avisar=avisar)


if __name__ == "__main__":
    pregunta = " ".join(sys.argv[1:]) or "¿Cuántas personas había en Uruguay en 2004?"
    res = preguntar(pregunta)
    print("PREGUNTA :", pregunta)
    print("SQL      :", res.get("sql"))
    print("VEREDICTO:", res.get("veredicto"))
    print("SUPRIMIDAS:", res.get("celdas_suprimidas", 0))
    if res.get("datos") is not None:
        print("DATOS    :", json.dumps(res["datos"][:12], ensure_ascii=False, default=str))
    print("RESPUESTA:", res.get("respuesta"))
