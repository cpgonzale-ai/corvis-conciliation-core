"""
Motor de Ingesta y Conciliación para el Libro de Compras (Minuta 5).

A diferencia del libro de ventas (app/core/engine.py):

  - La clave de comparación contra la RG es documento + RUC del proveedor (sin dígito
    verificador), concatenados sin separador — un mismo número de documento se repite entre
    proveedores distintos (cada uno tiene su propia numeración), a diferencia de ventas donde
    el documento por sí solo ya es único (siempre lo emite ACDG). Fórmula confirmada en el
    Excel de control real del cliente ("Control de RG Vs LC Mes de Mayo 2026.xlsx", hoja
    LCompras: =CONCATENATE(documento, ruc_sin_dv)).
  - No hay control de correlatividad ni de cortes/subtotales: no es responsabilidad del
    comprador que el proveedor salte numeración, y el export de compras no trae esas filas.
  - El "local" no se resuelve por punto de expedición del documento (que acá pertenece al
    proveedor, no a ACDG), sino por un código de sucursal — dónde se recibió la factura — que
    reutiliza la misma tabla `locales` de ventas, pero por su campo `codigo` en vez de
    `punto_expedicion`.
  - El campo "estado" (válido/anulado) que pide la Minuta 5 no viene en el export real del
    sistema (no se encontró esa columna en el archivo de referencia de mayo/2026) — todas las
    filas quedan como "Válida" hasta que el cliente confirme de dónde sale ese dato.

Por eso este motor vive aparte del de ventas en vez de forzar estas diferencias dentro de
`_process_dataframe` (pensado para reportes de POS con marcadores de sección, series con
prefijos no fiscales, clasificación de tasa de IVA por heurística, etc. — nada de eso aplica
acá, donde el export ya trae los montos separados por tasa).
"""

import os
import json
from typing import Any, Dict, List, Tuple

import pandas as pd

from app.core.desglose import lado_diff
from app.core.engine import (
    DOC_PATTERN,
    _matches_profile_signature,
    _monto_diff,
    _normalizar_tipo,
    _usecols_para_perfil,
    clean_numeric,
    fix_mojibake,
    fmt_gs as _fmt,
    normalize_invoice_number,
    tipo_doc_display,
)

# Códigos de nota de crédito del sistema origen ("NC", "NCE") + el texto largo tal como lo
# imprime la RG ("NOTA DE CRÉDITO"), incluida la variante electrónica del documento del
# cliente ("Tipo de Documentos RG -libros.xlsx") — cubre todos los formatos con la misma
# comparación.
_MARCADORES_NC = ("NC", "NCE", "NOTA DE CRÉDITO", "NOTA DE CREDITO", "NOTA DE CRÉDITO ELECTRONICA", "NOTA DE CREDITO ELECTRONICA")

# Códigos abreviados que algunos sistemas de origen usan en la columna "Tipo" en vez del
# nombre completo del SET (confirmado por el cliente sobre un archivo real de Formato
# Universal de Compras, ver "LC Formato universal 07-2026.xlsx"). Se resuelven al nombre
# oficial ANTES del filtro por tipo_comprobante_permitidos (ese filtro compara contra los
# nombres completos declarados en el perfil, ej. compras_universal.json) — sin esto, un
# archivo que usa códigos en vez de nombres largos queda con el 100% de sus filas
# descartadas en silencio, aunque el resto del comprobante sea válido. Cualquier código no
# listado acá sigue sin reconocerse y su fila se descarta como corresponde a un tipo no
# permitido.
_ABREVIATURAS_TIPO_COMPROBANTE = {
    "FA": "FACTURA",
    "FE": "FACTURA ELECTRONICA",
    "FV": "FACTURA VIRTUAL",
    "NC": "NOTA DE CRÉDITO",
    "NCE": "NOTA DE CRÉDITO ELECTRONICA",
    "ND": "NOTA DE DÉBITO",
    "NDE": "NOTA DE DÉBITO ELECTRONICA",
    "DE": "DESPACHO DE IMPORTACIÓN",
}
_ABREVIATURAS_TIPO_COMPROBANTE_NORM = {
    _normalizar_tipo(k): v for k, v in _ABREVIATURAS_TIPO_COMPROBANTE.items()
}


