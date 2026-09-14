"""comun/presupuesto.py — Tope de gasto fail-closed, mensual y diario.

El tope de USD 300 del piloto era una intención, no un control: el sistema seguía
gastando. Acá se vuelve un corte real. Antes de CADA llamada al modelo se consulta el
gasto acumulado; si el período está agotado, no se llama y se le explica al usuario.

QUÉ NO CORTA. La caché (`comun/cache.py`) vive por encima de este tope: una pregunta
que se resuelve con respuesta cacheada no llama al modelo, así que no gasta y se
contesta igual. Es deliberado: cuando el presupuesto se agota, el sitio sigue
respondiendo lo que ya sabe en vez de quedar mudo.

DE DÓNDE SALE EL GASTO. De `logs/usage.jsonl`, que es el mismo registro con el que se
costea el piloto: no hay una segunda fuente de verdad que pueda divergir.

EL CONTADOR VIVE EN EL ARCHIVO, NO EN LA MEMORIA. Al arrancar se lee entero; después
cada verificación lee SOLO lo que se agregó desde la última vez (se guarda el offset
en bytes). Es tan barato como sumar en memoria y tiene dos propiedades que sumar en
memoria no tiene:

  · Si algún día el servicio arranca con más de un worker de uvicorn (hoy arranca con
    uno solo), cada proceso sumaría por su cuenta y el tope se multiplicaría por N en
    silencio. Leyendo del log, todos los procesos ven el MISMO gasto, porque todos
    escriben en el mismo archivo.
  · Si el archivo se achica —logrotate— se detecta y se reconstruye, en vez de seguir
    contando sobre un offset que ya no existe. Hoy NINGUNA regla de logrotate toca
    este directorio (verificado el 14-sep-2026), pero el día que alguien agregue una,
    el tope no se vuelve permisivo en silencio.

HUSO HORARIO. El día y el mes son los de Montevideo, no UTC: un tope diario que se
renueva a las 21:00 hora local no es un tope diario para quien lo usa. El log guarda
UTC y se convierte al leerlo.

SI EL LOG NO SE PUEDE LEER se arranca en cero y se avisa por stderr. Es la única
concesión: preferir que el servicio arranque a que un archivo corrupto lo deje mudo.
Las líneas sueltas ilegibles se saltean, igual que en costo_tokens2.py.
"""
import contextvars
import json
import os
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Montevideo")

TOPE_MES = float(os.environ.get("CENSO_TOPE_USD_MES", "300"))
TOPE_DIA = float(os.environ.get("CENSO_TOPE_USD_DIA", "25"))
HABILITADO = os.environ.get("CENSO_TOPE", "1") != "0"

_AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUTA_LOG = os.environ.get("CENSO_USAGE_LOG", os.path.join(_AQUI, "logs", "usage.jsonl"))

# USD por millón: (entrada, entrada cacheada, salida). Verificadas el 14-sep-2026
# contra developers.openai.com/api/docs/pricing. Esta tabla es la que MANDA: la de
# informes/costo_tokens2.py la refleja, y si divergen gana ésta, porque es la que
# decide si se llama al modelo.
PRECIOS = {
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
}
MODELO_SI_FALTA = "gpt-5.5"

# Una pregunta que YA gastó (su SQL salió) puede terminar aunque el tope se cruce
# en el medio: cortarla entre el SQL y el redactor paga la llamada cara y no entrega
# nada. El corte va en el borde de la PREGUNTA, no en el de la llamada.
_EN_CURSO = contextvars.ContextVar("consulta_ya_gasto", default=False)

_LOCK = threading.Lock()
_ESTADO = {"listo": False, "mes": None, "dia": None, "usd_mes": 0.0, "usd_dia": 0.0,
           "offset": 0}


class SinCupo(Exception):
    """El período agotó su tope. Lleva el mensaje que ve el usuario."""

    def __init__(self, periodo, gastado, tope, renueva):
        self.periodo = periodo          # "diario" | "mensual"
        self.gastado = gastado
        self.tope = tope
        self.renueva = renueva          # datetime con tz de Montevideo
        super().__init__("tope %s alcanzado (%.2f de %.2f USD)" % (periodo, gastado, tope))

    def mensaje(self):
        cuando = ("mañana a las 00:00" if self.periodo == "diario"
                  else "el %d de %s" % (self.renueva.day, _MESES[self.renueva.month - 1]))
        return ("Se agotó la cuota de consultas de este %s. El servicio vuelve a "
                "responder preguntas nuevas %s (hora de Uruguay). Mientras tanto, las "
                "preguntas que ya se hicieron antes siguen contestándose al instante."
                % ("día" if self.periodo == "diario" else "mes", cuando))


_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "setiembre", "octubre", "noviembre", "diciembre")


def precio(modelo):
    m = (modelo or MODELO_SI_FALTA).lower()
    for k in sorted(PRECIOS, key=len, reverse=True):
        if k in m:
            return PRECIOS[k]
    return None


def costo(prompt_tokens, cached_tokens, completion_tokens, modelo):
    p = precio(modelo)
    if p is None:
        return 0.0
    ent, ent_c, sal = p
    pt = prompt_tokens or 0
    ca = cached_tokens or 0
    ct = completion_tokens or 0
    return ((pt - ca) * ent + ca * ent_c + ct * sal) / 1_000_000


def _ahora():
    return datetime.now(TZ)


