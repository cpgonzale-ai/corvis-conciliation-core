from app.core.compras_engine import reconcile_compras_with_rg, reconcile_compras_with_rg_iter_pares
from tests.fabricas import fila_compras as f


def categoria(libro, rg):
    res = reconcile_compras_with_rg([libro] if libro else [], [rg] if rg else [])
    return res[0]["diferencia"]


def test_coincide_cuando_todo_es_igual():
    assert categoria(f("A", 1000.0, 100.0), f("A", 1000.0, 100.0)) == "Coincide"


def test_diferencia_de_importe_cuando_difiere_el_total():
    assert categoria(f("A", 1000.0, 100.0), f("A", 1500.0, 100.0)) == "Diferencia de importe"


def test_diferencias_en_tasas_cuando_solo_difiere_una_tasa():
    assert categoria(f("A", 1000.0, 100.0), f("A", 1000.0, 300.0)) == "Diferencias en tasas"


def test_regla_1_tiene_precedencia_sobre_regla_2():
    assert categoria(f("A", 1000.0, 100.0), f("A", 1500.0, 500.0)) == "Diferencia de importe"


def test_tolerancia_de_100_gs_no_se_marca():
    assert categoria(f("A", 1000.0, 100.0), f("A", 1000.05, 100.0)) == "Coincide"


def test_no_llego_a_la_interfaz_y_no_existe_en_el_libro():
    assert categoria(f("A", 500.0), None) == "No llegó a la interfaz"
    assert categoria(None, f("B", 700.0)) == "No existe en el libro"


def test_nota_de_credito_con_mismo_monto_negativo_en_ambos_lados_coincide():
    # Regresión del bug corregido en esta sesión: la NC llega negativa de los dos lados y
    # la comparación debe ser por magnitud, no por signo.
    assert categoria(f("NC1", -100500.0, -9140.0), f("NC1", -100500.0, -9140.0)) == "Coincide"


def test_iter_pares_da_el_mismo_resultado_que_la_version_con_mapas():
    libro = [f("A", 1000.0, 100.0), f("B", 1000.0, 100.0), f("C", 1000.0, 100.0), f("D", 500.0)]
    rg = [f("A", 1000.0, 100.0), f("B", 1500.0, 100.0), f("C", 1000.0, 300.0), f("E", 700.0)]
    viejo = reconcile_compras_with_rg(libro, rg)
    mapa_l = {r["clave"]: r for r in libro}
    mapa_r = {r["clave"]: r for r in rg}
    claves = sorted(set(mapa_l) | set(mapa_r))
    nuevo = list(reconcile_compras_with_rg_iter_pares((c, mapa_l.get(c), mapa_r.get(c)) for c in claves))
    assert viejo == nuevo
    assert [d["diferencia"] for d in nuevo] == [
        "Coincide", "Diferencia de importe", "Diferencias en tasas", "No llegó a la interfaz", "No existe en el libro",
    ]