def _split_ruc_dv(raw: Any) -> Tuple[str, str]:
    """Separa '80016096-7' -> ('80016096', '7'). Si no trae guion (ej. la RG, que ya informa
    el RUC del proveedor sin el dígito verificador), devuelve el valor tal cual y DV vacío."""
    s = str(raw).strip() if raw is not None else ""
    if not s or s.lower() in ["nan", "none", "null"]:
        return "", ""
    if "." in s:
        s = s.split(".")[0]
    if "-" in s:
        ruc, dv = s.rsplit("-", 1)
        return ruc.strip(), dv.strip()
    return s, ""




def _texto_identificador(val: Any) -> str:
    """Timbrado/control son identificadores, no importes — pero si la columna de origen no
    tiene ningún valor no numérico, pandas la lee como float y un timbrado como 14353713
    llega acá como 14353713.0. Se saca el '.0' final en vez de arrastrarlo al libro limpio."""
    s = str(val).strip() if val is not None else ""
    if not s or s.lower() in ["nan", "none", "null"]:
        return ""
    if s.endswith(".0"):
        s = s[:-2]
    return s


class ComprasEngine:
    def __init__(self, profiles_dir: str):
        self.profiles_dir = profiles_dir
        self.profiles = self._load_profiles()

    def _load_profiles(self) -> Dict[str, Dict]:
        profiles = {}
        for fname in os.listdir(self.profiles_dir):
            if fname.endswith(".json"):
                path = os.path.join(self.profiles_dir, fname)
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    profiles[data["profile_id"]] = data
        return profiles

    def ingest_file(self, file_path: str, profile_id: str, local_name: str = "Local General") -> List[Dict[str, Any]]:
        profile = self.profiles.get(profile_id)
        if not profile:
            raise ValueError(f"Perfil de compras '{profile_id}' no encontrado.")

        ext = os.path.splitext(file_path)[1].lower()
        hdr_idx = profile.get("header_row_index", 0)
        sheet_index = profile.get("sheet_index", 0)
        # Ver _usecols_para_perfil en engine.py: limita la lectura a las columnas que el
        # perfil realmente mapea — la RG de compras tiene el mismo problema de rendimiento
        # que la RG90 de ventas con archivos .xls grandes (65.536×256 celdas por formato
        # aplicado a toda la hoja).
        usecols = _usecols_para_perfil(profile)

        if ext not in [".xls", ".xlsx"]:
            df = pd.read_csv(file_path, sep=";", header=hdr_idx, usecols=usecols)
        else:
            # engine="calamine" — mismo cambio y misma medición que en app/core/engine.py,
            # ver /auditoria/09-performance-backend-ingesta.md (93,7% del tiempo de ingesta
            # era openpyxl; calamine da el mismo resultado 8-9x más rápido).
            sheet_names = pd.ExcelFile(file_path, engine="calamine").sheet_names
            if not _matches_profile_signature(file_path, sheet_names, profile):
                raise ValueError(
                    f"El archivo no parece corresponder al formato de {profile['name']}. "
                    f"Verificá que adjuntaste el archivo correcto."
                )

            # El Formato Universal (Minuta 5) trae en el MISMO archivo una hoja de ventas y
            # otra de compras con columnas idénticas — si no aparece la hoja exacta que pide
            # el perfil, se corta acá en vez de caer en sheet_index por default: leer la
            # hoja equivocada sería indistinguible a simple vista (misma estructura).
            prefer_name = str(profile.get("prefer_sheet_name", "")).strip().lower()
            if prefer_name:
                encontrada = next((i for i, name in enumerate(sheet_names) if str(name).strip().lower() == prefer_name), None)
                if encontrada is None:
                    # Una sola hoja en TODO el archivo: no hay ninguna otra con la que
                    # confundirse (la razón de exigir el nombre exacto es evitar leer una
                    # hoja de Ventas como si fuera de Compras cuando ambas conviven en el
                    # mismo archivo con columnas idénticas). Si ya pasó
                    # _matches_profile_signature más arriba, es la hoja correcta aunque el
                    # cliente la haya dejado con el nombre por defecto de Excel ("Hoja1",
                    # "Sheet1", etc.) — mismo criterio que en engine.py (ingest_file de
                    # Ventas).
                    if len(sheet_names) == 1:
                        encontrada = 0
                    else:
                        raise ValueError(
                            f"El archivo no tiene una hoja llamada \"{profile.get('prefer_sheet_name')}\" — "
                            f"verificá que sea el archivo correcto para {profile['name']}."
                        )
                sheet_index = encontrada

            df = pd.read_excel(file_path, header=hdr_idx, sheet_name=sheet_index, usecols=usecols, engine="calamine")

        df.columns = [str(c).strip() for c in df.columns]
        return self._process_dataframe(df, profile, local_name)

    def _process_dataframe(self, df: pd.DataFrame, profile: Dict, local_name: str) -> List[Dict[str, Any]]:
        mappings = profile.get("column_mappings", [])
        extracted = {}
        for spec in mappings:
            target = spec["target_field"]
            # df.columns ya viene "strip"-eado (ver ingest_file) — se compara acá contra el
            # nombre del perfil también sin espacios al borde, para no depender de que el
            # perfil declare el espacio exacto que trae el archivo real (ver _usecols_para_perfil
            # en engine.py, mismo criterio del lado de la lectura).
            name = str(spec.get("source_name") or "").strip()
            extracted[target] = df[name] if name in df.columns else pd.Series([None] * len(df))
        records_df = pd.DataFrame(extracted)

        tipos_permitidos = profile.get("tipo_comprobante_permitidos")
        # Comparación sin tildes/mayúsculas (_normalizar_tipo): la lista se declara con la
        # ortografía oficial del SET (ej. "NOTA DE CRÉDITO"), pero el archivo real no
        # siempre trae la tilde.
        permitidos_norm = {_normalizar_tipo(t) for t in tipos_permitidos} if tipos_permitidos is not None else None

        n = len(records_df)

        def _col(name: str) -> list:
            return records_df[name].tolist() if name in records_df.columns else [None] * n

        # Mismo criterio que en app/core/engine.py (ver /auditoria/05-performance.md): se
        # extrae cada columna una sola vez como lista nativa en vez de reconstruir una
        # Series pandas por fila vía records_df.iterrows() — misma lógica exacta de abajo,
        # solo sin ese costo.
        tipo_comprobante_l = _col("tipo_comprobante")
        documento_l = _col("documento")
        ruc_proveedor_raw_l = _col("ruc_proveedor_raw")
        gravada_10_l = _col("gravada_10")
        iva_10_l = _col("iva_10")
        gravada_5_l = _col("gravada_5")
        iva_5_l = _col("iva_5")
        exenta_l = _col("exenta")
        total_l = _col("total")
        proveedor_l = _col("proveedor")
        codigo_sucursal_l = _col("codigo_sucursal")
        fecha_l = _col("fecha")
        condicion_l = _col("condicion")
        timbrado_l = _col("timbrado")
        control_l = _col("control")

        rows: List[Dict[str, Any]] = []
        for idx in range(n):
            tipo_comprobante = fix_mojibake(str(tipo_comprobante_l[idx] or "").strip())
            tipo_comprobante = _ABREVIATURAS_TIPO_COMPROBANTE_NORM.get(
                _normalizar_tipo(tipo_comprobante), tipo_comprobante
            )

            # Filtro explícito por tipo de comprobante, declarado por perfil: la RG de
            # compras admite todos los tipos de comprobante que el SET reconoce para el
            # libro de compras (Factura, Nota de Crédito, Nota de Débito, Autofactura,
            # Boleta de Venta, Ticket Máquina Registradora, etc.); el Formato Universal, en
            # cambio, solo trae Factura/Nota de Crédito en la práctica. Sin esta lista, un
            # comprobante con un número interno no fiscal (ej. "25030IC0400716") puede
            # terminar armando un doc EEE-PPP-NNNNNNN inventado que sí pasa la validación de
            # formato — se corta antes, por tipo, no solo por si el número calza.
            if permitidos_norm is not None and _normalizar_tipo(tipo_comprobante) not in permitidos_norm:
                continue

            doc = normalize_invoice_number(documento_l[idx])
            if not doc or not DOC_PATTERN.match(doc):
                continue

            ruc, dv = _split_ruc_dv(ruc_proveedor_raw_l[idx])
            if not ruc:
                continue
            clave = f"{doc}{ruc}"

            es_credito = tipo_comprobante.upper() in _MARCADORES_NC
            signo = -1 if es_credito else 1

            gravada_10 = signo * abs(clean_numeric(gravada_10_l[idx]))
            iva_10 = signo * abs(clean_numeric(iva_10_l[idx]))
            gravada_5 = signo * abs(clean_numeric(gravada_5_l[idx]))
            iva_5 = signo * abs(clean_numeric(iva_5_l[idx]))
            exenta = signo * abs(clean_numeric(exenta_l[idx]))
            total = signo * abs(clean_numeric(total_l[idx]))

            proveedor = fix_mojibake(str(proveedor_l[idx] or "").strip()) or "SIN NOMBRE"

            rows.append({
                "doc": doc,
                "clave": clave,
                "sistema": profile["name"],
                "local": local_name,
                "codigo_sucursal": str(codigo_sucursal_l[idx] or "").strip(),
                "fecha": str(fecha_l[idx] or "").strip(),
                "ruc_proveedor": ruc,
                "dv_proveedor": dv,
                "proveedor": proveedor,
                "tipo_comprobante": tipo_comprobante or ("NOTA DE CRÉDITO" if es_credito else "FACTURA"),
                # Se muestra el tipo de comprobante tal como vino del archivo (Autofactura,
                # Boleta de Venta, Nota de Débito, Ticket Máquina Registradora, etc.) en vez
                # de forzar todo a Factura/Nota de Crédito.
                "tipo_doc": tipo_doc_display(tipo_comprobante, es_credito),
                "condicion": str(condicion_l[idx] or "").strip(),
                "timbrado": _texto_identificador(timbrado_l[idx]),
                "control": _texto_identificador(control_l[idx]),
                "gravadas": _fmt(gravada_10),
                "iva": _fmt(iva_10),
                "gravadas_5": _fmt(gravada_5),
                "iva_5": _fmt(iva_5),
                "exentas": _fmt(exenta),
                "total": _fmt(total),
                "gravadas_num": gravada_10,
                "iva_num": iva_10,
                "gravadas_5_num": gravada_5,
                "iva_5_num": iva_5,
                "exentas_num": exenta,
                "total_num": total,
                # Placeholder hasta que se confirme de dónde sale el campo "estado" que pide
                # la Minuta 5 — no está en el export real del sistema (mayo/2026).
                "estado": "Válida",
            })
        return rows


