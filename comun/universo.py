"""universo.py — Códigos que sacan a la persona del UNIVERSO de una variable.

EL PROBLEMA QUE RESUELVE. Una variable del censo puede traer tres clases de valor,
y sólo dos estaban contempladas:

  1. categorías de respuesta      -> entran al numerador y al denominador
  2. perdidos (7777/8888/9898/9999) -> ya se excluían por regla del prompt
  3. FUERA DE UNIVERSO             -> nadie los excluía

La tercera clase es una categoría legítima del diccionario, con código chico y
etiqueta amable ('0 = Menor de 25 años' en NIVELEDU25MAS), que NO es una respuesta:
marca que a esa persona nunca se le hizo la pregunta porque no pertenece al universo
de la variable. Si entra al denominador, el porcentaje se calcula sobre una población
que incluye a quienes jamás pudieron contestar, y la cifra sale sistemáticamente baja.
En nivel educativo son 1.128.699 personas, un tercio del país: los porcentajes
quedaban subestimados un 48,8 %.

POR QUÉ SE DERIVA DEL DICCIONARIO Y NO SE ESCRIBE A MANO. Una lista hardcodeada
envejece en silencio: si mañana se recarga la base con una variable nueva que tenga
su propio 'Menor de N años', la lista sigue diciendo que hay tres y el bug vuelve sin
que nadie lo note. Acá la fuente de verdad es el mismo diccionario que genera el
esquema que ve el modelo, así que las dos cosas no pueden divergir.

QUÉ NO ES. Esto no reemplaza la exclusión de perdidos, que sigue siendo del prompt:
son dos cosas distintas —'no contestó' contra 'no le correspondía contestar'— y se
excluyen por razones distintas.
"""
import json
import os
import re

# Etiquetas que declaran fuera de universo. 'Menor de N años' es la forma que usa el
# INE en 2023 para las variables con piso de edad; las otras dos se agregan porque son
# las formas corrientes en los diccionarios y cuestan nada.
_RX_FUERA = re.compile(r"^\s*(menor(?:es)? de\s+\d+\s+a[ñn]os?"
                       r"|no aplica"
                       r"|no corresponde a la persona"
                       r"|fuera de universo)\s*$", re.I)

_CACHE = {}


def _cargar(ruta, tabla):
    with open(ruta, encoding="utf-8") as f:
        dicc = json.load(f)
    fuera = {}
    for var in dicc.get("tablas", {}).get(tabla, {}).get("variables", []):
        nombre = (var.get("nombre") or "").lower()
        perdidos = set(str(k) for k in (var.get("perdidos") or {}))
        codigos, etiquetas = [], []
        for codigo, etiqueta in (var.get("value_labels") or {}).items():
            if str(codigo) in perdidos:      # un perdido no es fuera de universo
                continue
            if _RX_FUERA.match(str(etiqueta)):
                codigos.append(str(codigo))
                etiquetas.append(str(etiqueta))
        if codigos:
            fuera[nombre] = {"nombre": var.get("nombre") or nombre,
                             "codigos": sorted(codigos), "etiquetas": etiquetas,
                             "descripcion": var.get("descripcion") or nombre,
                             "tipo": (var.get("tipo") or "TEXT").upper()}
    return fuera


def tabla(ruta_diccionario, tabla_datos="personas_2023"):
    """{variable_en_minúsculas: {codigos, etiquetas, descripcion, tipo}}.

    Cacheado por (ruta, tabla): el diccionario no cambia en caliente y esto lo lee
    el guard en cada consulta."""
    clave = (os.path.abspath(ruta_diccionario), tabla_datos)
    if clave not in _CACHE:
        _CACHE[clave] = _cargar(ruta_diccionario, tabla_datos)
    return _CACHE[clave]


def presentes_en(sql, fuera):
    """Las variables con fuera de universo que aparecen NOMBRADAS en el SQL.

    Sirve para contarle al usuario qué universo se aplicó. Se busca el nombre como
    palabra entera para no confundir DISC_TIENE con una columna que lo contenga."""
    bajo = (sql or "").lower()
    return [v for v in sorted(fuera) if re.search(r"\b%s\b" % re.escape(v), bajo)]


def frase_universo(variables, fuera):
    """Nota al pie, en castellano, de los universos que se aplicaron. Vacía si no
    hubo ninguno. Va en la respuesta porque un denominador que cambia sin avisar es
    justamente lo que hace que una cifra no se pueda auditar.

    Se nombra la CATEGORÍA excluida y no la descripción de la variable: las del
    diccionario del INE vienen en mayúsculas y sin tildes ('MAXIMO NIVEL ALCANZADO'),
    y metidas en una oración se leen como un error de ortografía nuestro."""
    etiquetas = []
    for v in variables:
        for e in (fuera.get(v) or {}).get("etiquetas", []):
            if e not in etiquetas:
                etiquetas.append(e)
    if not etiquetas:
        return ""
    return ("\nNota de universo: quedan fuera del numerador y del denominador las personas "
            "clasificadas como «%s», que no pertenecen al universo de la pregunta."
            % "», «".join(etiquetas))
