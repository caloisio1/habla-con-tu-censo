"""Tests del endurecimiento del 15-sep-2026. Run: pytest

Cuatro cosas que estaban abiertas en producción y que estos tests fijan:

1. /docs, /redoc y /openapi.json respondían 200.
2. Un censo inexistente ("1900") se respondía con el motor de 2023.
3. Las funciones que exponen el motor (current_setting y afines) pasaban en los
   validadores de 1996, 2004 y 2011. En 2023 las rechazaba la regla de SUM(W), no
   la función: con SUM(W) en la consulta pasaban igual. Por eso acá 2023 se prueba
   CON SUM(W).
4. DuckDB se abría con el acceso externo habilitado: sin el validador de por medio,
   SQL podía leer archivos del servidor.
"""
import json

import duckdb
import pytest

import usage_log
from app import sql_guard as guard_2011
from comun import ejecutor
import sql_guard_2023 as guard_2023
from sql_guard_historicos import GUARD_1996, GUARD_2004


# --- 3. funciones prohibidas en los cuatro validadores ----------------------

FUNCIONES = [
    "current_setting('threads')",
    "getenv('HOME')",
    "getvariable('x')",
    "which_secret('s3://x', 's3')",
    "version()",
    "current_database()",
    "current_schema()",
    "current_query()",
    "pg_typeof(1)",
    "txid_current()",
    "has_table_privilege('a', 'b')",
]

CONSULTAS = {
    "2011": (guard_2011.validar, guard_2011.SQLNoSeguro,
             "SELECT {f} AS s, COUNT(*) AS n FROM personas GROUP BY 1"),
    "2023": (guard_2023.validar, guard_2023.SQLNoSeguro,
             "SELECT {f} AS s, SUM(W) AS personas, COUNT(*) AS n FROM personas_2023 GROUP BY 1"),
    "1996": (GUARD_1996.validar, guard_2011.SQLNoSeguro,
             "SELECT {f} AS s, COUNT(*) AS n FROM personas_1996 GROUP BY 1"),
    "2004": (GUARD_2004.validar, guard_2011.SQLNoSeguro,
             "SELECT {f} AS s, COUNT(*) AS n FROM censo2004 GROUP BY 1"),
}


@pytest.mark.parametrize("censo", sorted(CONSULTAS))
@pytest.mark.parametrize("funcion", FUNCIONES)
def test_la_funcion_se_rechaza_por_ser_funcion(censo, funcion):
    validar, _, plantilla = CONSULTAS[censo]
    with pytest.raises(Exception) as e:
        validar(plantilla.format(f=funcion))
    # Tiene que ser la regla de funciones, no otra que la tape (como SUM(W) en 2023).
    assert "Función no permitida" in str(e.value), str(e.value)


@pytest.mark.parametrize("censo", sorted(CONSULTAS))
def test_tambien_en_el_where(censo):
    validar, _, plantilla = CONSULTAS[censo]
    sql = plantilla.format(f="1").replace("GROUP BY 1",
                                          "WHERE current_setting('threads') IS NOT NULL GROUP BY 1")
    with pytest.raises(Exception) as e:
        validar(sql)
    assert "Función no permitida" in str(e.value), str(e.value)


# --- 4. DuckDB sin acceso externo --------------------------------------------

@pytest.fixture
def base_nativa(tmp_path):
    """Una base nativa mínima. El ejecutor recibe el nombre .db y abre el .duckdb
    hermano, igual que en producción."""
    nat = tmp_path / "mini.duckdb"
    con = duckdb.connect(str(nat))
    con.execute("CREATE TABLE t AS SELECT range AS id FROM range(10)")
    con.close()
    (tmp_path / "secreto.txt").write_text("no debería leerse\n")
    return str(tmp_path / "mini.db"), str(tmp_path / "secreto.txt")


def test_la_base_nativa_responde(base_nativa):
    db, _ = base_nativa
    assert ejecutor.tuplas(db, "SELECT COUNT(*) FROM t") == [(10,)]


def test_el_acceso_externo_esta_apagado(base_nativa):
    db, _ = base_nativa
    assert ejecutor.tuplas(db, "SELECT current_setting('enable_external_access')") == [(False,)]


@pytest.mark.parametrize("plantilla", [
    "SELECT * FROM read_text('{archivo}')",
    "SELECT * FROM read_csv('{archivo}')",
    "SELECT COUNT(*) FROM '{archivo}'",
    "COPY (SELECT 1) TO '{archivo}.fuga'",
    "ATTACH '{archivo}.duckdb' AS otra",
    "SET enable_external_access=true",
])
def test_sql_no_toca_el_disco_aunque_no_haya_validador(base_nativa, plantilla):
    db, archivo = base_nativa
    with pytest.raises(Exception):
        ejecutor.tuplas(db, plantilla.format(archivo=archivo))


# --- 1 y 2. la app: sin documentación y con 400 para un censo inexistente -----

@pytest.fixture
def cliente(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    ruta = tmp_path / "usage.jsonl"
    monkeypatch.setattr(usage_log, "RUTA", str(ruta))     # nunca el log real
    return TestClient(main.app), ruta                     # sin `with`: no precalienta


@pytest.mark.parametrize("url", ["/docs", "/redoc", "/openapi.json"])
def test_no_hay_documentacion_automatica(cliente, url):
    c, _ = cliente
    assert c.get(url).status_code == 404


@pytest.mark.parametrize("url", ["/preguntar", "/preguntar_stream"])
def test_censo_inexistente_da_400_y_cierra_rechazada(cliente, url):
    c, ruta = cliente
    r = c.post(url, json={"texto": "¿Cuántas personas hay?", "censo": "1900"})
    assert r.status_code == 400
    cuerpo = r.json()
    assert cuerpo["ok"] is False and cuerpo["motivo"] == "censo_desconocido"
    assert "1900" not in cuerpo["respuesta"]          # no se repite lo que mandó
    lineas = [json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines()]
    assert [(x["etapa"], x["resultado"], x["veredicto"]) for x in lineas] == \
        [("fin", "rechazada", "CENSO_DESCONOCIDO")]
