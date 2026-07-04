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
            hogar_key TEXT
        )"""
    )

    def _o_none(s):
        s = s.strip()
        return s or None

    with open(CSV, newline="", encoding="utf-8") as f:
        lote, total = [], 0
        for r in csv.DictReader(f):
            lote.append((
                r["departamento"].strip().upper(),
                r["sexo"].strip(),
                int(r["edad"]),
                _o_none(r["asc_afro"]),
                _o_none(r["asc_principal"]),
                int(r["nbi"]) if r["nbi"].strip() else None,
                _o_none(r["hogar_key"]),
            ))
            if len(lote) >= 100_000:   # ~3.5M rows: insert in batches
                con.executemany("INSERT INTO personas VALUES (?,?,?,?,?,?,?)", lote)
                total += len(lote)
                lote = []
        if lote:
            con.executemany("INSERT INTO personas VALUES (?,?,?,?,?,?,?)", lote)
            total += len(lote)

    con.execute("CREATE INDEX ix_depto ON personas(departamento)")
    con.execute("CREATE INDEX ix_edad ON personas(edad)")
    con.execute("CREATE INDEX ix_asc_afro ON personas(asc_afro)")
    con.execute("CREATE INDEX ix_nbi ON personas(nbi)")
    con.execute("CREATE INDEX ix_hogar_key ON personas(hogar_key)")
    con.commit()
    print(f"OK: {total:,} personas cargadas en {DB}")
    con.close()


if __name__ == "__main__":
    main()