def _claves(t):
    return t.strftime("%Y-%m"), t.strftime("%Y-%m-%d")


def _sumar_linea(d, mes, dia):
    """Costo de una línea del log si cae en el mes/día en curso. (usd_mes, usd_dia)."""
    if d.get("etapa") == "fin" or not d.get("ts"):
        return 0.0, 0.0
    try:
        local = datetime.fromisoformat(d["ts"]).astimezone(TZ)
    except ValueError:
        return 0.0, 0.0
    m2, d2 = _claves(local)
    if m2 != mes:
        return 0.0, 0.0
    c = costo(d.get("prompt_tokens"), d.get("cached_tokens"),
              d.get("completion_tokens"), d.get("modelo"))
    return c, (c if d2 == dia else 0.0)


def _leer(desde):
    """Lee el log desde `desde` bytes. Devuelve (usd_mes, usd_dia, offset_final)."""
    mes, dia = _ESTADO["mes"], _ESTADO["dia"]
    um = ud = 0.0
    with open(RUTA_LOG, "rb") as f:
        f.seek(desde)
        crudo = f.read()
        fin = f.tell()
    # Si el archivo termina en una línea a medio escribir, se deja para la próxima:
    # el offset retrocede hasta el último salto de línea completo.
    corte = crudo.rfind(b"\n")
    if corte == -1:
        return 0.0, 0.0, desde
    fin = desde + corte + 1
    for raw in crudo[:corte].split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        a, b = _sumar_linea(d, mes, dia)
        um += a
        ud += b
    return um, ud, fin


def _reconstruir():
    """Lee el log ENTERO y fija los acumuladores del mes y del día en curso."""
    mes, dia = _claves(_ahora())
    _ESTADO.update(mes=mes, dia=dia, usd_mes=0.0, usd_dia=0.0, offset=0)
    try:
        um, ud, fin = _leer(0)
        _ESTADO.update(usd_mes=um, usd_dia=ud, offset=fin)
    except FileNotFoundError:
        pass
    except Exception as e:                                    # noqa: BLE001
        import sys
        print("presupuesto: no se pudo leer %s (%s); se arranca en cero"
              % (RUTA_LOG, e), file=sys.stderr)
    _ESTADO["listo"] = True


def _sincronizar():
    """Suma lo que se escribió en el log desde la última lectura."""
    try:
        tam = os.path.getsize(RUTA_LOG)
    except OSError:
        return
    if tam < _ESTADO["offset"]:
        import sys
        print("presupuesto: %s se achicó (%d < %d): rotación. Se reconstruye."
              % (RUTA_LOG, tam, _ESTADO["offset"]), file=sys.stderr)
        _reconstruir()
        return
    if tam == _ESTADO["offset"]:
        return
    try:
        um, ud, fin = _leer(_ESTADO["offset"])
    except OSError:
        return
    _ESTADO["usd_mes"] += um
    _ESTADO["usd_dia"] += ud
    _ESTADO["offset"] = fin


def _al_dia():
    """Reconstruye la primera vez y rota los acumuladores al cambiar de período."""
    if not _ESTADO["listo"]:
        _reconstruir()
        return
    mes, dia = _claves(_ahora())
    if mes != _ESTADO["mes"] or dia != _ESTADO["dia"]:
        # Cambió el período: los acumuladores del anterior no sirven y el offset
        # tampoco, porque hay que volver a decidir qué líneas caen en el período
        # nuevo. Se reconstruye del archivo, que es la única fuente.
        _reconstruir()


def abrir_consulta():
    """Marca el inicio de una pregunta nueva. La llama app/main.py:_responder()."""
    _EN_CURSO.set(False)


def verificar():
    """Lanza SinCupo si el período está agotado. Se llama ANTES de cada llamada."""
    if not HABILITADO or _EN_CURSO.get():
        return
    with _LOCK:
        _al_dia()
        _sincronizar()
        t = _ahora()
        if _ESTADO["usd_dia"] >= TOPE_DIA:
            manana = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            raise SinCupo("diario", _ESTADO["usd_dia"], TOPE_DIA, manana)
        if _ESTADO["usd_mes"] >= TOPE_MES:
            prox = (t.replace(day=28) + timedelta(days=5)).replace(day=1)
            raise SinCupo("mensual", _ESTADO["usd_mes"], TOPE_MES, prox)
    # Autorizada: el resto de las llamadas de ESTA pregunta no vuelven a consultar,
    # para no cortarla entre el SQL y el redactor.
    _EN_CURSO.set(True)


def sumar(modelo, usage):
    """Ya no acumula nada: el contador es el propio log.

    Se conserva la función —y su llamada en comun/llm.py— porque el punto donde se
    llamaba es el correcto si alguna vez hace falta un acumulador en memoria. Sumar
    acá Y leer del archivo contaría dos veces.
    """
    return


def estado():
    """Para diagnóstico: cuánto se lleva gastado y cuánto queda."""
    with _LOCK:
        _al_dia()
        _sincronizar()
        return {"mes": _ESTADO["mes"], "usd_mes": round(_ESTADO["usd_mes"], 4),
                "tope_mes": TOPE_MES, "dia": _ESTADO["dia"],
                "usd_dia": round(_ESTADO["usd_dia"], 4), "tope_dia": TOPE_DIA,
                "habilitado": HABILITADO}