def _comparar_par_compras(clave: str, pos_rec: Dict[str, Any] | None, rg_rec: Dict[str, Any] | None) -> Dict[str, Any]:
    """La lógica de negocio de la comparación, para UN comprobante a la vez -- extraída tal
    cual del loop que tenía reconcile_compras_with_rg (sin cambiar ninguna rama ni regla),
    para poder alimentarla tanto desde los dos mapas completos en memoria (esa función,
    abajo) como desde un cursor de un JOIN hecho en SQLite (reconcile_compras_with_rg_iter_pares,
    y app/api/compras.py) sin duplicar ni una condición -- mismo criterio que _comparar_par
    en engine.py (Ventas)."""
    if pos_rec and not rg_rec:
        return {
            "doc": pos_rec["doc"],
            "tipo_doc": pos_rec.get("tipo_doc", ""),
            "proveedor": pos_rec["proveedor"],
            "sistema": pos_rec["sistema"],
            "local": pos_rec["local"],
            "libro": lado_diff(pos_rec),
            "rg": lado_diff(None),
            "diferencia": "No llegó a la interfaz",
        }
    elif rg_rec and not pos_rec:
        return {
            "doc": rg_rec["doc"],
            "tipo_doc": rg_rec.get("tipo_doc", ""),
            "proveedor": rg_rec["proveedor"],
            "sistema": rg_rec.get("sistema", "RG"),
            "local": rg_rec.get("local", "Desconocido"),
            "libro": lado_diff(None),
            "rg": lado_diff(rg_rec),
            "diferencia": "No existe en el libro",
        }
    else:
        campo_diffs = {
            "total": _monto_diff(abs(pos_rec["total_num"]), abs(rg_rec["total_num"])),
            "iva_10": _monto_diff(abs(pos_rec.get("iva_num", 0.0)), abs(rg_rec.get("iva_num", 0.0))),
            "iva_5": _monto_diff(abs(pos_rec.get("iva_5_num", 0.0)), abs(rg_rec.get("iva_5_num", 0.0))),
            "exenta": _monto_diff(abs(pos_rec.get("exentas_num", 0.0)), abs(rg_rec.get("exentas_num", 0.0))),
        }
        campo_diffs = {k: v for k, v in campo_diffs.items() if v is not None}

        if campo_diffs:
            # Regla 1 / Regla 2 del Paso de Resultados (misma especificación funcional
            # que engine.py, Ventas): si el Total difiere, la observación es "Diferencia
            # de importe" sin importar si además hay diferencias en alguna tasa (Regla 1
            # tiene precedencia). Si el Total coincide pero alguna tasa (IVA 10%, IVA 5%
            # o Exenta) difiere, la observación es "Diferencias en tasas". Mutuamente
            # excluyentes.
            diferencia = "Diferencia de importe" if "total" in campo_diffs else "Diferencias en tasas"
            return {
                "doc": pos_rec["doc"],
                "tipo_doc": pos_rec.get("tipo_doc", ""),
                "proveedor": pos_rec["proveedor"],
                "sistema": pos_rec["sistema"],
                "local": pos_rec["local"],
                "libro": lado_diff(pos_rec),
                "rg": lado_diff(rg_rec),
                "diferencia": diferencia,
                "diferencias_detalle": campo_diffs,
            }
        else:
            # Mismo criterio que reconcile_with_rg90: el comprobante coincide (mismo doc
            # + RUC del proveedor en ambos lados, sin diferencia de monto) — antes no se
            # guardaba nada acá y la tarjeta "Coinciden" no tenía filas reales detrás.
            return {
                "doc": pos_rec["doc"],
                "tipo_doc": pos_rec.get("tipo_doc", ""),
                "proveedor": pos_rec["proveedor"],
                "sistema": pos_rec["sistema"],
                "local": pos_rec["local"],
                "libro": lado_diff(pos_rec),
                "rg": lado_diff(rg_rec),
                "diferencia": "Coincide",
            }


