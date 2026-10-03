from typing import Any, Dict


def lado_diff(rec: Dict[str, Any] | None) -> Dict[str, str]:
    """Arma el desglose (gravada 10%/5%, IVA 10%/5%, exenta, total) de un lado de la
    comparación (libro propio o RG, en Ventas y en Compras) tal como lo pide el Paso 3 --
    vacío ('—') cuando ese lado no tiene el comprobante."""
    if rec is None:
        return {"gravada_10": "—", "iva_10": "—", "gravada_5": "—", "iva_5": "—", "exenta": "—", "total": "—"}
    return {
        "gravada_10": rec["gravadas"],
        "iva_10": rec["iva"],
        "gravada_5": rec.get("gravadas_5", "0,00"),
        "iva_5": rec.get("iva_5", "0,00"),
        "exenta": rec["exentas"],
        "total": rec["total"],
    }
