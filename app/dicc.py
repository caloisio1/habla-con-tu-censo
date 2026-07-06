"""
dicc.py — Única fuente de verdad del esquema, derivada de datos/diccionario.json.

El diccionario es metadata pública del INE (nombre, etiqueta y value labels de
cada una de las 145 variables del Censo 2011). De acá salen, en capas:

  * la WHITELIST de columnas que el guard permite (145 crudas + derivadas + keys),
  * el texto del ESQUEMA que ve el LLM (compacto: nombre | etiqueta | códigos),
  * la lista de códigos PERDIDOS que nunca deben contarse.

Diseño en capas (v4): a la base van las 145 variables con sus valores CRUDOS
(códigos tal cual del INE) más las columnas DERIVADAS de v3 (departamento, sexo,
edad, asc_afro, asc_principal, nbi, codsec, codloc) y las keys (hogar_key,
vivienda_key). El LLM ve las etiquetas para saber qué significa cada código.
"""

import json
import os
from functools import lru_cache

_CANDIDATOS = (
    os.path.join(os.path.dirname(__file__), os.pardir, "datos", "diccionario.json"),
    os.path.join("datos", "diccionario.json"),
)

# Columnas que el pipeline v4 DERIVA de los microdatos crudos (semántica v3,
# legible) y agrega a la tabla personas junto a las 145 crudas.
DERIVADAS = frozenset({
    "departamento", "sexo", "edad",
    "asc_afro", "asc_principal", "nbi",
    "codsec", "codloc",
})

# Identificadores estructurales. Reidentificantes: el guard los restringe en la
# proyección externa (solo dentro de COUNT(DISTINCT ...)); ver sql_guard.py.
KEYS = frozenset({"hogar_key", "vivienda_key"})

# Columnas de la tabla de referencia geográfica.
COLUMNAS_LOCALIDADES = frozenset({"codloc", "nombre", "departamento"})

# Clasificación de perdidos: fragmento de la etiqueta REAL del INE -> abreviatura.
# La detección es POR LA ETIQUETA (no por el número de código): así, en las
# variables donde 8/9 son categorías válidas, esos códigos NO se marcan perdidos.
# Se comparan en minúsculas y por inclusión (cubre "NO RELEVADO"/"No relevado"/…).
_ABREV_PERDIDO = (
    ("no relevado", "NR"),
    ("no corresponde", "NC"),
    ("ignorado", "IG"),
    ("sin dato", "SD"),
    ("secreto estad", "SE"),
)

# Leyenda global (una sola vez, al inicio del esquema del prompt).
LEYENDA_PERDIDOS = (
    "Códigos PERDIDOS (excluir SIEMPRE de conteos, totales y denominadores; "
    "en las variables de abajo se anotan con estas siglas): "
    "NR=No relevado, NC=No corresponde, IG=Ignorado, SD=Sin dato, SE=Secreto estadístico."
)


def abrev_perdido(label: str):
    """Devuelve la sigla (NR/NC/IG/SD/SE) si la etiqueta es un perdido, o None."""
    l = (label or "").lower()
    for frag, sigla in _ABREV_PERDIDO:
        if frag in l:
            return sigla
    return None


def _ruta_diccionario() -> str:
    for c in _CANDIDATOS:
        if os.path.exists(c):
            return c
    raise FileNotFoundError(
        "No se encontró datos/diccionario.json (generalo con "
        "datos/generar_diccionario.py)."
    )


@lru_cache(maxsize=1)
def cargar() -> dict:
    """Lee y cachea diccionario.json. Una sola lectura por proceso."""
    with open(_ruta_diccionario(), encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def variables() -> tuple:
    """Lista de dicts {nombre, etiqueta, tipo, value_labels} de las 145 crudas."""
    return tuple(cargar()["variables"])


@lru_cache(maxsize=1)
def nombres_crudos() -> frozenset:
    """Nombres de las 145 variables del INE, en minúsculas (SQLite es
    case-insensitive con identificadores)."""
    return frozenset(v["nombre"].lower() for v in variables())


@lru_cache(maxsize=1)
def columnas_personas() -> frozenset:
    """Whitelist completa de la tabla personas: 145 crudas + derivadas + keys."""
    return nombres_crudos() | DERIVADAS | KEYS


def es_label_perdido(label: str) -> bool:
    """True si la etiqueta de un código representa un perdido (no se cuenta)."""
    return abrev_perdido(label) is not None


def _fmt_codigos(value_labels: dict) -> str:
    """Códigos compactos para una línea del esquema. Los perdidos se abrevian por
    su sigla (8:NR) y los válidos se listan completos (1=Casa). Así el modelo sabe
    QUÉ códigos son perdidos EN CADA variable sin repetir la etiqueta larga."""
    partes = []
    for cod, lab in value_labels.items():
        sigla = abrev_perdido(lab)
        partes.append(f"{cod}:{sigla}" if sigla else f"{cod}={lab}")
    return "; ".join(partes)


def esquema_variables() -> str:
    """Texto compacto de las 145 variables crudas para el prompt del LLM.
    Una línea por variable: NOMBRE | etiqueta | códigos (si tiene labels)."""
    lineas = []
    for v in variables():
        nombre = v["nombre"]
        etiqueta = (v["etiqueta"] or "").strip()
        vl = v.get("value_labels") or {}
        if vl:
            lineas.append(f"- {nombre} | {etiqueta} | {_fmt_codigos(vl)}")
        else:
            lineas.append(f"- {nombre} | {etiqueta}")
    return "\n".join(lineas)


def leyenda_de(nombres) -> str:
    """Subconjunto de esquema_variables() para SOLO las variables crudas cuyo
    nombre está en `nombres`. Sirve para inyectarle al redactor la leyenda de
    codificaciones acotada a lo que aparece en el SQL ejecutado (no las 145)."""
    quiero = {n.lower() for n in nombres}
    lineas = []
    for v in variables():
        if v["nombre"].lower() not in quiero:
            continue
        etiqueta = (v["etiqueta"] or "").strip()
        vl = v.get("value_labels") or {}
        if vl:
            lineas.append(f"- {v['nombre']} | {etiqueta} | {_fmt_codigos(vl)}")
        else:
            lineas.append(f"- {v['nombre']} | {etiqueta}")
    return "\n".join(lineas)
