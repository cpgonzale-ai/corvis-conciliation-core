"""
Parseo de fechas para SISCOM RG90.
Maneja los formatos heterogéneos con los que llegan las fechas en los distintos reportes
(seriales de Excel en float/int, DD/MM/YYYY, YYYY-MM-DD, timestamps con hora) y los convierte
a un string YYYY-MM-DD estándar internamente, más un formato DD/MM/YYYY para mostrar en la
interfaz.
"""

from datetime import datetime, date
import xlrd
from typing import Optional, Union

# Se recuerda, a nivel de módulo, cuál de los formatos de DATE_FORMATS funcionó la última
# vez: dentro de un mismo archivo, la enorme mayoría de las filas comparten el mismo formato
# de fecha (es el mismo reporte, exportado de la misma forma) — probarlo primero evita
# reintentar en cada fila los formatos que ya se sabe que no van a matchear. Medido en
# /auditoria/09-performance-backend-ingesta.md: 600.000 llamadas a strptime para 200.000
# filas (~3 por fecha, incluyendo format_display_date) — con esto, el caso común baja a 1
# intento en vez de 2 dentro de parse_date. Es solo un ORDEN de intento: si el formato
# recordado no matchea una fila puntual, se prueba la lista completa en el mismo orden de
# siempre — no cambia qué fechas se aceptan ni cómo se interpretan.
_ultimo_formato_fecha_ok: Optional[str] = None

DATE_FORMATS = [
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%Y/%m/%d",
    "%d/%m/%y",
    "%d-%m-%y",
]


def parse_date(value: Union[str, int, float, datetime, date]) -> Optional[str]:
    if value is None or str(value).strip() == "" or str(value).strip().lower() in ["nan", "null", "none", "—"]:
        return None

    # Objetos datetime/date ya parseados por pandas
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")

    # Serial de fecha de Excel como número (ej. 46144 o 46144.0)
    if isinstance(value, (int, float)):
        try:
            # Rango válido de seriales de Excel para las fechas que maneja el sistema (ej. 30000 a 60000)
            if 10000 <= value <= 80000:
                dt_tuple = xlrd.xldate_as_tuple(value, 0)
                dt = datetime(*dt_tuple[:3])
                return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    s_val = str(value).strip()

    # Serial de Excel como string numérico (ej. "46144.0")
    try:
        f_val = float(s_val)
        if 10000 <= f_val <= 80000:
            dt_tuple = xlrd.xldate_as_tuple(f_val, 0)
            dt = datetime(*dt_tuple[:3])
            return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass

    # Timestamps con hora incluida (ej. "01/06/2026 00:00:00") — se descarta la hora
    if " " in s_val:
        s_val = s_val.split(" ")[0]

    global _ultimo_formato_fecha_ok
    if _ultimo_formato_fecha_ok is not None:
        try:
            dt = datetime.strptime(s_val, _ultimo_formato_fecha_ok)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass  # esta fila puntual no matchea el formato recordado -- se sigue abajo con la lista completa

    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(s_val, fmt)
            _ultimo_formato_fecha_ok = fmt
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None

def format_display_date(iso_date_str: Optional[str]) -> str:
    """Convierte de YYYY-MM-DD a DD/MM/YYYY para mostrar en la interfaz."""
    if not iso_date_str:
        return "—"
    try:
        dt = datetime.strptime(iso_date_str, "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return iso_date_str
