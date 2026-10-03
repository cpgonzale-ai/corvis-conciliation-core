from app.core.engine import _comparar_par
from tests.fabricas import fila_ventas as v


def categoria(libro, rg):
    return _comparar_par("052-001-9000001", libro, rg)["diferencia"]


def test_coincide_cuando_todo_es_igual():
    assert categoria(v(1000.0, 100.0), v(1000.0, 100.0)) == "Coincide"


def test_diferencia_de_importe_y_de_tasas():
    assert categoria(v(1000.0, 100.0), v(1500.0, 100.0)) == "Diferencia de importe"
    assert categoria(v(1000.0, 100.0), v(1000.0, 300.0)) == "Diferencias en tasas"


def test_comprobante_anulado_que_no_llega_a_la_rg_es_anulada_y_no_una_diferencia():
    assert categoria(v(0.0, estado="Anulada"), None) == "Anulada"


def test_no_llego_a_la_rg_y_no_existe_en_el_libro():
    assert categoria(v(500.0), None) == "No llegó a la interfaz"
    assert categoria(None, v(700.0)) == "No existe en el libro"


def test_nota_de_credito_exacta_en_ambos_lados_coincide():
    # Regresión del bug de esta sesión: la RG90 también informa las NC en negativo, y el
    # control debe comparar magnitudes para no inventar una diferencia de 201.000.
    nc = v(-100500.0, -9140.0, tipo_doc="Nota de Crédito")
    assert categoria(dict(nc), dict(nc)) == "Coincide"


def test_regla_1_tiene_precedencia_sobre_regla_2():
    assert categoria(v(1000.0, 100.0), v(1500.0, 500.0)) == "Diferencia de importe"
