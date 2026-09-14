"""comun/presupuesto.py — Tope de gasto fail-closed, mensual y diario.

El tope de USD 300 del piloto era una intención, no un control: el sistema seguía
gastando. Acá se vuelve un corte real. Antes de CADA llamada al modelo se consulta el
gasto acumulado; si el período está agotado, no se llama y se le explica al usuario.

QUÉ NO CORTA. La caché (`comun/cache.py`) vive por encima de este tope: una pregunta
que se resuelve con respuesta cacheada no llama al modelo, así que no gasta y se
contesta igual. Es deliberado: cuando el presupuesto se agota, el sitio sigue
respondiendo lo que ya sabe en vez de quedar mudo.

DE DÓNDE SALE EL GASTO. De `logs/usage.jsonl`, que es el mismo registro con el que se
costea el piloto: no hay una segunda fuente de verdad que pueda divergir. El archivo
se lee UNA vez al arrancar para reconstruir los acumuladores del mes y del día en
curso; después cada llamada suma en memoria. No se relee por pedido.

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
_ESTADO = {"listo": False, "mes": None, "dia": None, "usd_mes": 0.0, "usd_dia": 0.0}


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


def _reconstruir():
    """Suma del log el gasto del mes y del día en curso. Se corre UNA vez."""
    t = _ahora()
    mes, dia = _claves(t)
    usd_mes = usd_dia = 0.0
    try:
        with open(RUTA_LOG, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if d.get("etapa") == "fin" or not d.get("ts"):
                    continue
                try:
                    local = datetime.fromisoformat(d["ts"]).astimezone(TZ)
                except ValueError:
                    continue
                m2, d2 = _claves(local)
                if m2 != mes:
                    continue
                c = costo(d.get("prompt_tokens"), d.get("cached_tokens"),
                          d.get("completion_tokens"), d.get("modelo"))
                usd_mes += c
                if d2 == dia:
                    usd_dia += c
    except FileNotFoundError:
        pass
    except Exception as e:                                    # noqa: BLE001
        import sys
        print("presupuesto: no se pudo leer %s (%s); se arranca en cero"
              % (RUTA_LOG, e), file=sys.stderr)
    _ESTADO.update(listo=True, mes=mes, dia=dia, usd_mes=usd_mes, usd_dia=usd_dia)


def _al_dia():
    """Reconstruye la primera vez y rota los acumuladores al cambiar de período."""
    if not _ESTADO["listo"]:
        _reconstruir()
        return
    mes, dia = _claves(_ahora())
    if mes != _ESTADO["mes"]:
        _ESTADO.update(mes=mes, dia=dia, usd_mes=0.0, usd_dia=0.0)
    elif dia != _ESTADO["dia"]:
        _ESTADO.update(dia=dia, usd_dia=0.0)


def abrir_consulta():
    """Marca el inicio de una pregunta nueva. La llama app/main.py:_responder()."""
    _EN_CURSO.set(False)


def verificar():
    """Lanza SinCupo si el período está agotado. Se llama ANTES de cada llamada."""
    if not HABILITADO or _EN_CURSO.get():
        return
    with _LOCK:
        _al_dia()
        t = _ahora()
        if _ESTADO["usd_dia"] >= TOPE_DIA:
            manana = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            raise SinCupo("diario", _ESTADO["usd_dia"], TOPE_DIA, manana)
        if _ESTADO["usd_mes"] >= TOPE_MES:
            prox = (t.replace(day=28) + timedelta(days=5)).replace(day=1)
            raise SinCupo("mensual", _ESTADO["usd_mes"], TOPE_MES, prox)


def sumar(modelo, usage):
    """Acumula lo que costó una llamada ya hecha. Nunca rompe la consulta."""
    if not HABILITADO or usage is None:
        return
    try:
        det = getattr(usage, "prompt_tokens_details", None)
        c = costo(getattr(usage, "prompt_tokens", 0),
                  getattr(det, "cached_tokens", 0) if det is not None else 0,
                  getattr(usage, "completion_tokens", 0), modelo)
        _EN_CURSO.set(True)
        with _LOCK:
            _al_dia()
            _ESTADO["usd_mes"] += c
            _ESTADO["usd_dia"] += c
    except Exception:                                         # noqa: BLE001
        pass


def estado():
    """Para diagnóstico: cuánto se lleva gastado y cuánto queda."""
    with _LOCK:
        _al_dia()
        return {"mes": _ESTADO["mes"], "usd_mes": round(_ESTADO["usd_mes"], 4),
                "tope_mes": TOPE_MES, "dia": _ESTADO["dia"],
                "usd_dia": round(_ESTADO["usd_dia"], 4), "tope_dia": TOPE_DIA,
                "habilitado": HABILITADO}
