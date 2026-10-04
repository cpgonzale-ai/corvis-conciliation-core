"""Test de caracterización del motor de Ventas (IngestionEngine._process_dataframe).

La salida esperada vive en tests/golden/*.json y se generó UNA vez con el motor validado.
Cualquier cambio de salida rompe la prueba, sea o no intencional. Para regenerar después de
un cambio intencional (revisado y aprobado):

    REGENERAR_GOLDEN=1 pytest tests/test_caracterizacion_ventas.py

Los datos de entrada son SINTÉTICOS (RUCs y nombres inventados): no usar archivos reales de
clientes en el repo.
"""

import json
import os
from pathlib import Path

import pandas as pd
import pytest

from app.core.engine import IngestionEngine

RAIZ = Path(__file__).resolve().parent.parent
PROFILES_DIR = RAIZ / "app" / "profiles"
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


@pytest.fixture(scope="module")
def motor():
    return IngestionEngine(str(PROFILES_DIR))


def _df_desde_perfil(profile: dict, filas: list[dict], ancho: int = 0) -> pd.DataFrame:
    """Arma el DataFrame con las columnas que el perfil declara. Los mapeos por nombre usan el
    source_name tal cual; los mapeos por posición (source_col) usan el índice de columna."""
    if any("source_col" in m for m in profile["column_mappings"]):
        columnas = list(range(max(ancho, max(m.get("source_col", 0) for m in profile["column_mappings"]) + 1)))
        datos = [[None] * len(columnas) for _ in filas]
        for fila_idx, fila in enumerate(filas):
            for celda in fila.get("celdas", []):
                datos[fila_idx][celda[0]] = celda[1]
        return pd.DataFrame(datos, columns=columnas)
    columnas = [m["source_name"] for m in profile["column_mappings"]]
    datos = []
    for fila in filas:
        datos.append([fila.get(m["target_field"]) for m in profile["column_mappings"]])
    return pd.DataFrame(datos, columns=columnas)


def _comparar_con_golden(nombre: str, salida) -> None:
    serializada = json.loads(json.dumps(salida, default=str, sort_keys=True))
    ruta = GOLDEN_DIR / f"{nombre}.json"
    if os.environ.get("REGENERAR_GOLDEN") == "1":
        GOLDEN_DIR.mkdir(exist_ok=True)
        ruta.write_text(json.dumps(serializada, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return
    esperado = json.loads(ruta.read_text(encoding="utf-8"))
    assert serializada == esperado


def test_caracterizacion_ventas_rg90_set(motor):
    """Camino vectorizado (sin marcador de sección): perfil RG90."""
    perfil = motor.profiles["rg90_set"]
    filas = [
        {"ruc": "80012345-6", "nombre_cliente": "Cliente Sintetico Uno", "tipo_documento": "FACTURA",
         "numero_comprobante": "001-001-0000001", "fecha": "02/05/2026", "gravada_10": 90909.09,
         "iva_10": 9090.91, "gravada_5": 0, "iva_5": 0, "exenta": 0, "total": 100000},
        {"ruc": "80012345-6", "nombre_cliente": "Cliente Sintetico Uno", "tipo_documento": "NOTA DE CRÉDITO",
         "numero_comprobante": "001-001-0000002", "fecha": "03/05/2026", "gravada_10": 45454.55,
         "iva_10": 4545.45, "gravada_5": 0, "iva_5": 0, "exenta": 0, "total": 50000},
        {"ruc": "", "nombre_cliente": None, "tipo_documento": "FACTURA ELECTRONICA",
         "numero_comprobante": "001-002-0000010", "fecha": "04/05/2026", "gravada_10": 0,
         "iva_10": 0, "gravada_5": 1000, "iva_5": 50, "exenta": 0, "total": 1050},
        {"ruc": "80099999-1", "nombre_cliente": "Cliente Sintetico Dos", "tipo_documento": "FACTURA",
         "numero_comprobante": "001-001-0000011", "fecha": "05/05/2026", "gravada_10": 0,
         "iva_10": 0, "gravada_5": 0, "iva_5": 0, "exenta": 0, "total": 0},
        {"ruc": "80012345-6", "nombre_cliente": "Cliente Sintetico Uno", "tipo_documento": "RECIBO",
         "numero_comprobante": "001-001-0000012", "fecha": "06/05/2026", "gravada_10": 1000,
         "iva_10": 100, "gravada_5": 0, "iva_5": 0, "exenta": 0, "total": 1100},
        {"ruc": "80012345-6", "nombre_cliente": "Cliente Sintetico Uno", "tipo_documento": "FACTURA",
         "numero_comprobante": "", "fecha": "07/05/2026", "gravada_10": 500,
         "iva_10": 50, "gravada_5": 0, "iva_5": 0, "exenta": 0, "total": 550},
    ]
    df = _df_desde_perfil(perfil, filas)
    filas_out, cortes = motor._process_dataframe(df, perfil, "Local Sintetico")
    assert cortes == []
    _comparar_con_golden("ventas_rg90_set", filas_out)


def test_caracterizacion_ventas_aloha_con_secciones_y_cortes(motor):
    """Camino con marcador de sección (Aloha): factura, nota de crédito, subtotales y anulada."""
    perfil = motor.profiles["aloha"]
    filas = [
        {"celdas": [(0, "001-001-0000001"), (1, "FACTURA"), (2, "80012345-6"), (3, "Cliente Sintetico Uno"),
                    (5, 90909.09), (6, 9090.91), (7, 100000), (8, "E")]},
        {"celdas": [(1, "NOTA DE CREDITO")]},
        {"celdas": [(0, "001-001-0000002"), (1, "2026-05-03"), (2, "80012345-6"), (3, "Cliente Sintetico Uno"),
                    (5, 45454.55), (6, 4545.45), (7, 50000), (8, "E")]},
        {"celdas": [(0, "Serie: 001"), (1, "Totales"), (5, 45454.55), (6, 4545.45), (7, 50000)]},
        {"celdas": [(0, "001-001-0000003"), (1, "2026-05-04"), (2, "X"), (3, None),
                    (5, 0), (6, 0), (7, 0), (8, "A")]},
    ]
    df = _df_desde_perfil(perfil, filas, ancho=9)
    filas_out, cortes = motor._process_dataframe(df, perfil, "Local Sintetico")
    _comparar_con_golden("ventas_aloha", {"filas": filas_out, "cortes": cortes})
