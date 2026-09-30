"""Export a Excel armado en el servidor — para grillas que pueden llegar a 200.000 filas.

Por qué esto vive en el backend y no en el navegador (ver auditoria/13-export-excel-wysiwyg.md):
medido con un archivo real de 200.000 filas, XLSX.write (librería xlsx/SheetJS, la que usa
el frontend) revienta con "JavaScript heap out of memory" — más de 2GB de heap antes de
morir — sin importar si corre en el hilo principal o en un Web Worker. No es un problema de
que bloquee la interfaz (eso ya se resolvió con un worker), es que la librería en sí no es
apta para este volumen: arma todo el workbook + el .xlsx comprimido en memoria de una sola
vez, no en streaming.

openpyxl con Workbook(write_only=True) escribe fila por fila sin mantener todo el sheet en
memoria (usa un XMLWriter incremental por dentro) — es la misma razón por la que se usaba
este modo acá y no el modo normal de openpyxl (que sí carga todo el workbook en memoria,
igual que SheetJS).

CAMBIO (ver auditoria de tuning, /tmp o el pedido explícito que motivó esto): se midió
XlsxWriter contra openpyxl con el mismo archivo (100.000 filas x 23 columnas, mismo formato
de moneda) -- 2,6-2,9x más rápido (39-43s contra 111-120s). Con `constant_memory=True` Y
apuntando a un ARCHIVO TEMPORAL EN DISCO (no un buffer en memoria: con buffer en memoria el
pico de RAM medido fue de 530MB, reintroduciendo el mismo riesgo de OOM que este endpoint
existe para evitar) el pico de RAM medido fue de 18MB -- MENOS que openpyxl (34MB). Mismo
patrón que ya usa /api/reconcile para el cruce (SQLite en un archivo temporal, nunca
":memory:", por la misma razón: un buffer en RAM sigue contando como RSS del proceso).

Las filas ya vienen filtradas por el frontend (fila por fila: filtros de columna + categoría
+ búsqueda ya aplicados, ver filteredRg90DiffCols/filteredRg90Rows en el frontend) — acá no
hay ninguna base de datos de la que recalcular eso, porque por diseño no se persiste ningún
comprobante fila por fila (ver el docstring de app/db/models.py). Lo que SÍ hace este
endpoint, del lado del servidor, es lo que el navegador no puede hacer sin agotar su memoria:
resolver el valor de cada celda visible (mismo cálculo que RG90_DIFF_COLUMNAS en el frontend,
ver diff_ventas_columnas de acá abajo) y armar el archivo .xlsx en sí.

Números reales, no texto: los montos llegan formateados en latino ("18.891.429,00", mismo
texto que la grilla) — se convierten acá a number + formato de celda "#,##0.00" para que se
puedan sumar en Excel (antes se escribían como texto a propósito, para que el archivo
coincidiera con la pantalla sin importar la configuración regional de quien lo abre; se
revirtió esa regla a pedido explícito porque el cliente siempre abre estos archivos con
Excel configurado en es-PY, donde "#,##0.00" se sigue viendo igual que en pantalla — ver
`_parse_monto_latino`, mismo criterio que `parseMontoLatino` en utils/exportExcel.ts del
frontend). Documentos, RUCs y fechas nunca matchean el patrón y quedan como texto.
"""

import os
import re
import shutil
import tempfile

import xlsxwriter
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.db.database import get_db
from app.db.models import Usuario
from app.schemas.export import ExportDiffVentasRequest, ExportTablaRequest, RG90DiffRowIn

router = APIRouter(prefix="/api/export", tags=["export"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_MONTO_LATINO_RE = re.compile(r"^-?\d{1,3}(\.\d{3})*(,\d+)?$")
_MONTO_NUMFMT = "#,##0.00"


def _parse_monto_latino(s: str) -> float | None:
    """"18.891.429,00" -> 18891429.0; "-110.000,00" -> -110000.0. Documento, RUC, fecha o
    cualquier otro texto (incluido "—") no matchea el patrón y devuelve None."""
    t = s.strip()
    if not _MONTO_LATINO_RE.match(t):
        return None
    try:
        return float(t.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _xlsx_response(filename: str, sheet_name: str, headers: list[str], filas) -> StreamingResponse:
    """filas: iterable de listas de valores, ya en el orden de headers. constant_memory=True:
    XlsxWriter descarta cada fila de la hoja de la memoria apenas la escribe al archivo (no
    arma el sheet completo en RAM); el archivo en sí se abre en un temporal EN DISCO (no un
    buffer en memoria -- ver el porqué en el docstring del módulo) y se borra siempre en el
    finally de _generar_respuesta(), de abajo, pase lo que pase durante el streaming --
    mismo criterio que tmp_dir_sqlite en /api/reconcile (main.py). Cada valor de tipo str que
    matchea un monto latino se escribe como número con formato "#,##0.00"; el resto, como
    texto plano.
    """
    # mkdtemp (directorio), no mktemp (solo nombre): mismo criterio que tmp_dir_sqlite en
    # /api/reconcile (main.py) -- evita la ventana de carrera de mktemp, que solo reserva un
    # nombre sin crear nada, entre elegirlo y que xlsxwriter lo abra.
    tmp_dir = tempfile.mkdtemp(prefix="export_xlsx_")
    tmp_path = os.path.join(tmp_dir, "export.xlsx")
    wb = xlsxwriter.Workbook(tmp_path, {"constant_memory": True})
    ws = wb.add_worksheet(sheet_name)
    numfmt = wb.add_format({"num_format": _MONTO_NUMFMT})
    # write_string()/write_number() explícitos, no el write() genérico (que internamente
    # detecta el tipo del valor en cada llamada antes de despachar al método específico) --
    # medido: ~16% más rápido con el mismo archivo de salida, sin cambiar ni un valor ni un
    # formato. headers y filas son siempre str acá (ExportTablaRequest.rows: list[list[str]]
    # y las funciones de DIFF_VENTAS_COLUMNAS de más abajo devuelven str), así que
    # write_string() es siempre correcto para el caso "no es un monto".
    for col, valor in enumerate(headers):
        ws.write_string(0, col, valor)
    for row, fila in enumerate(filas, start=1):
        for col, valor in enumerate(fila):
            numero = _parse_monto_latino(valor) if isinstance(valor, str) else None
            if numero is None:
                ws.write_string(row, col, valor)
            else:
                ws.write_number(row, col, numero, numfmt)
    wb.close()

    def _generar_respuesta():
        try:
            with open(tmp_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return StreamingResponse(
        _generar_respuesta(),
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
    # Bug real corregido acá (mismo fix que diferenciaCampoVentas en el frontend, ver
    # utils/diffVentasColumns.ts): para una Nota de Crédito, el libro guarda el monto en
    # NEGATIVO mientras que la RG90 siempre lo informa en positivo -- restar los valores
    # CRUDOS (con signo) daba un número muy distinto al real (un caso de diferencia=500 se
    # exportaba como -200.500,00). Se usa directamente el valor que el backend ya calculó
    # correctamente al comparar (guardado en diferencias_detalle), en vez de recalcularlo acá
    # con una resta que no contempla el signo de las NC.
    detalle = d.diferencias_detalle or {}
    if d.diferencia == "Diferencia de monto" and campo in detalle:
        return _format_gs(detalle[campo])
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
