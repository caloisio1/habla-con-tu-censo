"""
convertir_ine.py — Converts INE's official Census 2011 persons microdata file
(.sav from SPSS, or .dbf) to the project format (datos/personas.csv).

STEP 1 (always first) — inspect the real file's columns and labels:

    python datos/convertir_ine.py --inspeccionar datos/ARCHIVO.sav

STEP 2 — if the mapping below matches (or after adjusting it):

    python datos/convertir_ine.py datos/ARCHIVO.sav

Default mapping (standard INE 2011 dictionary — VERIFY with --inspeccionar):
    DPTO     -> departamento (code 1-19)
    PERPH02  -> sexo (1 = Hombres, 2 = Mujeres)
    PERNA01  -> edad (years)
"""

import csv
import sys
from pathlib import Path

# ---- Adjust here if --inspeccionar shows different column names ----
COL_DEPARTAMENTO = "DPTO"
COL_SEXO = "PERPH02"
COL_EDAD = "PERNA01"
COL_IMPUTADO = "MA"   # 1 = persona imputada (moradores ausentes), 0 = censada
# --------------------------------------------------------------------

DEPARTAMENTOS = {
    1: "MONTEVIDEO", 2: "ARTIGAS", 3: "CANELONES", 4: "CERRO LARGO",
    5: "COLONIA", 6: "DURAZNO", 7: "FLORES", 8: "FLORIDA",
    9: "LAVALLEJA", 10: "MALDONADO", 11: "PAYSANDU", 12: "RIO NEGRO",
    13: "RIVERA", 14: "ROCHA", 15: "SALTO", 16: "SAN JOSE",
    17: "SORIANO", 18: "TACUAREMBO", 19: "TREINTA Y TRES",
}

SEXO = {1: "Hombres", 2: "Mujeres"}

AQUI = Path(__file__).parent
SALIDA = AQUI / "personas.csv"
LOTE = 200_000  # rows per chunk when reading .sav


def inspeccionar(ruta: Path) -> None:
    if ruta.suffix.lower() == ".sav":
        import pyreadstat
        _, meta = pyreadstat.read_sav(str(ruta), metadataonly=True)
        print(f"Archivo SPSS: {meta.number_rows or '?'} filas, {len(meta.column_names)} columnas\n")
        for nombre, etiqueta in zip(meta.column_names, meta.column_labels):
            print(f"  {nombre:<15} {etiqueta or ''}")
        columnas = meta.column_names
    elif ruta.suffix.lower() == ".dbf":
        from dbfread import DBF
        tabla = DBF(str(ruta), load=False)
        columnas = tabla.field_names
        print(f"Archivo DBF: {len(columnas)} columnas\n")
        for c in columnas:
            print(f"  {c}")
    else:
        sys.exit("Formato no reconocido: usá un .sav o un .dbf")

    faltan = [c for c in (COL_DEPARTAMENTO, COL_SEXO, COL_EDAD) if c not in columnas]
    if faltan:
        print(f"\nATENCIÓN: no encuentro {faltan}. Ajustá el mapeo al inicio del script.")
    else:
        print("\nOK: las tres columnas del mapeo existen. Podés convertir.")


def _escribir(filas_iter, escritor) -> tuple[int, int]:
    total, descartadas = 0, 0
    for depto_v, sexo_v, edad_v in filas_iter:
        try:
            depto = DEPARTAMENTOS[int(depto_v)]
            sexo = SEXO[int(sexo_v)]
            edad = int(edad_v)
            if not 0 <= edad <= 115:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            descartadas += 1
            continue
        escritor.writerow([depto, sexo, edad])
        total += 1
    return total, descartadas


def convertir(ruta: Path, solo_censadas: bool = False) -> None:
    total, descartadas, imputadas = 0, 0, 0
    with open(SALIDA, "w", newline="", encoding="utf-8") as f_out:
        escritor = csv.writer(f_out)
        escritor.writerow(["departamento", "sexo", "edad"])

        def procesar(filas_iter):
            nonlocal total, descartadas, imputadas
            for depto_v, sexo_v, edad_v, ma_v in filas_iter:
                try:
                    es_imputada = ma_v is not None and int(float(ma_v)) == 1
                except (ValueError, TypeError):
                    es_imputada = False
                if es_imputada:
                    imputadas += 1
                    if solo_censadas:
                        continue
                try:
                    depto = DEPARTAMENTOS[int(depto_v)]
                    sexo = SEXO[int(sexo_v)]
                    edad = int(edad_v)
                    if not 0 <= edad <= 115:
                        raise ValueError
                except (KeyError, ValueError, TypeError):
                    descartadas += 1
                    continue
                escritor.writerow([depto, sexo, edad])
                total += 1

        if ruta.suffix.lower() == ".sav":
            import pyreadstat
            _, meta = pyreadstat.read_sav(str(ruta), metadataonly=True)
            tiene_ma = COL_IMPUTADO in meta.column_names
            cols = [COL_DEPARTAMENTO, COL_SEXO, COL_EDAD] + ([COL_IMPUTADO] if tiene_ma else [])
            lector = pyreadstat.read_file_in_chunks(
                pyreadstat.read_sav, str(ruta), chunksize=LOTE, usecols=cols
            )
            for df, _ in lector:
                ma_serie = df[COL_IMPUTADO] if tiene_ma else [None] * len(df)
                procesar(zip(df[COL_DEPARTAMENTO], df[COL_SEXO], df[COL_EDAD], ma_serie))
                print(f"  … {total:,} personas procesadas", flush=True)
        elif ruta.suffix.lower() == ".dbf":
            from dbfread import DBF
            registros = DBF(str(ruta), load=False)
            filas = (
                (r.get(COL_DEPARTAMENTO), r.get(COL_SEXO), r.get(COL_EDAD), r.get(COL_IMPUTADO))
                for r in registros
            )
            procesar(filas)
        else:
            sys.exit("Formato no reconocido: usá un .sav o un .dbf")

    print(f"\nOK: {total:,} personas escritas en {SALIDA}")
    if imputadas:
        accion = "excluidas" if solo_censadas else "incluidas"
        print(f"Personas imputadas por moradores ausentes (MA=1): {imputadas:,} ({accion}).")
        print("Referencia oficial: censada 3.252.091 | contabilizada (con imputados) 3.286.314")
    if descartadas:
        print(f"Aviso: {descartadas:,} filas descartadas por valores inválidos o faltantes.")
    print("Siguiente paso: python datos/cargar.py")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if sys.argv[1] == "--inspeccionar":
        inspeccionar(Path(sys.argv[2]))
    else:
        args = [a for a in sys.argv[1:] if a != "--solo-censadas"]
        convertir(Path(args[0]), solo_censadas="--solo-censadas" in sys.argv)
