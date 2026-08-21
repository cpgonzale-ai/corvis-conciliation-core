"""
High-Performance Conciliation Engine for SISCOM RG90.
Handles ingestion, profile matching, sequence gap detection, and RG90 SQL-based reconciliation.
"""

import os
import re
import json
import pandas as pd
import numpy as np
from typing import List, Dict, Any, Tuple, Optional
from app.core.date_parser import parse_date, format_display_date

# Tolerancia para comparaciones de montos (evita falsos positivos por decimales periódicos
# al dividir gravada = total / 1.1, tal como se ve en los archivos reales del cliente).
MONTO_TOLERANCE = 0.6

DOC_PATTERN = re.compile(r"^(NC)?\d{3}-\d{3}-\d{7}$")
SERIE_PATTERN = re.compile(r"^(NC)?\d{3}-\d{3}$")


def normalize_invoice_number(raw_num: Any) -> Optional[str]:
    """Normalizes document numbers into standard EEE-PPP-NNNNNNN format."""
    if raw_num is None or pd.isna(raw_num):
        return None
    s = str(raw_num).strip()
    if not s or s.lower() in ["nan", "null", "none"]:
        return None

    if "." in s:
        s = s.split(".")[0]

    clean = re.sub(r"[^\dA-Z-]", "", s.upper())
    if DOC_PATTERN.match(clean):
        return clean

    digits = re.sub(r"\D", "", s)
    if len(digits) == 13:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    elif len(digits) > 7:
        n = digits[-7:]
        rest = digits[:-7]
        p = rest[-3:] if len(rest) >= 3 else rest.zfill(3)
        e = rest[:-3] if len(rest) > 3 else "001"
        return f"{e.zfill(3)}-{p.zfill(3)}-{n.zfill(7)}"

    return s


def clean_numeric(val: Any) -> float:
    """Parses and cleans float amounts."""
    if val is None or pd.isna(val):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def fix_mojibake(s: str) -> str:
    """Repara texto exportado como UTF-8 y releído como Windows-1252 (frecuente en reportes
    de Hiopos, generados en Windows: 'BelÃ©n' -> 'Belén', 'PATIÃ‘O' -> 'PATIÑO')."""
    if not s:
        return s
    try:
        repaired = s.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return repaired if repaired != s else s


def classify_tax_rate(gravada_bruta: float, iva_bruta: float) -> Tuple[float, float, float, float, float]:
    """Clasifica un comprobante en 10%, 5% o exento probando `gravada * tasa - iva ≈ 0`
    (Minutas de relevamiento 3 y 4). Devuelve (gravada_10, iva_10, gravada_5, iva_5, exenta).
    """
    if abs(iva_bruta) <= MONTO_TOLERANCE:
        return 0.0, 0.0, 0.0, 0.0, gravada_bruta

    if abs(gravada_bruta * 0.10 - iva_bruta) <= MONTO_TOLERANCE:
        return gravada_bruta, iva_bruta, 0.0, 0.0, 0.0

    if abs(gravada_bruta * 0.05 - iva_bruta) <= MONTO_TOLERANCE:
        return 0.0, 0.0, gravada_bruta, iva_bruta, 0.0

    # Ninguna tasa conocida calzó: se conserva como gravada al 10% (caso más común) para no
    # perder el registro, pero queda disponible para revisión manual vía el total.
    return gravada_bruta, iva_bruta, 0.0, 0.0, 0.0


def _extract_corte(raw_row: List[Any], seccion: str) -> Optional[Dict[str, Any]]:
    """Detecta y extrae una fila de corte/subtotal ("Serie: 001 Totales", "Resolución: 052
    Totales", "1 Factura Totales", "N NOTA DE CREDITO Totales") tal como las imprime Aloha al
    cierre de cada bloque. Antes se descartaban sin guardar nada; ahora se conservan para
    poder cruzarlas contra lo que el motor calculó y para armar el resumen del libro en
    limpio (Minuta 3: "un resumen ... que determina la venta neta")."""
    if not any(isinstance(v, str) and v.strip().lower() == "totales" for v in raw_row):
        return None

    etiqueta = " ".join(
        str(v).strip() for v in raw_row[:2]
        if isinstance(v, str) and v.strip() and v.strip().lower() != "totales"
    ) or "Corte"

    numeric_vals = [float(v) for v in raw_row if isinstance(v, (int, float)) and not pd.isna(v)]
    if len(numeric_vals) < 3:
        return None
    gravada, iva, total = numeric_vals[-3], numeric_vals[-2], numeric_vals[-1]

    return {
        "etiqueta": etiqueta,
        "seccion": "Nota de Crédito" if seccion == "credito" else "Factura",
        "gravada": gravada,
        "iva": iva,
        "total": total,
    }


