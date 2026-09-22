"""Schemas del export a Excel server-side (ver app/api/export.py y
auditoria/13-export-excel-wysiwyg.md) — se usan cuando el volumen de filas hace que armar
el .xlsx en el navegador (librería xlsx/SheetJS) sea inviable por memoria, no solo por
bloquear el hilo principal."""

from typing import Optional

from pydantic import BaseModel


class ExportTablaRequest(BaseModel):
    filename: str
    sheet_name: str
    headers: list[str]
    rows: list[list[str]]


class RG90DiffLadoIn(BaseModel):
    gravada_10: str
    iva_10: str
    gravada_5: str
    iva_5: str
    exenta: str
    total: str


class RG90DiffRowIn(BaseModel):
    doc: str
    tipo_doc: str
    sistema: str
    local: str
    libro: RG90DiffLadoIn
    rg90: RG90DiffLadoIn
    diferencia: str
    diferencias_detalle: Optional[dict[str, float]] = None


class ExportDiffVentasRequest(BaseModel):
    rows: list[RG90DiffRowIn]
    visible_columns: list[str]
