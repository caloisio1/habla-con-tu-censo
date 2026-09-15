"""comun/funciones.py — Funciones SQL que ningún validador deja pasar, en los cuatro censos.

Los validadores controlan tablas y columnas con listas blancas; las funciones
escalares no. Casi todas son aritmética o texto y no importan. Estas sí: devuelven
configuración del motor, variables de entorno, secretos, el catálogo o la sesión, o
tocan archivos y extensiones. Ninguna pregunta sobre el censo las necesita.

Hasta el 15-sep-2026 cada validador tenía su propia lista, heredada de SQLite
(load_extension, readfile...), y ninguna incluía las de DuckDB. En la prueba
adversarial del 15-sep, `current_setting(...)` pasaba en 1996, 2004 y 2011. 2023 la
rechazaba, pero no por la función sino porque exige SUM(W): con SUM(W) en la
consulta, pasaba igual. Por eso la lista es UNA y la usan los cuatro.

Es la primera capa. La segunda no depende de esta: comun/ejecutor.py abre cada base
con enable_external_access=false, así que leer un archivo, una URL, hacer COPY o
ATTACH falla en el motor aunque algo llegara a pasar el validador.
"""
from sqlglot import exp

NOMBRES = frozenset({
    # configuración, variables y secretos del motor
    "current_setting", "getenv", "getvariable", "which_secret",
    # catálogo, sesión y versión
    "current_database", "current_catalog", "current_schema", "current_schemas",
    "current_query", "current_user", "current_role", "session_user", "user",
    "version", "current_version", "in_search_path",
    "col_description", "obj_description", "shobj_description",
    "format_type", "format_pg_type",
    # archivos y extensiones (las que ya bloqueaban los validadores, de SQLite)
    "load_extension", "readfile", "writefile", "edit", "fsdir", "zipfile",
})

# Familias enteras: pg_* (compatibilidad Postgres), duckdb_* (catálogo interno),
# pragma_*, has_*_privilege, txid_*, sqlite_*, read_* y glob (archivos).
PREFIJOS = ("pg_", "duckdb_", "pragma_", "has_", "txid_", "sqlite_", "read_", "glob")

_ANONIMAS = tuple(c for c in (getattr(exp, "Anonymous", None),
                              getattr(exp, "AnonymousAggFunc", None)) if c)


def nombre(f):
    """Nombre de la función en minúsculas. sqlglot tipa algunas (CURRENT_DATABASE,
    CURRENT_VERSION) y deja otras como Anonymous: hay que mirar las dos formas."""
    return (f.name if isinstance(f, _ANONIMAS) else f.sql_name()).lower()


def prohibida(n):
    n = (n or "").lower()
    return n in NOMBRES or n.startswith(PREFIJOS)


def verificar(arbol, error):
    """Lanza `error` si en cualquier parte del árbol (proyección, WHERE, CTE,
    subconsulta) aparece una función prohibida."""
    for f in arbol.find_all(exp.Func):
        n = nombre(f)
        if prohibida(n):
            raise error(f"Función no permitida: {n}")
