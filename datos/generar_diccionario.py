"""
generar_diccionario.py — Lee el .sav del INE (metadata only, sin cargar datos)
y escribe datos/diccionario.json con, por variable: nombre, etiqueta, tipo y
value labels completas.

El diccionario es METADATA pública del INE (etiquetas), no microdatos: va al repo.

Uso:  python datos/generar_diccionario.py [ruta_al_sav]
"""

import json
import sys
from pathlib import Path

import pyreadstat

AQUI = Path(__file__).parent
SALIDA = AQUI / "diccionario.json"
DEFAULT_SAV = AQUI / "Base unificada Viv_Hog_Pers.sav"


def _code_key(v):
    """Normaliza el código de una value label a string (1.0 -> '1')."""
    try:
        f = float(v)
        if f.is_integer():
            return str(int(f))
        return repr(f)
    except (TypeError, ValueError):
        return str(v)


def generar(ruta: Path) -> dict:
    _, meta = pyreadstat.read_sav(str(ruta), metadataonly=True)
    tipos = getattr(meta, "readstat_variable_types", None) or {}
    val_labels = meta.variable_value_labels or {}
    variables = []
    for nombre, etiqueta in zip(meta.column_names, meta.column_labels):
        vl = val_labels.get(nombre, {}) or {}
        variables.append({
            "nombre": nombre,
            "etiqueta": etiqueta or "",
            "tipo": tipos.get(nombre, ""),
            "value_labels": {_code_key(k): v for k, v in vl.items()},
        })
    return {
        "fuente": ruta.name,
        "n_filas": meta.number_rows,
        "n_variables": len(variables),
        "variables": variables,
    }


def main():
    ruta = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SAV
    if not ruta.exists():
        sys.exit(f"No existe el .sav: {ruta}")
    dicc = generar(ruta)
    SALIDA.write_text(
        json.dumps(dicc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"OK: {dicc['n_variables']} variables, {dicc['n_filas']:,} filas -> {SALIDA}")
    destacadas = ("PERER01_1", "PERER02", "NBI_CANTIDAD")
    for v in dicc["variables"]:
        if v["nombre"] in destacadas:
            print(f"\n{v['nombre']}  |  {v['etiqueta']}  |  tipo={v['tipo']}")
            if v["value_labels"]:
                for code, lab in v["value_labels"].items():
                    print(f"    {code} = {lab}")
            else:
                print("    (sin value labels)")


if __name__ == "__main__":
    main()
