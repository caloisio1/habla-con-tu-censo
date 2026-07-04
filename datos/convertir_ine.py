"""
convertir_ine.py — Converts INE's official Census 2011 persons microdata file
(.sav from SPSS, or .dbf) to the project format (datos/personas.csv).

STEP 1 (always first) — inspect the real file's columns and labels:

    python datos/convertir_ine.py --inspeccionar datos/ARCHIVO.sav

STEP 2 — if the mapping below matches (or after adjusting it):

    python datos/convertir_ine.py datos/ARCHIVO.sav

Default mapping (standard INE 2011 dictionary — VERIFY with --inspeccionar):
    DPTO       -> departamento (code 1-19)
    PERPH02    -> sexo (1 = Hombres, 2 = Mujeres)
    PERNA01    -> edad (years)
    PERER01_1  -> asc_afro (1 = Si, 2 = No; 8/9/NaN = NULL)
    PERER02    -> asc_principal (1-6; 8/9/NaN = NULL)
    NBI_CANTIDAD -> nbi (0,1,2,3 donde 3 = "3 o más"; 8/9/5555/NaN = NULL)
    ID_VIVIENDA + HOGID -> hogar_key (HOGID solo es único dentro de la vivienda)
"""

import csv
import sys
from pathlib import Path

# ---- Adjust here if --inspeccionar shows different column names ----
COL_DEPARTAMENTO = "DPTO"
COL_SEXO = "PERPH02"
COL_EDAD = "PERNA01"
COL_IMPUTADO = "MA"   # 1 = persona imputada (moradores ausentes), 0 = censada
COL_ASC_AFRO = "PERER01_1"       # menciona ascendencia afro o negra
COL_ASC_PRINCIPAL = "PERER02"    # ascendencia principal (solo si declaró >1)
COL_NBI = "NBI_CANTIDAD"         # cantidad de NBI del hogar (topeada en 3)
COL_ID_VIVIENDA = "ID_VIVIENDA"
COL_HOGID = "HOGID"
COL_SECC = "SECC"        # sección censal (dentro del departamento)
COL_LOC = "LOC"          # localidad (dentro del departamento)
COL_BARRIO = "BARRIO85"  # barrio (solo Montevideo)
COL_CCZ = "CCZ"          # centro comunal zonal (solo Montevideo)
# --------------------------------------------------------------------

DEPARTAMENTOS = {
    1: "MONTEVIDEO", 2: "ARTIGAS", 3: "CANELONES", 4: "CERRO LARGO",
    5: "COLONIA", 6: "DURAZNO", 7: "FLORES", 8: "FLORIDA",
    9: "LAVALLEJA", 10: "MALDONADO", 11: "PAYSANDU", 12: "RIO NEGRO",
    13: "RIVERA", 14: "ROCHA", 15: "SALTO", 16: "SAN JOSE",
    17: "SORIANO", 18: "TACUAREMBO", 19: "TREINTA Y TRES",
}

SEXO = {1: "Hombres", 2: "Mujeres"}

# Códigos VERIFICADOS contra el .sav real. Todo lo que no esté acá (incluidos
# 8 = no relevado, 9 = ignorado/viv. colectivas, 5555 = secreto estadístico y
# los NaN) se escribe como celda vacía = NULL: perdidos que NO cuentan.
ASC_AFRO = {1: "Si", 2: "No"}
ASC_PRINCIPAL = {
    1: "Afro o Negra", 2: "Asiática o Amarilla", 3: "Blanca",
    4: "Indígena", 5: "Otra", 6: "Ninguna",
}
NBI_VALIDOS = {0, 1, 2, 3}  # 3 = "3 o más" (variable topeada)

AQUI = Path(__file__).parent
SALIDA = AQUI / "personas.csv"
LOTE = 200_000  # rows per chunk when reading .sav

CAMPOS = ["departamento", "sexo", "edad",
          "asc_afro", "asc_principal", "nbi", "hogar_key",
          "SECC", "LOC", "BARRIO85", "CCZ", "codsec", "codloc"]