def reconcile_compras_with_rg(libro_rows: List[Dict[str, Any]], rg_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Full outer join por clave (documento + RUC del proveedor sin DV) — mismo control por
    campo (Total/IVA 10%/IVA 5%/Exenta) que `reconcile_with_rg90`, pero con la clave
    compuesta que exige compras (ver Minuta 5 y la fórmula real del Excel de control del
    cliente: SUMIF por clave concatenada). Wrapper sobre _comparar_par_compras para quien
    tenga los dos mapas completos en memoria -- /api/compras/reconcile usa en cambio
    reconcile_compras_with_rg_iter_pares, sobre un cursor de un JOIN en SQLite."""
    libro_map = {r["clave"]: r for r in libro_rows}
    rg_map = {r["clave"]: r for r in rg_rows}

    all_claves = set(libro_map.keys()).union(set(rg_map.keys()))
    return [_comparar_par_compras(clave, libro_map.get(clave), rg_map.get(clave)) for clave in sorted(all_claves)]


def reconcile_compras_with_rg_iter_pares(pares):
    """Misma lógica de comparación que reconcile_compras_with_rg, para pares (clave,
    pos_rec_o_None, rg_rec_o_None) ya emparejados de antemano -- pensado para recibir
    directo el cursor de un JOIN hecho en SQLite (ver app/api/compras.py), sin necesitar
    los dos mapas completos en memoria de Python al mismo tiempo. Mismo criterio que
    reconcile_with_rg90_iter_pares en engine.py (Ventas)."""
    for clave, pos_rec, rg_rec in pares:
        yield _comparar_par_compras(clave, pos_rec, rg_rec)


