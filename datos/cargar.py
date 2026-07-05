"""
cargar.py — Construye datos/censo.db desde los MICRODATOS del Censo 2011 (.sav).

v4: carga las 145 variables del INE con sus valores CRUDOS (tal cual, NULL donde
falte) MÁS las columnas DERIVADAS legibles de v3 (departamento, sexo, edad,
asc_afro, asc_principal, nbi, codsec, codloc) y las keys (hogar_key, vivienda_key).
Lee el .sav en chunks (no genera CSV intermedio). El diccionario (datos/
diccionario.json) define el orden, nombre y tipo de las 145 crudas.

Uso:  python datos/cargar.py [ruta.sav] [ruta_salida.db]
      (por defecto: datos/Base unificada Viv_Hog_Pers.sav -> datos/censo.db)

Las filas se aceptan/descartan con la MISMA lógica que v3 (departamento, sexo y
edad válidos; imputados por moradores ausentes incluidos) para reproducir el
total de 3.285.824 personas.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pyreadstat

AQUI = Path(__file__).parent
sys.path.insert(0, str(AQUI))
from convertir_ine import (  # noqa: E402  (constantes de derivación de v3)
    DEPARTAMENTOS, SEXO, ASC_AFRO, ASC_PRINCIPAL, NBI_VALIDOS,
    _reparar_texto, _codigo,
)

DEFAULT_SAV = AQUI / "Base unificada Viv_Hog_Pers.sav"
DEFAULT_DB = AQUI / "censo.db"
DICCIONARIO = AQUI / "diccionario.json"
LOCALIDADES = AQUI / "localidades_2011.csv"
LOTE = 200_000

# Columnas crudas que además son columnas "conservadas" de v3: se guardan con la
# MISMA transformación que aplicaba v3 (no crudas), para no romper la regresión.
COL_DPTO, COL_SEXO, COL_EDAD = "DPTO", "PERPH02", "PERNA01"
COL_AFRO, COL_ASCP, COL_NBI = "PERER01_1", "PERER02", "NBI_CANTIDAD"
COL_IDV, COL_HOGID = "ID_VIVIENDA", "HOGID"
COL_SECC, COL_LOC, COL_BARRIO, COL_CCZ = "SECC", "LOC", "BARRIO85", "CCZ"
OVERLAP_V3 = {COL_SECC, COL_LOC, COL_BARRIO, COL_CCZ}

# Columnas derivadas que se agregan además de las 145 crudas (orden estable).
DERIVADAS = [
    ("departamento", "TEXT"), ("sexo", "TEXT"), ("edad", "INTEGER"),
    ("asc_afro", "TEXT"), ("asc_principal", "TEXT"), ("nbi", "INTEGER"),
    ("codsec", "INTEGER"), ("codloc", "INTEGER"),
    ("hogar_key", "TEXT"), ("vivienda_key", "TEXT"),
]

# Tipo SQLite de las 4 crudas que solapan con v3 (se guardan como en v3).
TIPO_OVERLAP = {"SECC": "TEXT", "LOC": "TEXT", "BARRIO85": "TEXT", "CCZ": "INTEGER"}


def _q(nombre: str) -> str:
    """Identificador SQL entre comillas dobles (nombres con acentos: Años_estudio)."""
    return '"' + nombre.replace('"', '""') + '"'


def _raw_num(x):
    """Valor crudo numérico tal cual: int si es entero, float si no, None si falta."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return int(f) if f.is_integer() else f


def _raw_txt(x):
    """Valor crudo de texto tal cual (stripeado), None si falta."""
    if x is None:
        return None
    if isinstance(x, float) and x != x:  # NaN
        return None
    s = str(x).strip()
    return s or None


def _cargar_diccionario():
    with open(DICCIONARIO, encoding="utf-8") as f:
        d = json.load(f)
    return [(v["nombre"], v["tipo"]) for v in d["variables"]]


def _tipo_sqlite(nombre, tipo_sav):
    if nombre in TIPO_OVERLAP:
        return TIPO_OVERLAP[nombre]
    return "TEXT" if tipo_sav == "string" else "NUMERIC"


