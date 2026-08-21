"""
Date Parser Module for SISCOM RG90.
Handles heterogeneous date inputs (Excel serial floats/integers, DD/MM/YYYY, YYYY-MM-DD, timestamps)
and converts them into standard YYYY-MM-DD strings and DD/MM/YYYY formatted output.
"""

from datetime import datetime, date
import xlrd
from typing import Optional, Union

EXCEL_EPOCH = datetime(1899, 12, 30)

def parse_date(value: Union[str, int, float, datetime, date]) -> Optional[str]:
    if value is None or str(value).strip() == "" or str(value).strip().lower() in ["nan", "null", "none", "—"]:
        return None

    # Handle datetime / date objects
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")

    # Handle Excel float/integer serial dates (e.g. 46144 or 46144.0)
    if isinstance(value, (int, float)):
        try:
            # Check if within valid Excel serial range (e.g. 30000 to 60000)
            if 10000 <= value <= 80000:
                dt_tuple = xlrd.xldate_as_tuple(value, 0)
                dt = datetime(*dt_tuple[:3])
                return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    s_val = str(value).strip()

    # Try numeric string Excel float (e.g. "46144.0")
    try:
        f_val = float(s_val)
        if 10000 <= f_val <= 80000:
            dt_tuple = xlrd.xldate_as_tuple(f_val, 0)
            dt = datetime(*dt_tuple[:3])
            return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass

    # Clean string timestamps (e.g. "01/06/2026 00:00:00")
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
    """Converts YYYY-MM-DD to DD/MM/YYYY for UI display."""
    if not iso_date_str:
        return "—"
    try:
        dt = datetime.strptime(iso_date_str, "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return iso_date_str
