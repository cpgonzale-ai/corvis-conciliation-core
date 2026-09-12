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
            sheet_names = pd.ExcelFile(file_path).sheet_names
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
                    raise ValueError(
                        f"El archivo no tiene una hoja llamada \"{profile.get('prefer_sheet_name')}\" — "
                        f"verificá que sea el archivo correcto para {profile['name']}."
                    )
                sheet_index = encontrada

            df = pd.read_excel(file_path, header=hdr_idx, sheet_name=sheet_index, usecols=usecols)

        df.columns = [str(c).strip() for c in df.columns]
        return self._process_dataframe(df, profile, local_name)

    def _process_dataframe(self, df: pd.DataFrame, profile: Dict, local_name: str) -> List[Dict[str, Any]]:
        mappings = profile.get("column_mappings", [])
        extracted = {}
        for spec in mappings:
            target = spec["target_field"]
            name = spec.get("source_name")
            extracted[target] = df[name] if name in df.columns else pd.Series([None] * len(df))
        records_df = pd.DataFrame(extracted)

        tipos_permitidos = profile.get("tipo_comprobante_permitidos")
        # Comparación sin tildes/mayúsculas (_normalizar_tipo): la lista se declara con la
        # ortografía oficial del SET (ej. "NOTA DE CRÉDITO"), pero el archivo real no
        # siempre trae la tilde.
        permitidos_norm = {_normalizar_tipo(t) for t in tipos_permitidos} if tipos_permitidos is not None else None

        rows: List[Dict[str, Any]] = []
        for _, r in records_df.iterrows():
            tipo_comprobante = fix_mojibake(str(r.get("tipo_comprobante") or "").strip())

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

            doc = normalize_invoice_number(r.get("documento"))
            if not doc or not DOC_PATTERN.match(doc):
                continue

            ruc, dv = _split_ruc_dv(r.get("ruc_proveedor_raw"))
            if not ruc:
                continue
            clave = f"{doc}{ruc}"

            es_credito = tipo_comprobante.upper() in _MARCADORES_NC
            signo = -1 if es_credito else 1

            gravada_10 = signo * abs(clean_numeric(r.get("gravada_10")))
            iva_10 = signo * abs(clean_numeric(r.get("iva_10")))
            gravada_5 = signo * abs(clean_numeric(r.get("gravada_5")))
            iva_5 = signo * abs(clean_numeric(r.get("iva_5")))
            exenta = signo * abs(clean_numeric(r.get("exenta")))
            total = signo * abs(clean_numeric(r.get("total")))

            proveedor = fix_mojibake(str(r.get("proveedor") or "").strip()) or "SIN NOMBRE"

            rows.append({
                "doc": doc,
                "clave": clave,
                "sistema": profile["name"],
                "local": local_name,
                "codigo_sucursal": str(r.get("codigo_sucursal") or "").strip(),
                "fecha": str(r.get("fecha") or "").strip(),
                "ruc_proveedor": ruc,
                "dv_proveedor": dv,
                "proveedor": proveedor,
                "tipo_comprobante": tipo_comprobante or ("NOTA DE CRÉDITO" if es_credito else "FACTURA"),
                # Se muestra el tipo de comprobante tal como vino del archivo (Autofactura,
                # Boleta de Venta, Nota de Débito, Ticket Máquina Registradora, etc.) en vez
                # de forzar todo a Factura/Nota de Crédito.
                "tipo_doc": tipo_doc_display(tipo_comprobante, es_credito),
                "condicion": str(r.get("condicion") or "").strip(),
                "timbrado": _texto_identificador(r.get("timbrado")),
                "control": _texto_identificador(r.get("control")),
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


def reconcile_compras_with_rg(libro_rows: List[Dict[str, Any]], rg_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Full outer join por clave (documento + RUC del proveedor sin DV) — mismo control por
    campo (Total/IVA 10%/IVA 5%/Exenta) que `reconcile_with_rg90`, pero con la clave
    compuesta que exige compras (ver Minuta 5 y la fórmula real del Excel de control del
    cliente: SUMIF por clave concatenada)."""
    libro_map = {r["clave"]: r for r in libro_rows}
    rg_map = {r["clave"]: r for r in rg_rows}

    all_claves = set(libro_map.keys()).union(set(rg_map.keys()))
    diffs: List[Dict[str, Any]] = []

    for clave in sorted(all_claves):
        pos_rec = libro_map.get(clave)
        rg_rec = rg_map.get(clave)

        if pos_rec and not rg_rec:
            diffs.append({
                "doc": pos_rec["doc"],
                "proveedor": pos_rec["proveedor"],
                "sistema": pos_rec["sistema"],
                "local": pos_rec["local"],
                "libro": _lado_diff(pos_rec),
                "rg": _lado_diff(None),
                "diferencia": "No llegó a la interfaz",
            })
        elif rg_rec and not pos_rec:
            diffs.append({
                "doc": rg_rec["doc"],
                "proveedor": rg_rec["proveedor"],
                "sistema": rg_rec.get("sistema", "RG"),
                "local": rg_rec.get("local", "Desconocido"),
                "libro": _lado_diff(None),
                "rg": _lado_diff(rg_rec),
                "diferencia": "No en libro propio",
            })
        elif pos_rec and rg_rec:
            campo_diffs = {
                "total": _monto_diff(abs(pos_rec["total_num"]), abs(rg_rec["total_num"])),
                "iva_10": _monto_diff(abs(pos_rec.get("iva_num", 0.0)), abs(rg_rec.get("iva_num", 0.0))),
                "iva_5": _monto_diff(abs(pos_rec.get("iva_5_num", 0.0)), abs(rg_rec.get("iva_5_num", 0.0))),
                "exenta": _monto_diff(abs(pos_rec.get("exentas_num", 0.0)), abs(rg_rec.get("exentas_num", 0.0))),
            }
            campo_diffs = {k: v for k, v in campo_diffs.items() if v is not None}

            if campo_diffs:
                diffs.append({
                    "doc": pos_rec["doc"],
                    "proveedor": pos_rec["proveedor"],
                    "sistema": pos_rec["sistema"],
                    "local": pos_rec["local"],
                    "libro": _lado_diff(pos_rec),
                    "rg": _lado_diff(rg_rec),
                    "diferencia": "Diferencia de monto",
                    "diferencias_detalle": campo_diffs,
                })
            else:
                # Mismo criterio que reconcile_with_rg90: el comprobante coincide (mismo doc
                # + RUC del proveedor en ambos lados, sin diferencia de monto) — antes no se
                # guardaba nada acá y la tarjeta "Coinciden" no tenía filas reales detrás.
                diffs.append({
                    "doc": pos_rec["doc"],
                    "proveedor": pos_rec["proveedor"],
                    "sistema": pos_rec["sistema"],
                    "local": pos_rec["local"],
                    "libro": _lado_diff(pos_rec),
                    "rg": _lado_diff(rg_rec),
                    "diferencia": "Coincide",
                })

    return diffs


def _lado_diff(rec: Dict[str, Any] | None) -> Dict[str, str]:
    """Arma el desglose (gravada 10%/5%, IVA 10%/5%, exenta, total) de un lado de la
    comparación (libro propio o RG) tal como lo pide el Paso 3 — vacío ('—') cuando ese
    lado no tiene el comprobante."""
    if rec is None:
        return {"gravada_10": "—", "iva_10": "—", "gravada_5": "—", "iva_5": "—", "exenta": "—", "total": "—"}
    return {
        "gravada_10": rec["gravadas"],
        "iva_10": rec["iva"],
        "gravada_5": rec["gravadas_5"],
        "iva_5": rec["iva_5"],
        "exenta": rec["exentas"],
        "total": rec["total"],
    }
