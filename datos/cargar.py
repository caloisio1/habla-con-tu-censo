"""
cargar.py — Builds datos/censo.db from Census 2011 MICRODATA.

Expected CSV format (datos/personas.csv), one row per person:
    departamento,sexo,edad,asc_afro,asc_principal,nbi,hogar_key
    MONTEVIDEO,Mujeres,34,Si,,1,01010010003-1
    ...
Las cuatro columnas nuevas admiten celda vacía = NULL (perdido).

IMPORTANT: this repo does NOT ship census data. Download the official
anonymized public-use microdata from INE (https://www.ine.gub.uy) and map
its columns to the three fields above (a mapping script for the official
file layout lives in convertir_ine.py once we inspect the real file).

Run:  python datos/cargar.py
"""

import csv
import sqlite3
import sys
from pathlib import Path

AQUI = Path(__file__).parent
CSV = AQUI / "personas.csv"
DB = AQUI / "censo.db"
LOCALIDADES = AQUI / "localidades_2011.csv"


def main() -> None:
    if not CSV.exists():
        sys.exit(
            f"No existe {CSV}. Descargá los microdatos públicos del INE y convertilos.\n"
            "Formato esperado: departamento,sexo,edad (una fila por persona)"
        )

    con = sqlite3.connect(DB)
    con.execute("DROP TABLE IF EXISTS personas")
    con.execute(
        """CREATE TABLE personas (
            departamento TEXT NOT NULL,
            sexo TEXT NOT NULL,
            edad INTEGER NOT NULL,
            asc_afro TEXT,
            asc_principal TEXT,
            nbi INTEGER,
            hogar_key TEXT,
            SECC TEXT,
            LOC TEXT,
            BARRIO85 TEXT,
            CCZ INTEGER,
            codsec INTEGER,
            codloc INTEGER
        )"""
    )

    def _o_none(s):
        s = s.strip()
        return s or None

    def _i_none(s):
        s = s.strip()
        return int(s) if s else None

    with open(CSV, newline="", encoding="utf-8") as f:
        lote, total = [], 0
        for r in csv.DictReader(f):
            lote.append((
                r["departamento"].strip().upper(),
                r["sexo"].strip(),
                int(r["edad"]),
                _o_none(r["asc_afro"]),
                _o_none(r["asc_principal"]),
                _i_none(r["nbi"]),
                _o_none(r["hogar_key"]),
                _o_none(r["SECC"]),
                _o_none(r["LOC"]),
                _o_none(r["BARRIO85"]),
                _i_none(r["CCZ"]),
                _i_none(r["codsec"]),
                _i_none(r["codloc"]),
            ))
            if len(lote) >= 100_000:   # ~3.5M rows: insert in batches
                con.executemany("INSERT INTO personas VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", lote)
                total += len(lote)
                lote = []
        if lote:
            con.executemany("INSERT INTO personas VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", lote)
            total += len(lote)

    con.execute("CREATE INDEX ix_depto ON personas(departamento)")
    con.execute("CREATE INDEX ix_edad ON personas(edad)")
    con.execute("CREATE INDEX ix_asc_afro ON personas(asc_afro)")
    con.execute("CREATE INDEX ix_nbi ON personas(nbi)")
    con.execute("CREATE INDEX ix_hogar_key ON personas(hogar_key)")
    con.execute("CREATE INDEX ix_codsec ON personas(codsec)")
    con.execute("CREATE INDEX ix_codloc ON personas(codloc)")
    con.execute("CREATE INDEX ix_ccz ON personas(CCZ)")
    con.execute("CREATE INDEX ix_barrio ON personas(BARRIO85)")

    cargar_localidades(con)

    con.commit()
    print(f"OK: {total:,} personas cargadas en {DB}")
    con.close()


def cargar_localidades(con) -> None:
    """Tabla de referencia localidades(codloc, nombre, departamento) desde el
    CSV de metadata pública del INE (localidades_2011.csv)."""
    if not LOCALIDADES.exists():
        sys.exit(f"No existe {LOCALIDADES}. Es metadata geográfica pública del INE.")
    con.execute("DROP TABLE IF EXISTS localidades")
    con.execute(
        """CREATE TABLE localidades (
            codloc INTEGER PRIMARY KEY,
            nombre TEXT,
            departamento TEXT
        )"""
    )
    with open(LOCALIDADES, newline="", encoding="utf-8") as f:
        filas = [
            (int(r["codloc"]), r["nombre"].strip(), r["departamento"].strip().upper())
            for r in csv.DictReader(f)
        ]
    con.executemany("INSERT OR REPLACE INTO localidades VALUES (?,?,?)", filas)
    print(f"OK: {len(filas):,} localidades cargadas en {DB}")


if __name__ == "__main__":
    main()
