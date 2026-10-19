from app.core.compras_engine import reconcile_compras_with_rg, reconcile_compras_with_rg_iter_pares, tipo_cruce_de, separar_clave, duplicados_en_filas
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
    mapa_l = {(r["clave"], tipo_cruce_de(r)): r for r in libro}
    mapa_r = {(r["clave"], tipo_cruce_de(r)): r for r in rg}
    claves = sorted(set(mapa_l) | set(mapa_r))
    nuevo = list(reconcile_compras_with_rg_iter_pares((c, mapa_l.get(c), mapa_r.get(c)) for c in claves))
    assert viejo == nuevo
    assert [d["diferencia"] for d in nuevo] == [
        "Coincide", "Diferencia de importe", "Diferencias en tasas", "No llegó a la interfaz", "No existe en el libro",
    ]


NC = "NOTA DE CRÉDITO"


def test_tipo_cruce_distingue_nota_de_credito_de_todo_lo_demas():
    assert tipo_cruce_de(f("A", 1.0, tipo=NC)) == "Nota de Crédito"
    assert tipo_cruce_de({"tipo_comprobante": "NC"}) == "Nota de Crédito"
    assert tipo_cruce_de({"tipo_doc": "Nota de Crédito"}) == "Nota de Crédito"
    assert tipo_cruce_de(f("A", 1.0, tipo="FACTURA")) == "Factura"
    assert tipo_cruce_de(f("A", 1.0, tipo="AUTOFACTURA")) == "Factura"
    assert tipo_cruce_de({}) == "Factura"  # fila vieja sin tipo


def test_factura_y_nota_de_credito_con_el_mismo_numero_no_se_cruzan_entre_si():
    # Mismo documento + RUC (misma clave), distinto tipo: son dos comprobantes distintos.
    libro = [f("X", 1000.0, tipo="FACTURA"), f("X", -400.0, tipo=NC)]
    rg = [f("X", 1000.0, tipo="FACTURA"), f("X", -400.0, tipo=NC)]
    res = reconcile_compras_with_rg(libro, rg)
    assert [d["diferencia"] for d in res] == ["Coincide", "Coincide"]
    assert sorted(d["tipo_doc"] for d in res) == ["Factura", "Nota de Crédito"]


def test_si_la_rg_solo_tiene_la_factura_la_nc_del_libro_queda_sin_pareja():
    res = reconcile_compras_with_rg([f("X", 1000.0), f("X", -400.0, tipo=NC)], [f("X", 1000.0)])
    assert sorted(d["diferencia"] for d in res) == ["Coincide", "No llegó a la interfaz"]


def test_separar_clave_divide_el_numero_de_comprobante_del_ruc():
    assert separar_clave("001-003-000021980102117") == ("001-003-0000219", "80102117")
    assert separar_clave("NC001-001-00000013509285") == ("NC001-001-0000001", "3509285")
    assert separar_clave("raro") == ("raro", "")  # no calza: se devuelve tal cual, sin fallar


def test_duplicados_en_filas_cuenta_por_tipo_numero_y_ruc():
    libro = [
        f("001-001-000069580129373", 1.0), f("001-001-000069580129373", 2.0),   # duplicado
        f("001-001-000069580129373", -1.0, tipo=NC),                              # NC: no es duplicado
        f("001-001-000070080129373", 1.0),
    ]
    items = duplicados_en_filas(libro, "Libro")
    assert items == [{
        "comprobante": "001-001-0000695", "ruc": "80129373", "tipo": "Factura",
        "cantidad": 2, "origen": "Libro",
    }]
    assert duplicados_en_filas([f("A", 1.0), f("B", 1.0)], "Libro") == []


def test_el_resultado_incluye_la_fecha_del_libro_o_la_de_la_rg_si_solo_esta_alli():
    libro = {**f("001-001-0000001", 100.0), "fecha": "05/10/2026"}
    rg = {**f("001-001-0000002", 100.0), "fecha": "06/10/2026"}
    res = {d["doc"]: d for d in reconcile_compras_with_rg([libro], [rg])}
    assert res["001-001-0000001"]["fecha"] == "05/10/2026"
    assert res["001-001-0000002"]["fecha"] == "06/10/2026"


def test_la_fecha_del_resultado_no_lleva_hora():
    from app.core.compras_engine import solo_fecha
    assert solo_fecha("2026-10-05 00:00:00") == "2026-10-05"
    assert solo_fecha("05/10/2026 13:45") == "05/10/2026"
    assert solo_fecha("2026-10-05T00:00:00") == "2026-10-05"
    assert solo_fecha("05/10/2026") == "05/10/2026"
    assert solo_fecha(None) == ""


def test_nota_de_credito_se_reconoce_con_o_sin_tildes_y_en_cualquier_variante():
    from app.core.compras_engine import es_nota_de_credito
    for t in ["NC", "NCE", "NOTA DE CRÉDITO", "NOTA DE CREDITO", "Nota de Crédito", "NOTA DE CRÉDITO ELECTRONICA",
              "NOTA DE CRÉDITO ELECTRÓNICA", "NOTA DE CREDITO ELECTRÓNICA", "NOTA DE CRÉDITO  ELECTRONICA"]:
        assert es_nota_de_credito(t), t
    for t in ["FACTURA", "FACTURA ELECTRÓNICA", "NOTA DE DÉBITO", "NOTA DE DÉBITO ELECTRONICA", "AUTOFACTURA", "", None]:
        assert not es_nota_de_credito(t), t


def test_factura_y_nota_de_credito_electronica_con_tilde_no_son_duplicado():
    fac = f("001-001-0000001" + "80016096", 100.0)
    nc = {**f("001-001-0000001" + "80016096", -100.0, tipo="NOTA DE CRÉDITO ELECTRÓNICA"), "tipo_doc": "Nota de Crédito"}
    assert duplicados_en_filas([fac, nc], "Libro") == []


def test_el_resultado_incluye_el_ruc_del_proveedor_con_dv_si_lo_trae():
    libro = {**f("001-001-0000001", 100.0), "ruc_proveedor": "80016096", "dv_proveedor": "7"}
    rg = {**f("001-001-0000002", 100.0), "ruc_proveedor": "80016096", "dv_proveedor": ""}
    res = {d["doc"]: d for d in reconcile_compras_with_rg([libro], [rg])}
    assert res["001-001-0000001"]["ruc"] == "80016096-7"
    assert res["001-001-0000002"]["ruc"] == "80016096"
