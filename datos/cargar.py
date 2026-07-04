"""
cargar.py — Builds datos/censo.db from Census 2011 MICRODATA.

Expected CSV format (datos/personas.csv), one row per person:
    departamento,sexo,edad
    MONTEVIDEO,Mujeres,34
    ...

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
            edad INTEGER NOT NULL
        )"""
    )

    with open(CSV, newline="", encoding="utf-8") as f:
        lote, total = [], 0
        for r in csv.DictReader(f):
            lote.append((
                r["departamento"].strip().upper(),
                r["sexo"].strip(),
                int(r["edad"]),
            ))
            if len(lote) >= 100_000:   # ~3.5M rows: insert in batches
                con.executemany("INSERT INTO personas VALUES (?,?,?)", lote)
                total += len(lote)
                lote = []
        if lote:
            con.executemany("INSERT INTO personas VALUES (?,?,?)", lote)
            total += len(lote)

    con.execute("CREATE INDEX ix_depto ON personas(departamento)")
    con.execute("CREATE INDEX ix_edad ON personas(edad)")
    con.commit()
    print(f"OK: {total:,} personas cargadas en {DB}")
    con.close()


if __name__ == "__main__":
    main()
