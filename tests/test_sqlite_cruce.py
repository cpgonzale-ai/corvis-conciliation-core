import json
import shutil

import pytest
from fastapi import HTTPException

from app.core.sqlite_cruce import armar_error_duplicados, insertar_lote_diagnosticando_duplicados, nueva_sqlite_temporal


@pytest.fixture
def con_temporal():
    con, tmp = nueva_sqlite_temporal(tabla_a="libro", tabla_b="rg")
    yield con
    con.close()
    shutil.rmtree(tmp, ignore_errors=True)


def test_duplicado_dentro_del_mismo_lote_se_reporta_con_conteo(con_temporal):
    lote = [("A1", "", json.dumps({"c": 1})), ("A1", "", json.dumps({"c": 2})), ("A2", "", "{}")]
    with pytest.raises(HTTPException) as e:
        insertar_lote_diagnosticando_duplicados(con_temporal, "libro", lote, None, origen_default="RG")
    detalle = e.value.detail
    assert detalle["tipo"] == "comprobantes_duplicados"
    assert detalle["detalle"][0]["comprobante"] == "A1"
    assert detalle["detalle"][0]["cantidad"] == 2


def test_con_acumulador_no_corta_y_acumula(con_temporal):
    acumulador = []
    lote = [("B1", "", "{}"), ("B1", "", "{}")]
    insertar_lote_diagnosticando_duplicados(con_temporal, "libro", lote, acumulador, origen_default="RG")
    assert acumulador and acumulador[0]["comprobante"] == "B1"


def test_compras_no_menciona_rg90_en_los_mensajes(con_temporal):
    acumulador = []
    insertar_lote_diagnosticando_duplicados(con_temporal, "rg", [("X", "", "{}"), ("X", "", "{}")], acumulador, origen_default="RG")
    error = armar_error_duplicados(acumulador, origen_default="RG")
    assert "RG90" not in error["mensaje"]
    assert "RG90" not in (error["aclaracion"] or "")


def test_ventas_mantiene_el_texto_rg90_por_defecto():
    error = armar_error_duplicados([{"origen": "Libro", "comprobante": "X", "tipo": "", "cantidad": 2}, {"origen": "RG90", "comprobante": "Y", "tipo": "", "cantidad": 2}])
    assert error["origen"] == "Ambos"
