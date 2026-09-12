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

    formats = [
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%d/%m/%y",
        "%d-%m-%y"
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(s_val, fmt)
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