def construir(sav: Path, db: Path) -> None:
    raw = _cargar_diccionario()                    # [(nombre, tipo_sav), ...] x145
    raw_nombres = [n for n, _ in raw]
    es_string = {n: (t == "string") for n, t in raw}
    columnas = raw_nombres + [n for n, _ in DERIVADAS]

    con = sqlite3.connect(db)
    con.execute("DROP TABLE IF EXISTS personas")
    defs = [f"{_q(n)} {_tipo_sqlite(n, t)}" for n, t in raw]
    defs += [f"{_q(n)} {tsql}" for n, tsql in DERIVADAS]
    con.execute(f"CREATE TABLE personas ({', '.join(defs)})")
    placeholders = ",".join("?" * len(columnas))
    insert = f"INSERT INTO personas ({', '.join(_q(n) for n in columnas)}) VALUES ({placeholders})"

    total = descartadas = imputadas = 0
    lote = []

    def fila_a_tupla(idx, valores):
        """valores: tupla en orden raw_nombres. Devuelve la fila completa
        (crudas + derivadas) o None si la fila se descarta (lógica v3)."""
        nonlocal imputadas
        g = lambda nombre: valores[idx[nombre]]

        # --- Aceptación de fila (idéntica a v3) ---
        ma = _codigo(g("MA")) if "MA" in idx else None
        if ma == 1:
            imputadas += 1
        try:
            dpto_int = int(g(COL_DPTO))
            departamento = DEPARTAMENTOS[dpto_int]
            sexo = SEXO[int(g(COL_SEXO))]
            edad = int(g(COL_EDAD))
            if not 0 <= edad <= 115:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            return None

        # --- Derivadas (semántica v3) ---
        asc_afro = ASC_AFRO.get(_codigo(g(COL_AFRO)))
        asc_principal = ASC_PRINCIPAL.get(_codigo(g(COL_ASCP)))
        cod_nbi = _codigo(g(COL_NBI))
        nbi = cod_nbi if cod_nbi in NBI_VALIDOS else None
        idv = _raw_txt(g(COL_IDV))
        cod_hog = _codigo(g(COL_HOGID))
        hogar_key = f"{idv}-{cod_hog}" if idv and cod_hog is not None else None
        vivienda_key = idv
        cod_secc = _codigo(g(COL_SECC))
        cod_loc = _codigo(g(COL_LOC))
        codsec = dpto_int * 100 + cod_secc if cod_secc is not None else None
        codloc = dpto_int * 1000 + cod_loc if cod_loc is not None else None

        # --- Crudas: valor tal cual, salvo las 4 que conserva v3 ---
        crudas = []
        for n in raw_nombres:
            v = valores[idx[n]]
            if n == COL_SECC or n == COL_LOC:
                crudas.append(_raw_txt(v))                 # v3: texto stripeado
            elif n == COL_BARRIO:
                t = _raw_txt(v)
                crudas.append(_reparar_texto(t) if t else None)  # v3: acentos reparados
            elif n == COL_CCZ:
                crudas.append(_codigo(v))                  # v3: entero
            elif es_string[n]:
                crudas.append(_raw_txt(v))
            else:
                crudas.append(_raw_num(v))

        return crudas + [departamento, sexo, edad, asc_afro, asc_principal,
                         nbi, codsec, codloc, hogar_key, vivienda_key]

    # pyreadstat entrega los chunks en el orden de columnas del .sav (= diccionario).
    lector = pyreadstat.read_file_in_chunks(pyreadstat.read_sav, str(sav), chunksize=LOTE)
    for df, _meta in lector:
        idx = {n: i for i, n in enumerate(df.columns)}
        faltan = [n for n in raw_nombres if n not in idx]
        if faltan:
            sys.exit(f"El .sav no tiene las columnas del diccionario: {faltan[:5]} ...")
        for valores in df.itertuples(index=False, name=None):
            fila = fila_a_tupla(idx, valores)
            if fila is None:
                descartadas += 1
                continue
            lote.append(fila)
            if len(lote) >= 100_000:
                con.executemany(insert, lote)
                total += len(lote)
                lote = []
        print(f"  … {total:,} personas cargadas", flush=True)
    if lote:
        con.executemany(insert, lote)
        total += len(lote)

    _crear_indices(con)
    _cargar_localidades(con)
    con.commit()
    con.close()
    print(f"\nOK: {total:,} personas en {db}")
    print(f"Imputados por moradores ausentes (MA=1) incluidos: {imputadas:,}")
    if descartadas:
        print(f"Filas descartadas (depto/sexo/edad inválidos): {descartadas:,}")


def _crear_indices(con) -> None:
    # Índices de v3 + PERID (número de persona) + vivienda_key (nueva key v4).
    for col in ("departamento", "edad", "asc_afro", "nbi", "hogar_key",
                "codsec", "codloc", "CCZ", "BARRIO85", "PERID", "vivienda_key"):
        con.execute(f"CREATE INDEX {_q('ix_' + col.lower())} ON personas({_q(col)})")


def _cargar_localidades(con) -> None:
    """Tabla de referencia localidades(codloc, nombre, departamento). No se toca
    respecto de v3."""
    import csv
    if not LOCALIDADES.exists():
        sys.exit(f"No existe {LOCALIDADES}. Es metadata geográfica pública del INE.")
    con.execute("DROP TABLE IF EXISTS localidades")
    con.execute(
        "CREATE TABLE localidades (codloc INTEGER PRIMARY KEY, nombre TEXT, departamento TEXT)"
    )
    with open(LOCALIDADES, newline="", encoding="utf-8") as f:
        filas = [
            (int(r["codloc"]), r["nombre"].strip(), r["departamento"].strip().upper())
            for r in csv.DictReader(f)
        ]
    con.executemany("INSERT OR REPLACE INTO localidades VALUES (?,?,?)", filas)
    print(f"OK: {len(filas):,} localidades cargadas")


if __name__ == "__main__":
    sav = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SAV
    db = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DB
    if not sav.exists():
        sys.exit(f"No existe el .sav: {sav}")
    construir(sav, db)