class IngestionEngine:
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

    def _read_source(self, file_path: str, ext: str, hdr_idx: int, sheet_index: int) -> pd.DataFrame:
        if ext not in [".xls", ".xlsx"]:
            return pd.read_csv(file_path, sep=";", header=hdr_idx)
        return pd.read_excel(file_path, header=hdr_idx, sheet_name=sheet_index)

    def ingest_file(self, file_path: str, profile_id: str, local_name: str = "Local General") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        profile = self.profiles.get(profile_id)
        if not profile:
            raise ValueError(f"Perfil '{profile_id}' no encontrado.")

        ext = os.path.splitext(file_path)[1].lower()
        hdr_idx = profile.get("header_row_index", 0)
        # Los sistemas de origen no exportan sus hojas con un nombre ni una cantidad
        # consistente: a veces la hoja preferida (ej. la "hoja 2", tal como se descarga sin
        # edición manual) no existe o tiene una estructura distinta (borradores, resúmenes).
        # Por eso se prueba primero la hoja configurada y, si no existe o no produce ningún
        # comprobante válido, se recorren las demás hojas del archivo en orden.
        preferred_sheet = profile.get("sheet_index", 0)

        if ext not in [".xls", ".xlsx"]:
            df = self._read_source(file_path, ext, hdr_idx, preferred_sheet)
            return self._process_dataframe(df, profile, local_name)

        sheet_names = pd.ExcelFile(file_path).sheet_names
        candidatos = [preferred_sheet] + [i for i in range(len(sheet_names)) if i != preferred_sheet]

        rows: List[Dict[str, Any]] = []
        cortes: List[Dict[str, Any]] = []
        for idx in candidatos:
            if idx >= len(sheet_names):
                continue
            df = pd.read_excel(file_path, header=hdr_idx, sheet_name=idx)
            rows, cortes = self._process_dataframe(df, profile, local_name)
            if rows:
                break
        return rows, cortes

    def _process_dataframe(self, df: pd.DataFrame, profile: Dict, local_name: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        mappings = profile.get("column_mappings", [])
        extracted_cols = {}

        for col_spec in mappings:
            target = col_spec["target_field"]
            if "source_col" in col_spec:
                c_idx = col_spec["source_col"]
                extracted_cols[target] = df.iloc[:, c_idx] if c_idx < len(df.columns) else pd.Series([None] * len(df))
            else:
                c_name = col_spec.get("source_name")
                extracted_cols[target] = df[c_name] if c_name in df.columns else pd.Series([None] * len(df))

        records_df = pd.DataFrame(extracted_cols)
        usa_clasificacion_tasa = "gravada_bruta" in records_df.columns
        processed_rows: List[Dict[str, Any]] = []
        cortes: List[Dict[str, Any]] = []

        # Aloha no trae un campo "serie" por fila que identifique una nota de crédito (a
        # diferencia de Hiopos, que sí prefija "NC..." en su columna serie). En cambio, el
        # reporte separa las facturas de las notas de crédito con una fila marcadora ("1
        # Factura" / "N NOTA DE CREDITO") y después imprime, al cierre de cada bloque,
        # subtotales por serie y por resolución ("Serie: 001 Totales", "Resolución: 052
        # Totales") que hoy se descartaban sin guardar nada. Acá se sigue ese estado fila a
        # fila: mientras no aparezca el marcador de crédito, las filas son facturas; a partir
        # de él (y hasta el próximo marcador de factura, si el archivo combina varios puntos
        # de expedición) son notas de crédito.
        #
        # El "documento" final NO lleva prefijo "NC" (así lo pide el libro limpio de
        # referencia del cliente: la columna "Factura" trae la numeración cruda para ambos
        # tipos); la clasificación factura/nota de crédito vive aparte, en "tipo_doc", y el
        # control de correlatividad (detect_sequence_gaps) agrupa por local+serie+tipo_doc
        # para no mezclar la numeración de facturas con la de notas de crédito.
        marker_col = profile.get("section_marker_col")
        credit_marker = str(profile.get("section_credit_marker", "")).strip().upper()
        factura_marker = str(profile.get("section_factura_marker", "FACTURA")).strip().upper()
        current_section = "factura"

        for idx, r in records_df.iterrows():
            if marker_col is not None and marker_col < len(df.columns):
                raw_marker = df.iat[idx, marker_col]
                if pd.notna(raw_marker):
                    marker_txt = str(raw_marker).strip().upper()
                    if credit_marker and marker_txt == credit_marker:
                        current_section = "credito"
                    elif marker_txt == factura_marker:
                        current_section = "factura"

            corte = _extract_corte(df.iloc[idx].tolist(), current_section)
            if corte:
                cortes.append(corte)

            # Filtro explícito por "Tipo Documento" (Hiopos): solo se procesan factura de
            # venta, factura de venta simplificada, abono factura de venta y abono factura de
            # venta simplificada. El resto (pedido/albarán/factura de compra, merma,
            # invitación, recuento, filas vacías) se descarta acá directamente, sin depender
            # únicamente de que su serie no calce con el patrón EEE-PPP.
            tipos_permitidos = profile.get("tipo_documento_permitidos")
            if tipos_permitidos is not None:
                tipo_doc_raw = fix_mojibake(str(r.get("tipo_documento") or "").strip())
                permitidos_lower = {t.lower() for t in tipos_permitidos}
                if tipo_doc_raw.lower() not in permitidos_lower:
                    continue

            serie = str(r.get("serie") or "").strip()
            serie_norm = fix_mojibake(serie)
            if serie_norm.lower() in ["anulación", "anulacion"]:
                continue
            # Series internas no fiscales (ej. mermas, invitaciones/cortesías en Hiopos) no
            # tienen el formato EEE-PPP de un punto de expedición real y deben descartarse.
            if serie_norm and not SERIE_PATTERN.match(serie_norm.upper()):
                continue

            # El "documento" en el libro limpio final (ver hoja "LIBRO VENTAS GLOBAL-Fact-NC"
            # del archivo de referencia del cliente) es la numeración cruda, SIN prefijo "NC":
            # facturas y notas de crédito conviven en la misma columna "Factura" y se
            # distinguen únicamente por la columna "Tipo Doc.". El prefijo "NC" solo se usaba
            # internamente antes; ahora se guarda es_credito y se arma el doc limpio.
            es_credito = current_section == "credito"
            serie_for_doc = serie_norm
            if serie_norm.upper().startswith("NC"):
                es_credito = True
                serie_for_doc = serie_norm[2:]

            raw_doc = r.get("numero_comprobante")
            if serie_for_doc and not str(raw_doc).startswith(serie_for_doc):
                try:
                    seq_int = int(float(str(raw_doc)))
                    doc = f"{serie_for_doc}-{seq_int:07d}"
                except ValueError:
                    doc = normalize_invoice_number(f"{serie_for_doc}-{raw_doc}")
            else:
                doc = normalize_invoice_number(raw_doc)

            if not doc:
                continue

            if not DOC_PATTERN.match(doc):
                continue

            fecha_iso = parse_date(r.get("fecha"))
            fecha_disp = format_display_date(fecha_iso)

            # Convención del libro propio (no de la comparación contra RG90, que sigue en
            # valor absoluto en reconcile_with_rg90): una nota de crédito resta de la venta,
            # así que sus montos quedan en negativo — es lo que permite que "Total Neto" dé la
            # venta neta real, tal como lo imprime el cliente en su Excel de referencia.
            total = -abs(clean_numeric(r.get("total"))) if es_credito else abs(clean_numeric(r.get("total")))

            if usa_clasificacion_tasa:
                gravada_bruta = -abs(clean_numeric(r.get("gravada_bruta"))) if es_credito else abs(clean_numeric(r.get("gravada_bruta")))
                iva_bruta = -abs(clean_numeric(r.get("iva_bruta"))) if es_credito else abs(clean_numeric(r.get("iva_bruta")))
                gravada, iva, gravada_5, iva_5, exenta = classify_tax_rate(gravada_bruta, iva_bruta)
            else:
                signo = -1 if es_credito else 1
                gravada = signo * abs(clean_numeric(r.get("gravada_10")))
                iva = signo * abs(clean_numeric(r.get("iva_10")))
                gravada_5 = signo * abs(clean_numeric(r.get("gravada_5")))
                iva_5 = signo * abs(clean_numeric(r.get("iva_5")))
                exenta = signo * abs(clean_numeric(r.get("exenta")))

                if total != 0 and gravada == 0 and gravada_5 == 0 and exenta == 0:
                    gravada = signo * round(abs(total) / 1.1, 0)
                    iva = total - gravada

            ruc = str(r.get("ruc") or "").strip()
            if not ruc or ruc.upper() in ["NAN", "NONE", "NULL", "X"]:
                ruc = "X"

            nombre = fix_mojibake(str(r.get("nombre_cliente") or "").strip())
            if not nombre or nombre.upper() in ["NAN", "NONE", "NULL", "SIN NOMBRE"]:
                nombre = "SIN NOMBRE"

            estado = str(r.get("estado") or "Válida").strip()
            if estado.upper() == "E":
                estado = "Válida"
            elif estado.upper() == "A":
                estado = "Anulada"
            elif total == 0 and estado.lower() != "anulada":
                estado = "Anulada"

            def _fmt(n: float) -> str:
                return f"{int(n):,}".replace(",", ".")

            processed_rows.append({
                "doc": doc,
                "sistema": profile["name"],
                "local": local_name,
                "fecha": fecha_disp,
                "fecha_iso": fecha_iso,
                "ruc": ruc,
                "nombre": nombre,
                "gravadas": _fmt(gravada),
                "iva": _fmt(iva),
                "gravadas_5": _fmt(gravada_5),
                "iva_5": _fmt(iva_5),
                "exentas": _fmt(exenta),
                "total": _fmt(total),
                "gravadas_num": gravada,
                "iva_num": iva,
                "gravadas_5_num": gravada_5,
                "iva_5_num": iva_5,
                "exentas_num": exenta,
                "total_num": total,
                "estado": estado,
                "tipo_doc": "Nota de Crédito" if es_credito else "Factura",
            })

        return processed_rows, cortes


def detect_sequence_gaps(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Detects missing document numbers per local / establecimiento / tipo de documento.

    Facturas y notas de crédito de un mismo punto de expedición llevan cada una su propia
    numeración correlativa independiente (confirmado sobre archivos reales: en un mismo
    punto de expedición, las facturas arrancan en un rango — ej. 0031187 — y las notas de
    crédito en otro completamente distinto — ej. 0000019 —, sin relación entre sí). Por eso
    el control de correlatividad agrupa por local + serie + "tipo_doc": alcanza con comparar
    ambos campos (Tipo y Documento) para saber si hay un salto real dentro de cada secuencia,
    sin mezclar la numeración de facturas con la de notas de crédito.
    """
    grouped = {}
    for r in rows:
        doc = r["doc"]
        m = re.match(r"^(\d{3}-\d{3})-(\d{7})$", doc)
        if not m:
            continue
        tipo = r.get("tipo_doc", "Factura")
        series = f"{r['local']} ({m.group(1)}) — {tipo}"
        seq = int(m.group(2))
        if series not in grouped:
            grouped[series] = []
        grouped[series].append((seq, doc, r))

    gaps = []
    for series, items in grouped.items():
        items.sort(key=lambda x: x[0])
        for i in range(len(items) - 1):
            curr_seq, curr_doc, curr_r = items[i]
            next_seq, next_doc, _ = items[i + 1]
            diff = next_seq - curr_seq
            if diff > 1:
                missing_start = f"{curr_doc[:8]}{(curr_seq + 1):07d}"
                missing_end = f"{curr_doc[:8]}{(next_seq - 1):07d}"
                gap_label = missing_start if diff == 2 else f"{missing_start} → {missing_end}"
                gaps.append({
                    "local": curr_r["local"],
                    "sistema": curr_r["sistema"],
                    "tipo_doc": curr_r.get("tipo_doc", "Factura"),
                    "ultimo": next_doc,
                    "salto": gap_label,
                    "cantidad": diff - 1,
                    "estado": "Pendiente de revisión"
                })

    return gaps


def _monto_diff(a: float, b: float) -> Optional[float]:
    diff = round(a - b, 2)
    return None if abs(diff) <= MONTO_TOLERANCE else diff


def reconcile_with_rg90(libro_rows: List[Dict[str, Any]], rg90_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Runs FULL OUTER JOIN reconciliation between POS Sales Book and RG90 SET Report.

    El control por campo (Total, IVA 10%, IVA 5%, Exenta) replica la planilla real del
    cliente ("Check RG Total / IVA 10% / IVA 5% / Exentas"), en vez de comparar solo el
    total — la RG90 no permite validar por "gravada" porque esa columna viene mal
    calculada (incluye el IVA, según Minuta 4).
    """
    libro_map = {r["doc"]: r for r in libro_rows}
    rg90_map = {r["doc"]: r for r in rg90_rows}

    all_docs = set(libro_map.keys()).union(set(rg90_map.keys()))
    diffs = []

    for doc in sorted(all_docs):
        pos_rec = libro_map.get(doc)
        rg_rec = rg90_map.get(doc)

        if pos_rec and not rg_rec:
            if pos_rec["estado"].lower() == "anulada":
                # Un comprobante anulado nunca llega a informarse a la RG90: es el
                # comportamiento esperado, no una diferencia a revisar (dato confirmado con
                # archivos reales: 81 de 657 anuladas en Aloha Sheraton, ninguna en RG90).
                diffs.append({
                    "doc": doc,
                    "sistema": pos_rec["sistema"],
                    "local": pos_rec["local"],
                    "libro": pos_rec["total"],
                    "rg90": "—",
                    "diferencia": "Anulada"
                })
                continue
            diffs.append({
                "doc": doc,
                "sistema": pos_rec["sistema"],
                "local": pos_rec["local"],
                "libro": pos_rec["total"],
                "rg90": "—",
                "diferencia": "No llegó a la interfaz"
            })
        elif rg_rec and not pos_rec:
            diffs.append({
                "doc": doc,
                "sistema": rg_rec.get("sistema", "RG90"),
                "local": rg_rec.get("local", "Desconocido"),
                "libro": "—",
                "rg90": rg_rec["total"],
                "diferencia": "No en libro propio"
            })
        elif pos_rec and rg_rec:
            # La RG90 siempre informa en valor absoluto (confirmado: tanto el reporte de
            # venta como el de NC del SET traen montos positivos), mientras que el libro
            # propio ahora guarda las notas de crédito en negativo (para que "Total Neto" dé
            # la venta neta real). El control contra la RG90 compara magnitudes, no signo.
            campo_diffs = {
                "total": _monto_diff(abs(pos_rec["total_num"]), rg_rec["total_num"]),
                "iva_10": _monto_diff(abs(pos_rec.get("iva_num", 0.0)), rg_rec.get("iva_num", 0.0)),
                "iva_5": _monto_diff(abs(pos_rec.get("iva_5_num", 0.0)), rg_rec.get("iva_5_num", 0.0)),
                "exenta": _monto_diff(abs(pos_rec.get("exentas_num", 0.0)), rg_rec.get("exentas_num", 0.0)),
            }
            campo_diffs = {k: v for k, v in campo_diffs.items() if v is not None}

            if campo_diffs:
                diffs.append({
                    "doc": doc,
                    "sistema": pos_rec["sistema"],
                    "local": pos_rec["local"],
                    "libro": pos_rec["total"],
                    "rg90": rg_rec["total"],
                    "diferencia": "Diferencia de monto",
                    "diferencias_detalle": campo_diffs,
                })
            elif rg_rec.get("estado", "").lower() == "rechazada":
                diffs.append({
                    "doc": doc,
                    "sistema": pos_rec["sistema"],
                    "local": pos_rec["local"],
                    "libro": pos_rec["total"],
                    "rg90": "Rechazada",
                    "diferencia": "Rechazada"
                })
            elif pos_rec["estado"].lower() == "anulada" or rg_rec.get("estado", "").lower() == "anulada":
                diffs.append({
                    "doc": doc,
                    "sistema": pos_rec["sistema"],
                    "local": pos_rec["local"],
                    "libro": pos_rec["total"],
                    "rg90": "Anulada",
                    "diferencia": "Anulada"
                })

    return diffs
