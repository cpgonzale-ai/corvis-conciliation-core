"""Export a Excel armado en el servidor — para grillas que pueden llegar a 200.000 filas.

Por qué esto vive en el backend y no en el navegador (ver auditoria/13-export-excel-wysiwyg.md):
medido con un archivo real de 200.000 filas, XLSX.write (librería xlsx/SheetJS, la que usa
el frontend) revienta con "JavaScript heap out of memory" — más de 2GB de heap antes de
morir — sin importar si corre en el hilo principal o en un Web Worker. No es un problema de
que bloquee la interfaz (eso ya se resolvió con un worker), es que la librería en sí no es
apta para este volumen: arma todo el workbook + el .xlsx comprimido en memoria de una sola
vez, no en streaming.

openpyxl con Workbook(write_only=True) sí escribe fila por fila sin mantener todo el sheet
en memoria (usa un XMLWriter incremental por dentro) — es la misma razón por la que se usa
este modo acá y no el modo normal de openpyxl (que sí carga todo el workbook en memoria,
igual que SheetJS).

Las filas ya vienen filtradas por el frontend (fila por fila: filtros de columna + categoría
+ búsqueda ya aplicados, ver filteredRg90DiffCols/filteredRg90Rows en el frontend) — acá no
hay ninguna base de datos de la que recalcular eso, porque por diseño no se persiste ningún
comprobante fila por fila (ver el docstring de app/db/models.py). Lo que SÍ hace este
endpoint, del lado del servidor, es lo que el navegador no puede hacer sin agotar su memoria:
resolver el valor de cada celda visible (mismo cálculo que RG90_DIFF_COLUMNAS en el frontend,
ver diff_ventas_columnas de acá abajo) y armar el archivo .xlsx en sí.
"""

import io

import openpyxl
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.db.database import get_db
from app.db.models import Usuario
from app.schemas.export import ExportDiffVentasRequest, ExportTablaRequest, RG90DiffRowIn

router = APIRouter(prefix="/api/export", tags=["export"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_response(filename: str, sheet_name: str, headers: list[str], filas) -> StreamingResponse:
    """filas: iterable de listas de valores, ya en el orden de headers. write_only=True: la
    fila se escribe y se descarta, no queda un modelo de objetos por celda en memoria."""
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet(sheet_name)
    ws.append(headers)
    for fila in filas:
        ws.append(fila)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/tabla-excel")
def exportar_tabla_excel(
    payload: ExportTablaRequest,
    usuario: Usuario = Depends(get_current_user),
):
    """Genérico: el frontend ya resolvió headers/filas (valores ya formateados como texto,
    igual que en pantalla) — se usa para grillas sin cálculo de negocio propio por celda
    (ej. RG90 (SET) — Ventas, Paso 3), donde lo único que hacía falta mover al servidor era
    el armado del archivo en sí."""
    return _xlsx_response(payload.filename, payload.sheet_name, payload.headers, payload.rows)


# --- Detalle de Discrepancias (Paso 4) — mismo cálculo por celda que
# app/utils/diffVentasColumns.ts en el frontend (RG90_DIFF_COLUMNAS), para que el Excel sea
# idéntico a lo que arma la grilla en pantalla. ---

def _parse_gs(s: str) -> float:
    try:
        return float(str(s or "").strip().replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


def _format_gs(n: float) -> str:
    # Mismo swap que formatGs en el frontend (utils/format.ts): f"{n:,.2f}" da "1,234.56"
    # (formato en-US: coma de miles, punto decimal) -- se intercambian para el formato es-PY
    # ("1.234,56").
    s = f"{n:,.2f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def _valor_celda(d: RG90DiffRowIn, lado: str, campo: str) -> str:
    valor = getattr(d.libro if lado == "libro" else d.rg90, campo)
    if d.diferencia != "Diferencia de monto" or valor == "—":
        return valor
    detalle = d.diferencias_detalle or {}
    return valor if campo in detalle else "0,00"


def _diferencia_campo(d: RG90DiffRowIn, campo: str) -> str:
    return _format_gs(_parse_gs(_valor_celda(d, "libro", campo)) - _parse_gs(_valor_celda(d, "rg90", campo)))


# Mismas 6 etiquetas por lado que RG90_DIFF_COLUMNAS_PICKER en el frontend (utils/
# diffVentasColumns.ts) — a mano, no generadas con .title()/.replace(), para poder
# garantizar que coinciden EXACTO (ej. "IVA 10%", no "Iva 10 %").
_ETIQUETAS_CAMPO = {
    "gravada_10": "Gravada 10%",
    "gravada_5": "Gravada 5%",
    "iva_10": "IVA 10%",
    "iva_5": "IVA 5%",
    "exenta": "Exenta",
    "total": "Total",
}
_CAMPOS = list(_ETIQUETAS_CAMPO.keys())

DIFF_VENTAS_COLUMNAS: list[tuple[str, str, "callable"]] = [
    ("doc", "Documento", lambda d: d.doc),
    ("tipo_doc", "Tipo", lambda d: d.tipo_doc),
    ("sistema", "Sistema", lambda d: d.sistema),
    ("local", "Local", lambda d: d.local),
    *[(f"libro_{c}", f"Libro — {_ETIQUETAS_CAMPO[c]}", (lambda c: lambda d: _valor_celda(d, "libro", c))(c)) for c in _CAMPOS],
    *[(f"rg_{c}", f"RG90 — {_ETIQUETAS_CAMPO[c]}", (lambda c: lambda d: _valor_celda(d, "rg90", c))(c)) for c in _CAMPOS],
    *[(f"dif_{c}", f"Diferencia — {_ETIQUETAS_CAMPO[c]}", (lambda c: lambda d: _diferencia_campo(d, c))(c)) for c in _CAMPOS],
    ("diferencia", "Diferencia / Diagnóstico", lambda d: d.diferencia),
]


@router.post("/diff-ventas-excel")
def exportar_diff_ventas_excel(
    payload: ExportDiffVentasRequest,
    usuario: Usuario = Depends(get_current_user),
):
    columnas = [c for c in DIFF_VENTAS_COLUMNAS if c[0] in set(payload.visible_columns)]
    headers = [c[1] for c in columnas]

    def filas():
        for d in payload.rows:
            yield [c[2](d) for c in columnas]

    return _xlsx_response("Resultado_Comparacion_Ventas_RG90.xlsx", "Resultado — Ventas vs RG90", headers, filas())