def _reparar_texto(s: str) -> str:
    """Repara la corrupción de acentos del .sav (WINDOWS-1252 con el bit 0x40
    perdido en el rango de letras Latin-1). Los bytes 0x80-0xBF son letras
    acentuadas 0xC0-0xFF a las que les falta ese bit; se restaura con OR 0x40
    (ñ 0xF1 llega como ± 0xB1 -> se recupera; á/é/í/ó/ú idem). Las letras ya
    correctas (0xC0-0xFF) y el ASCII quedan intactos. Verificado contra el
    geojson de barrios: el único caso real en la base es ± -> ñ."""
    return "".join(
        chr(o | 0x40) if 0x80 <= (o := ord(c)) <= 0xBF else c for c in s
    )


def _codigo(v):
    """float/NaN/None -> código entero, o None si falta o no es numérico."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return int(f)


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

    requeridas = (COL_DEPARTAMENTO, COL_SEXO, COL_EDAD, COL_ASC_AFRO,
                  COL_ASC_PRINCIPAL, COL_NBI, COL_ID_VIVIENDA, COL_HOGID,
                  COL_SECC, COL_LOC, COL_BARRIO, COL_CCZ)
    faltan = [c for c in requeridas if c not in columnas]
    if faltan:
        print(f"\nATENCIÓN: no encuentro {faltan}. Ajustá el mapeo al inicio del script.")
    else:
        print("\nOK: todas las columnas del mapeo existen. Podés convertir.")


def convertir(ruta: Path, solo_censadas: bool = False) -> None:
    total, descartadas, imputadas = 0, 0, 0
    # Control d: peso de cada motivo por el que nbi queda NULL.
    nbi_nulos = {8: 0, 9: 0, 5555: 0, "NaN": 0, "otro": 0}

    with open(SALIDA, "w", newline="", encoding="utf-8") as f_out:
        escritor = csv.writer(f_out)
        escritor.writerow(CAMPOS)

        def procesar(filas_iter):
            nonlocal total, descartadas, imputadas
            for (depto_v, sexo_v, edad_v, ma_v,
                 afro_v, ascp_v, nbi_v, idv_v, hog_v,
                 secc_v, loc_v, barrio_v, ccz_v) in filas_iter:
                try:
                    es_imputada = ma_v is not None and int(float(ma_v)) == 1
                except (ValueError, TypeError):
                    es_imputada = False
                if es_imputada:
                    imputadas += 1
                    if solo_censadas:
                        continue
                try:
                    dpto_int = int(depto_v)
                    depto = DEPARTAMENTOS[dpto_int]
                    sexo = SEXO[int(sexo_v)]
                    edad = int(edad_v)
                    if not 0 <= edad <= 115:
                        raise ValueError
                except (KeyError, ValueError, TypeError):
                    descartadas += 1
                    continue

                # Variables nuevas: código no válido / faltante -> NULL (perdido).
                asc_afro = ASC_AFRO.get(_codigo(afro_v), "")
                asc_principal = ASC_PRINCIPAL.get(_codigo(ascp_v), "")

                cod_nbi = _codigo(nbi_v)
                if cod_nbi in NBI_VALIDOS:
                    nbi = str(cod_nbi)
                else:
                    nbi = ""  # NULL
                    if cod_nbi is None:
                        nbi_nulos["NaN"] += 1
                    elif cod_nbi in (8, 9, 5555):
                        nbi_nulos[cod_nbi] += 1
                    else:
                        nbi_nulos["otro"] += 1

                # hogar_key: ID_VIVIENDA + '-' + HOGID (entero dentro de la vivienda).
                idv = str(idv_v).strip() if idv_v is not None else ""
                cod_hog = _codigo(hog_v)
                hogar_key = f"{idv}-{cod_hog}" if idv and cod_hog is not None else ""

                # Geografía: SECC/LOC/BARRIO85/CCZ tal como vienen + derivadas.
                secc = str(secc_v).strip() if secc_v is not None else ""
                loc = str(loc_v).strip() if loc_v is not None else ""
                barrio = _reparar_texto(str(barrio_v).strip()) if barrio_v is not None else ""
                cod_secc = _codigo(secc_v)
                cod_loc = _codigo(loc_v)
                cod_ccz = _codigo(ccz_v)
                codsec = dpto_int * 100 + cod_secc if cod_secc is not None else ""
                codloc = dpto_int * 1000 + cod_loc if cod_loc is not None else ""
                ccz = cod_ccz if cod_ccz is not None else ""

                escritor.writerow([depto, sexo, edad,
                                   asc_afro, asc_principal, nbi, hogar_key,
                                   secc, loc, barrio, ccz, codsec, codloc])
                total += 1

        if ruta.suffix.lower() == ".sav":
            import pyreadstat
            _, meta = pyreadstat.read_sav(str(ruta), metadataonly=True)
            tiene_ma = COL_IMPUTADO in meta.column_names
            cols = [COL_DEPARTAMENTO, COL_SEXO, COL_EDAD, COL_ASC_AFRO,
                    COL_ASC_PRINCIPAL, COL_NBI, COL_ID_VIVIENDA, COL_HOGID,
                    COL_SECC, COL_LOC, COL_BARRIO, COL_CCZ]
            if tiene_ma:
                cols.append(COL_IMPUTADO)
            lector = pyreadstat.read_file_in_chunks(
                pyreadstat.read_sav, str(ruta), chunksize=LOTE, usecols=cols
            )
            for df, _ in lector:
                ma_serie = df[COL_IMPUTADO] if tiene_ma else [None] * len(df)
                procesar(zip(
                    df[COL_DEPARTAMENTO], df[COL_SEXO], df[COL_EDAD], ma_serie,
                    df[COL_ASC_AFRO], df[COL_ASC_PRINCIPAL], df[COL_NBI],
                    df[COL_ID_VIVIENDA], df[COL_HOGID],
                    df[COL_SECC], df[COL_LOC], df[COL_BARRIO], df[COL_CCZ],
                ))
                print(f"  … {total:,} personas procesadas", flush=True)
        elif ruta.suffix.lower() == ".dbf":
            from dbfread import DBF
            registros = DBF(str(ruta), load=False)
            filas = (
                (r.get(COL_DEPARTAMENTO), r.get(COL_SEXO), r.get(COL_EDAD),
                 r.get(COL_IMPUTADO), r.get(COL_ASC_AFRO), r.get(COL_ASC_PRINCIPAL),
                 r.get(COL_NBI), r.get(COL_ID_VIVIENDA), r.get(COL_HOGID),
                 r.get(COL_SECC), r.get(COL_LOC), r.get(COL_BARRIO), r.get(COL_CCZ))
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

    # Control d: desglose de nbi NULL por código de origen.
    total_nbi_null = sum(nbi_nulos.values())
    print(f"\nnbi NULL: {total_nbi_null:,} personas, desglosado por código de origen:")
    print(f"  8    (no relevado)                 {nbi_nulos[8]:>12,}")
    print(f"  9    (viviendas colectivas)        {nbi_nulos[9]:>12,}")
    print(f"  5555 (secreto estadístico INE)     {nbi_nulos[5555]:>12,}")
    print(f"  NaN  (sin dato)                    {nbi_nulos['NaN']:>12,}")
    if nbi_nulos["otro"]:
        print(f"  otro (código inesperado)           {nbi_nulos['otro']:>12,}")
    print("Siguiente paso: python datos/cargar.py")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if sys.argv[1] == "--inspeccionar":
        inspeccionar(Path(sys.argv[2]))
    else:
        args = [a for a in sys.argv[1:] if a != "--solo-censadas"]
        convertir(Path(args[0]), solo_censadas="--solo-censadas" in sys.argv)
