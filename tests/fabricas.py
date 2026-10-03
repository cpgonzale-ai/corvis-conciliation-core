"""Constructores de filas de prueba con la forma que producen los motores de ingesta."""


def fila_ventas(total, iva=0.0, iva5=0.0, exenta=0.0, estado="Válida", tipo_doc="Factura", doc="052-001-9000001"):
    return {
        "doc": doc, "tipo_doc": tipo_doc, "sistema": "Aloha", "local": "L1", "estado": estado,
        "total_num": total, "iva_num": iva, "iva_5_num": iva5, "exentas_num": exenta,
        "gravadas": "0,00", "iva": "0,00", "gravadas_5": "0,00", "iva_5": "0,00",
        "exentas": "0,00", "total": "0,00",
    }


def fila_compras(clave, total, iva=0.0, iva5=0.0, exenta=0.0):
    return {
        "clave": clave, "doc": clave, "tipo_doc": "Factura", "proveedor": "Proveedor",
        "sistema": "Compras", "local": "L1",
        "total_num": total, "iva_num": iva, "iva_5_num": iva5, "exentas_num": exenta,
        "gravadas": "0,00", "iva": "0,00", "gravadas_5": "0,00", "iva_5": "0,00",
        "exentas": "0,00", "total": "0,00",
    }
