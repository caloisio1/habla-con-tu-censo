"""Tests para _reparar_texto (corrupción de acentos WINDOWS-1252 del .sav). Run: pytest"""

from datos.convertir_ine import _reparar_texto


def test_repara_enie_corrupta():
    # ñ (0xF1) llega del .sav como ± (0xB1): bit 0x40 perdido.
    assert _reparar_texto("Ba±ados de Carrasco") == "Bañados de Carrasco"
    assert _reparar_texto("Larra±aga") == "Larrañaga"


def test_no_altera_ascii():
    assert _reparar_texto("Ciudad Vieja") == "Ciudad Vieja"
    assert _reparar_texto("Punta Carretas") == "Punta Carretas"


def test_no_altera_acentos_ya_correctos():
    # Letras 0xC0-0xFF ya tienen el bit 0x40: OR 0x40 es no-op.
    assert _reparar_texto("Peñarol") == "Peñarol"
    assert _reparar_texto("Bañados") == "Bañados"


def test_repara_rango_completo():
    # á(0xE1)->¡(0xA1), ó(0xF3)->³(0xB3): la corrupción y su inversa.
    assert _reparar_texto("¡") == "á"
    assert _reparar_texto("³") == "ó"
