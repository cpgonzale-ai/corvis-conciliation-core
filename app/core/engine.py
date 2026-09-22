"""
High-Performance Conciliation Engine for SISCOM RG90.
Handles ingestion, profile matching, sequence gap detection, and RG90 SQL-based reconciliation.
"""

import os
import re
import json
import unicodedata
from datetime import datetime, date
import numpy as np
import openpyxl
import pandas as pd
from typing import List, Dict, Any, Tuple, Optional
from app.core.date_parser import parse_date, format_display_date, DATE_FORMATS

# Dos tolerancias separadas — antes era una sola (MONTO_TOLERANCE) usada para dos cosas
# distintas, lo que hacía que no se pudiera ajustar una sin afectar la otra:
#
# - Clasificación de tasa de IVA al ingerir un archivo (classify_tax_rate): evita falsos
#   negativos por decimales periódicos al dividir gravada = total / 1.1 o / 1.05 (tal como
#   se ve en los archivos reales del cliente) — si esto se pone en 0, un comprobante real
#   con una fracción de guaraní de diferencia entre gravada*tasa e iva deja de matchear
#   ninguna tasa conocida y cae en la clasificación por defecto (10%), un error de datos
#   silencioso. Valor definido por la regla de negocio del cliente ("cercano a 0,
#   aproximadamente 0,5 por redondeo").
MONTO_TOLERANCE_CLASIFICACION = 0.5

# - Comparación de montos en el resultado (Paso 3 de Compras / Paso 4 de Ventas, mismo valor
#   para los dos): decide si una diferencia entre el libro propio y la RG es lo bastante
#   grande como para mostrarse. Definida por el cliente en 100 Gs — una diferencia de hasta
#   100 Gs no se marca.
MONTO_TOLERANCE_DIFERENCIA = 100.0

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


def fmt_gs(n: float) -> str:
    """Formatea un importe al revés de clean_numeric: de float a texto es-PY (punto de
    miles, coma decimal), sin redondear a entero — se muestra tal como viene calculado del
    Excel, con decimales (ej. al clasificar la tasa de IVA, gravada = total / 1.1 no da un
    número redondo). Un solo lugar para esta conversión — antes vivía duplicada como función
    anidada acá (redefinida en cada fila procesada) y, aparte, como función propia en
    compras_engine.py."""
    s = f"{n:,.2f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def _normalizar_tipo(s: str) -> str:
    """Normaliza un 'Tipo de Comprobante' para compararlo sin importar tildes ni mayúsculas.
    La terminología oficial del SET usa tildes (ej. 'NOTA DE CRÉDITO', 'BOLETO O TICKET DE
    TRANSPORTE AÉREO'), pero los reportes reales exportados no siempre las conservan — así se
    puede declarar el perfil una sola vez, con la ortografía oficial, y que matchee ambas
    formas sin tener que listar cada variante a mano."""
    s = unicodedata.normalize("NFKD", s.strip().upper())
    return "".join(c for c in s if not unicodedata.combining(c))


# Conectores que quedan en minúscula al armar un título en español (salvo que sean la
# primera palabra) — usado para mostrar el "Tipo de Comprobante" tal como vino del archivo,
# en vez de forzarlo a "Factura"/"Nota de Crédito" (ver tipo_doc en _process_dataframe).
_CONECTORES_MINUSCULA_ES = {"de", "del", "la", "y", "o", "a", "en", "para"}


def _titulo_es(s: str) -> str:
    palabras = s.strip().lower().split()
    return " ".join(
        w if (i > 0 and w in _CONECTORES_MINUSCULA_ES) else (w[:1].upper() + w[1:])
        for i, w in enumerate(palabras)
    )



# Tipos de Comprobante oficiales del SET, según el documento del cliente "Tipo de
# Documentos RG -libros.xlsx": los válidos para Ventas (Factura, Nota de Crédito, Nota de
# Débito — únicos 3 marcados en verde ahí) más los que solo aplican a Compras (el resto de
# la hoja, incluidos los clasificados ahí como Ingresos/Egresos, que la columna D marca
# igual como válidos para Compras). Se muestran con el texto tal como vino del archivo, con
# mayúsculas iniciales. El resto de los perfiles usa el mismo campo "tipo_documento"/
# "tipo_comprobante" con otro propósito: Hiopos, por ejemplo, lo usa como subtipo interno
# del POS solo para filtrar filas ("Factura venta", "Abono factura venta simplificada"), no
# como la clasificación legal del comprobante — mostrar eso tal cual rompería el badge
# Factura/Nota de Crédito que el resto del sistema ya asume.
_TIPOS_COMPROBANTE_SET_EXTRA = {
    _normalizar_tipo(t) for t in [
        "AUTOFACTURA",
        "BOLETA DE TRANSPORTE PÚBLICO DE PASAJEROS",
        "BOLETA DE VENTA",
        "BOLETA RESIMPLE",
        "BOLETOS DE LOTERÍAS, JUEGOS DE AZAR",
        "BOLETO O TICKET DE TRANSPORTE AÉREO",
        "DESPACHO DE IMPORTACIÓN",
        "ENTRADA A ESPECTÁCULOS PÚBLICOS",
        "TICKET MÁQUINA REGISTRADORA",
        "COMPROBANTE DE EGRESOS POR COMPRAS A CRÉDITO",
        "COMPROBANTE DEL EXTERIOR LEGALIZADO",
        "COMPROBANTE DE INGRESO POR VENTAS A CRÉDITO",
        "COMPROBANTE DE INGRESOS ENTIDADES PÚBLICAS, RELIGIOSAS O DE BENEFICIO PÚBLICO",
        "EXTRACTO DE CUENTA – BILLETAJE ELECTRÓNICO",
        "EXTRACTO DE CUENTA DE IPS",
        "EXTRACTO DE CUENTA TC/TD",
        "LIQUIDACIÓN DE SALARIO",
        "OTROS COMPROBANTES DE EGRESOS",
        "OTROS COMPROBANTES DE INGRESOS",
        "TRANSFERENCIAS O GIROS BANCARIOS/ BOLETA DE DEPÓSITO",
    ]
}

# Variantes que se consideran el mismo comprobante que su base en papel (solo cambia el
# medio de emisión: electrónico/virtual) — mismo criterio confirmado por el cliente sobre
# el documento "Tipo de Documentos RG -libros.xlsx": FACTURA VIRTUAL/FACTURA ELECTRONICA se
# tratan como Factura, NOTA DE CRÉDITO ELECTRONICA como Nota de Crédito, NOTA DE DÉBITO
# ELECTRONICA como Nota de Débito. Se colapsan al mismo texto — no solo para mostrarlo
# igual, sino porque detect_sequence_gaps agrupa la numeración por (local, serie, tipo_doc):
# tratarlas como un tipo aparte partiría en dos una numeración que en la práctica es una
# sola secuencia por punto de expedición.
_ALIASES_FACTURA = {_normalizar_tipo(t) for t in ["FACTURA", "FACTURA VIRTUAL", "FACTURA ELECTRONICA"]}
_ALIASES_NOTA_CREDITO = {_normalizar_tipo(t) for t in ["NOTA DE CRÉDITO", "NOTA DE CRÉDITO ELECTRONICA"]}
_ALIASES_NOTA_DEBITO = {_normalizar_tipo(t) for t in ["NOTA DE DÉBITO", "NOTA DE DÉBITO ELECTRONICA"]}


def tipo_doc_display(tipo_doc_raw: str, es_credito: bool) -> str:
    """Etiqueta de tipo de comprobante para la grilla. Factura, Nota de Crédito y Nota de
    Débito (y sus variantes electrónica/virtual — ver _ALIASES_*) siempre se muestran con su
    ortografía canónica (acentuada), sin importar cómo los haya escrito el archivo de
    origen. Los demás tipos de comprobante oficiales del SET (Boleta de Venta, Ticket
    Máquina Registradora, Autofactura, etc. — ver _TIPOS_COMPROBANTE_SET_EXTRA, todos
    Compras-only) se muestran con el texto tal como vino del archivo, con mayúsculas
    iniciales. Cualquier otro valor (subtipos internos del sistema de origen que no son
    parte de esta clasificación legal, ej. Hiopos) cae al binario Factura/Nota de Crédito
    de siempre, inferido por es_credito. (Los sets _ALIASES_*/_TIPOS_COMPROBANTE_SET_EXTRA
    se calculan una sola vez al importar el módulo — esta función se llama una vez por fila
    del libro, y con archivos reales de decenas de miles de filas recalcularlas en cada
    llamada es un costo innecesario que se nota.)"""
    if tipo_doc_raw:
        norm = _normalizar_tipo(tipo_doc_raw)
        if norm in _ALIASES_FACTURA:
            return "Factura"
        if norm in _ALIASES_NOTA_CREDITO:
            return "Nota de Crédito"
        if norm in _ALIASES_NOTA_DEBITO:
            return "Nota de Débito"
        if norm in _TIPOS_COMPROBANTE_SET_EXTRA:
            return _titulo_es(tipo_doc_raw)
    return "Nota de Crédito" if es_credito else "Factura"


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
    """Clasifica un comprobante en 10%, 5% o exento probando cuál de las tres hipótesis
    (exento: iva ≈ 0 / gravada*10% ≈ iva / gravada*5% ≈ iva) ajusta MEJOR — no cuál es la
    PRIMERA en pasar el umbral de tolerancia (Minutas de relevamiento 3 y 4). Devuelve
    (gravada_10, iva_10, gravada_5, iva_5, exenta).

    Antes se probaba en un orden fijo con "return" apenas una calzaba dentro de la
    tolerancia — para una factura de importe grande eso no generaba ambigüedad (una
    diferencia de hasta 0,6 Gs es insignificante frente a montos de miles/millones), pero
    para una factura de muy pocos guaraníes (ej. gravada 3,64 / iva 0,36 — una factura real
    al 10%, 3,64*10% = 0,364 ≈ 0,36) el propio IVA real cae dentro de esa misma tolerancia
    de "¿es cero?" y la clasificaba como exenta, perdiendo la gravada real (mostraba 0 en vez
    de 3,64). Confirmado sobre los 31 archivos Aloha reales de mayo/2026: 5 comprobantes con
    este patrón, todos con total entre 1 y 4 guaraníes.

    Comparar las tres diferencias y quedarse con la menor no tiene este problema: para ese
    mismo caso, la diferencia contra "exento" es 0,36 pero contra "10%" es apenas 0,004 —
    gana 10% con claridad. Y una factura exenta chica genuina (ej. gravada 0,50, iva 0,00)
    sigue clasificando bien: diferencia contra "exento" es 0, contra "10%" es 0,05 — sigue
    ganando exento.
    """
    diff_exento = abs(iva_bruta)
    diff_10 = abs(gravada_bruta * 0.10 - iva_bruta)
    diff_5 = abs(gravada_bruta * 0.05 - iva_bruta)
    mejor = min(diff_exento, diff_10, diff_5)

    if mejor > MONTO_TOLERANCE_CLASIFICACION:
        # Ninguna tasa conocida calzó ni siquiera de forma aproximada: se conserva como
        # gravada al 10% (caso más común) para no perder el registro, pero queda disponible
        # para revisión manual vía el total.
        return gravada_bruta, iva_bruta, 0.0, 0.0, 0.0

    if mejor == diff_exento:
        return 0.0, 0.0, 0.0, 0.0, gravada_bruta
    if mejor == diff_10:
        return gravada_bruta, iva_bruta, 0.0, 0.0, 0.0
    return 0.0, 0.0, gravada_bruta, iva_bruta, 0.0


# ---------------------------------------------------------------------------------------
# Camino vectorizado de _process_dataframe (auditoria/10-vectorizacion-process-dataframe.md)
# — SOLO para perfiles sin estado entre filas (section_marker_col es None; hoy es únicamente
# Aloha el que lo declara — ver app/profiles/*.json — así que Universal, Hiopos y RG90 SET
# entran acá). El bucle fila por fila original queda intacto y sin tocar para Aloha, donde
# el estado current_section (factura/nota de crédito, que depende de la fila anterior) no es
# un mapeo elemento por elemento y vectorizarlo sería mucho más riesgoso.
# ---------------------------------------------------------------------------------------

def classify_tax_rate_vec(gravada_bruta: np.ndarray, iva_bruta: np.ndarray) -> Tuple[np.ndarray, ...]:
    """Misma función que classify_tax_rate, vectorizada con numpy — puramente aritmética,
    sin ninguna ambigüedad de tipos que resolver (a diferencia de clean_numeric), así que se
    vectoriza directo sin necesidad de casos especiales. Mismo orden de prioridad
    (fuera de tolerancia -> exento -> 10% -> 5%, con "exento" ganando empates con "10%" tal
    como hace el if/elif original) vía np.select, que evalúa las condiciones en orden y usa
    la primera que matchea."""
    diff_exento = np.abs(iva_bruta)
    diff_10 = np.abs(gravada_bruta * 0.10 - iva_bruta)
    diff_5 = np.abs(gravada_bruta * 0.05 - iva_bruta)
    mejor = np.minimum(np.minimum(diff_exento, diff_10), diff_5)

    fuera_tolerancia = mejor > MONTO_TOLERANCE_CLASIFICACION
    es_exento = mejor == diff_exento
    es_10 = mejor == diff_10
    condiciones = [fuera_tolerancia, es_exento, es_10]

    gravada = np.select(condiciones, [gravada_bruta, 0.0, gravada_bruta], default=0.0)
    iva = np.select(condiciones, [iva_bruta, 0.0, iva_bruta], default=0.0)
    gravada_5 = np.select(condiciones, [0.0, 0.0, 0.0], default=gravada_bruta)
    iva_5 = np.select(condiciones, [0.0, 0.0, 0.0], default=iva_bruta)
    exenta = np.select(condiciones, [0.0, gravada_bruta, 0.0], default=0.0)
    return gravada, iva, gravada_5, iva_5, exenta


def _clean_numeric_series(s: pd.Series) -> pd.Series:
    """Misma función que clean_numeric, vectorizada por columna. Réplica exacta del orden
    de decisión original: None/NaN -> 0.0; valor YA int/float en origen -> float(x) directo
    (SIN pasar por el reemplazo de separadores); cualquier otra cosa (típicamente string) ->
    reemplazar "." y "," al estilo es-PY y convertir, 0.0 si falla. Esta rama por tipo es
    necesaria: pd.to_numeric por sí solo interpretaría un string como "1.5" en notación
    inglesa (1.5), mientras que clean_numeric lo trata como "1.500" al estilo es-PY (punto de
    miles) al no ser un tipo numérico nativo — no son intercambiables."""
    es_nulo = s.map(lambda v: v is None or pd.isna(v))
    es_numero_nativo = s.map(lambda v: isinstance(v, (int, float))) & ~es_nulo

    resultado = pd.Series(0.0, index=s.index)
    if es_numero_nativo.any():
        resultado.loc[es_numero_nativo] = s.loc[es_numero_nativo].astype(float)

    resto_mask = ~es_numero_nativo & ~es_nulo
    if resto_mask.any():
        textos = s.loc[resto_mask].astype(str).str.strip().str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
        resultado.loc[resto_mask] = pd.to_numeric(textos, errors="coerce").fillna(0.0)

    return resultado


def _parse_fechas_vectorizado(fecha_s: pd.Series) -> Tuple[pd.Series, pd.Series]:
    """Versión vectorizada de aplicar parse_date + format_display_date fila por fila (ver
    date_parser.py) — tres niveles, del más al menos común, cada uno reduciendo lo que le
    queda resolver al siguiente:

    1) Celdas que pandas ya parseó como datetime/date (lo más común cuando la columna de
       fecha del Excel tiene formato de fecha real, no texto) — vectorizado 100% con
       .dt.strftime().
    2) El resto, como texto: se prueban los mismos formatos de DATE_FORMATS, en el mismo
       orden, con pd.to_datetime() sobre toda la columna restante a la vez por formato, en
       vez de fila por fila con strptime.
    3) Lo que ninguno de los dos anteriores pudo resolver (seriales de Excel como número,
       valores nulos/centinela, cualquier caso raro) se delega a parse_date() original, fila
       por fila, sólo sobre ese subconjunto residual — garantiza el mismo resultado que el
       camino de siempre para esos casos sin reimplementar su lógica (que ya cubre seriales
       vía xlrd, timestamps con hora, y los valores nulos/centinela)."""
    resultado = pd.Series([None] * len(fecha_s), index=fecha_s.index, dtype=object)

    es_fecha_nativa = fecha_s.map(lambda v: isinstance(v, (datetime, date)))
    if es_fecha_nativa.any():
        resultado.loc[es_fecha_nativa] = pd.to_datetime(fecha_s.loc[es_fecha_nativa]).dt.strftime("%Y-%m-%d")

    pendientes_mask = ~es_fecha_nativa
    for fmt in DATE_FORMATS:
        if not pendientes_mask.any():
            break
        subset_idx = fecha_s.index[pendientes_mask]
        s_val = fecha_s.loc[subset_idx].astype(str).str.strip().str.split(" ", n=1).str[0]
        candidatos = pd.to_datetime(s_val, format=fmt, errors="coerce")
        ok_idx = subset_idx[candidatos.notna()]
        if len(ok_idx):
            resultado.loc[ok_idx] = candidatos.loc[ok_idx].dt.strftime("%Y-%m-%d")
            pendientes_mask.loc[ok_idx] = False

    if pendientes_mask.any():
        subset_idx = fecha_s.index[pendientes_mask]
        resultado.loc[subset_idx] = fecha_s.loc[subset_idx].map(parse_date)

    fecha_iso_s = resultado
    fecha_disp_s = fecha_iso_s.map(format_display_date)
    return fecha_iso_s, fecha_disp_s


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


def _repair_embedded_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Repara celdas donde, por un error de exportación o de edición manual del archivo de
    origen, terminaron pegadas varias filas completas dentro de una sola celda de texto
    (separadas por saltos de línea, con los campos de cada fila unidos por ';'). Confirmado
    sobre un archivo real (Libro Ventas Fabric.xls, hoja "Original", columna "Contacto"):
    esa única celda tenía 209 filas de venta completas embebidas — sin este arreglo esas
    filas quedan totalmente invisibles para el motor, no como filtradas sino como si nunca
    hubiesen existido en el DataFrame.

    Reconstruye dos cosas: (1) los valores que le correspondían a la propia fila afectada,
    a partir de esa misma columna en adelante, y (2) cada fila adicional embebida, agregada
    como una fila nueva al final del DataFrame — se procesan después exactamente igual que
    cualquier otra fila (mismos filtros, mismo mapeo de columnas).
    """
    # Chequeo rápido y vectorizado antes de entrar al loop celda por celda: en la enorme
    # mayoría de los archivos (y en la totalidad de los reportes oficiales de la RG90/RG,
    # que no pasan por edición manual) no hay ninguna celda con salto de línea, así que no
    # hay nada que reparar. Confirmado sobre un archivo real de 65.535 filas: el loop
    # completo tardaba ~30 s; este chequeo, 0,1 s.
    tiene_embebidas = any(
        df[col].dtype == object and df[col].astype(str).str.contains("\n", regex=False, na=False).any()
        for col in df.columns
    )
    if not tiene_embebidas:
        return df

    ncols = len(df.columns)
    extra_rows: List[List[Any]] = []

    # Los valores reparados son texto (vienen de partir la celda por ';'), aunque la columna
    # sea numérica en el resto del archivo — se pasa todo el DataFrame a dtype "object" antes
    # de escribir para evitar el warning/futuro error de pandas por tipo incompatible.
    df = df.astype(object)

    for row_idx in df.index:
        for col_pos in range(ncols):
            val = df.iat[row_idx, col_pos]
            if not isinstance(val, str) or "\n" not in val:
                continue

            segments = val.split("\n")

            # La primera "línea" de la celda son los valores que le tocaban a esta misma
            # fila desde esta columna en adelante (el resto de sus propias columnas
            # terminó vacío porque todo se fue a parar acá).
            own_parts = segments[0].split(";")
            for offset, part in enumerate(own_parts):
                target_col = col_pos + offset
                if target_col < ncols:
                    df.iat[row_idx, target_col] = part.strip() or None

            # Las líneas siguientes son filas completas independientes (ancho de columnas
            # del archivo) que quedaron atrapadas en la misma celda.
            for seg in segments[1:]:
                parts = seg.split(";")
                if len(parts) < ncols - 2:
                    continue  # no calza con el ancho de una fila real — se descarta
                new_row = [None] * ncols
                for i in range(min(len(parts), ncols)):
                    new_row[i] = parts[i].strip() or None
                extra_rows.append(new_row)

    if extra_rows:
        extra_df = pd.DataFrame(extra_rows, columns=df.columns)
        df = pd.concat([df, extra_df], ignore_index=True)

    return df


def _nombres_columnas_perfil(profile: Dict) -> Optional[set]:
    """Nombres de columna que el perfil realmente mapea (source_name), o None si el perfil
    mezcla mapeo por posición (source_col, ej. Aloha/Hiopos) — ahí no se puede saber de
    antemano qué nombre corresponde a cada posición sin leer el archivo primero."""
    mappings = profile.get("column_mappings", [])
    nombres = [m.get("source_name") for m in mappings if "source_name" in m]
    if not mappings or len(nombres) != len(mappings):
        return None
    return set(nombres)


def _usecols_para_perfil(profile: Dict) -> Optional[Any]:
    """Devuelve un filtro de columnas para pasarle a pd.read_excel/read_csv, cuando el
    perfil lo permite, para no leer columnas que no se van a usar.

    OJO — esto NO es lo que explica la mejora de "más de 4 minutos" a segundos que describía
    antes este mismo comentario (corregido en la auditoría de performance del 2026-09,
    /auditoria/05-performance.md): esa mejora fue el efecto combinado de otras dos cosas del
    mismo commit (40e1348) — el chequeo vectorizado previo en _repair_embedded_rows (~30 s a
    ~0,1 s) y sacar del loop de _process_dataframe el recálculo de los sets de tipos
    permitidos/notas de crédito por fila (~1 tercio del tiempo de procesamiento). Esta
    función por sí sola aporta bastante menos: medido de nuevo sobre el mismo archivo real
    ("SOLO VENTAS MAYO 2026.xls", 31 MB, 65.535 filas), pasar de 256 a 11 columnas ahorra
    apenas ~15-20% del tiempo de pd.read_excel (11,4 s vs. 14,0 s en promedio de 2 corridas).

    La razón: pandas.io.excel._xlrd.XlrdReader.get_sheet_data() (el reader que usa xlrd para
    .xls) siempre itera las 256 columnas de cada fila del archivo — usecols no evita ese
    trabajo, solo reduce qué columnas terminan armando el DataFrame final después. Para
    reducir el costo de la lectura en sí en un .xls con este patrón (formato aplicado a toda
    la grilla), hay que leer menos FILAS o CELDAS realmente parseadas por xlrd, no menos
    columnas seleccionadas después — usecols sigue siendo correcto tenerlo (reduce memoria y
    el trabajo de _process_dataframe más abajo, que si itera solo 11 columnas en vez de
    256), pero no es la palanca que explica la mejora grande documentada en el commit.

    Se devuelve un callable, no una lista, para que una columna ausente en el archivo no
    haga fallar la lectura entera (usecols=[lista] tira ValueError si algún nombre no
    aparece; usecols=callable simplemente no la incluye) — mismo criterio tolerante que ya
    usa _process_dataframe con `df[c_name] if c_name in df.columns else ...`.

    La comparación ignora espacios al inicio/final (ej. la RG de compras real trae "Monto No
    Gravado / Exento " con un espacio final que el perfil no declara): sin esto, esa columna
    se excluía silenciosamente acá mismo, antes de que _process_dataframe llegara a
    intentar leerla — el campo quedaba siempre en 0 sin ningún error, indistinguible de una
    columna genuinamente vacía.
    """
    nombres_set = _nombres_columnas_perfil(profile)
    if nombres_set is None:
        return None
    nombres_norm = {str(n).strip() for n in nombres_set}
    return lambda c: c in nombres_set or str(c).strip() in nombres_norm


def _matches_profile_signature(file_path: str, sheet_names: List[str], profile: Dict) -> bool:
    """Valida que el archivo adjuntado realmente corresponda al sistema elegido en el Paso 1,
    antes de intentar mapear ninguna fila. Confirmado sobre los 39 archivos reales de mayo
    2026 (31 Aloha + 8 Hiopos): ninguna firma da falso positivo ni falso negativo.

    - Aloha: el texto "Reporte de Facturas Emitidas" aparece siempre en las primeras filas
      (encabezado fijo de Factury/Aloha), sin depender de header_row_index.
    - Hiopos: la columna "Tipo Documento" está siempre en la fila de encabezado.

    Si el perfil no declara ninguna firma (ej. universal, rg90_set), no se valida — se
    mantiene el comportamiento anterior.
    """
    signature_text = profile.get("signature_text")
    signature_columns = profile.get("signature_columns")
    if not signature_text and not signature_columns:
        return True

    hdr_idx = profile.get("header_row_index", 0)
    for sheet in sheet_names:
        if signature_text:
            try:
                raw = pd.read_excel(file_path, sheet_name=sheet, header=None, nrows=15, engine="calamine")
            except Exception:
                continue
            texto = " ".join(str(v) for v in raw.values.flatten() if pd.notna(v))
            if signature_text.lower() in texto.lower():
                return True

        if signature_columns:
            try:
                df_hdr = pd.read_excel(file_path, sheet_name=sheet, header=hdr_idx, nrows=1, engine="calamine")
            except Exception:
                continue
            cols = {str(c).strip().lower() for c in df_hdr.columns}
            if all(col.lower() in cols for col in signature_columns):
                return True

    return False


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
        return pd.read_excel(file_path, header=hdr_idx, sheet_name=sheet_index, engine="calamine")

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
            df = _repair_embedded_rows(df)
            return self._process_dataframe(df, profile, local_name)

        # engine="calamine" en las 3 lecturas de este método (acá y las dos de más abajo) y
        # en _read_source/_matches_profile_signature: calamine (python-calamine, lector
        # Excel en Rust) reemplaza a openpyxl, el motor por defecto de pandas para .xlsx —
        # medido en /auditoria/09-performance-backend-ingesta.md: openpyxl es 93,7% del
        # tiempo total de ingesta de un archivo de 200.000 filas (celda por celda vía XML
        # puro en Python); el mismo archivo con calamine tardó 8-9x menos, sin cambiar ni
        # una fila del resultado. Validado además con archivos reales del cliente (.xls y
        # .xlsx completos, incluido el caso ya documentado de Libro Ventas Fabric.xls) —
        # calamine devuelve exactamente el mismo DataFrame (mismas hojas, mismos valores,
        # mismos tipos) que el motor anterior, confirmado celda por celda con
        # DataFrame.equals() antes de aplicar este cambio.
        sheet_names = pd.ExcelFile(file_path, engine="calamine").sheet_names

        if not _matches_profile_signature(file_path, sheet_names, profile):
            raise ValueError(
                f"El archivo no parece corresponder al formato de {profile['name']}. "
                f"Verificá que elegiste el sistema correcto (Aloha/Hiopos) para este reporte."
            )

        # Cuando el archivo trae una hoja explícitamente "cruda" (ej. "Original" en Hiopos:
        # el reporte tal como se exporta, sin edición manual), esa hoja es la fuente confiable
        # y va primera en el orden de candidatos — antes incluso que sheet_index — porque
        # otras hojas del mismo archivo (ej. "SOLO VENTAS") pueden estar editadas a mano y
        # les puede faltar comprobantes reales que sí están en la cruda (confirmado sobre un
        # caso real: 282 comprobantes de Libro Ventas Fabric.xls solo existen en "Original").
        # En los archivos reales (una sola hoja sin nombre) esto no cambia nada.
        prefer_name = str(profile.get("prefer_sheet_name", "")).strip().lower()
        candidatos = []
        if prefer_name:
            for i, name in enumerate(sheet_names):
                if str(name).strip().lower() == prefer_name:
                    candidatos.append(i)
                    break

        # El Formato Universal (Minuta) trae en el MISMO archivo una hoja de ventas y otra
        # de compras con columnas idénticas — si se cayera al fallback de abajo (probar
        # cualquier otra hoja del archivo) y la hoja preferida no matcheara por algún typo,
        # se podría terminar leyendo silenciosamente la hoja equivocada (compras como si
        # fuera ventas, o viceversa) sin que salte ningún error. Perfiles con esta bandera
        # exigen la hoja exacta: si no aparece, se corta acá, no se prueba ninguna otra.
        if profile.get("require_exact_sheet_name") and not candidatos:
            raise ValueError(
                f"El archivo no tiene una hoja llamada \"{profile.get('prefer_sheet_name')}\" — "
                f"verificá que sea el Formato Universal correcto para {profile['name']}."
            )
        if profile.get("require_exact_sheet_name"):
            candidatos = candidatos[:1]
        else:
            candidatos += [preferred_sheet] + [i for i in range(len(sheet_names)) if i != preferred_sheet and i not in candidatos]

        usecols = _usecols_para_perfil(profile)
        nombres_esperados = _nombres_columnas_perfil(profile)
        rows: List[Dict[str, Any]] = []
        cortes: List[Dict[str, Any]] = []
        for idx in candidatos:
            if idx >= len(sheet_names):
                continue

            # Con más de un candidato (ej. un archivo con varias hojas y ninguna
            # prefer_sheet_name configurada), antes de leer la hoja completa se espía
            # apenas el encabezado: si ninguna columna que el perfil espera está presente,
            # se descarta sin gastar tiempo en la lectura completa. Confirmado sobre un
            # archivo real de ~98.000 filas con una hoja de control (sin relación con la
            # RG90) antes de la hoja real: sin este chequeo, la hoja de control —igual de
            # grande— se leía entera (40 s) solo para descartarla después.
            if len(candidatos) > 1 and nombres_esperados is not None:
                try:
                    hdr_df = pd.read_excel(file_path, header=hdr_idx, sheet_name=idx, nrows=0, engine="calamine")
                except Exception:
                    hdr_df = None
                if hdr_df is not None and not (set(hdr_df.columns) & nombres_esperados):
                    continue

            df = pd.read_excel(file_path, header=hdr_idx, sheet_name=idx, usecols=usecols, engine="calamine")
            df = _repair_embedded_rows(df)
            rows, cortes = self._process_dataframe(df, profile, local_name)
            if rows:
                break
        return rows, cortes

    def _leer_hoja_openpyxl_streaming(self, file_path: str, sheet_idx: int, hdr_idx: int, usecols, chunk_size: int):
        """Generador de bloques de hasta chunk_size filas (cada fila, un dict {nombre de
        columna: valor}) de UNA hoja, leída con openpyxl en modo read_only=True.

        A diferencia de pd.read_excel(engine="calamine") (usado en todo el resto de este
        archivo, y mucho más rápido para leer una hoja ENTERA de una sola vez — ver
        auditoria/09), read_only=True de openpyxl parsea el XML de la hoja como un stream
        real (SAX-like), fila por fila, sin nunca materializar la hoja completa en memoria
        — es justo lo que hace falta acá, no velocidad. calamine (vía python-calamine) no
        expone hoy una API de lectura incremental en su binding de Python: su función
        de lectura entrega la hoja completa de una sola vez, así que no sirve para este
        propósito aunque sea más rápido para el caso no-streaming.
        """
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        try:
            ws = wb.worksheets[sheet_idx]
            filas = ws.iter_rows(values_only=True)
            for _ in range(hdr_idx):
                next(filas, None)
            header_row = next(filas, None)
            if header_row is None:
                return
            columnas = [str(c).strip() if c is not None else f"__col{i}__" for i, c in enumerate(header_row)]
            if usecols is not None:
                indices_usar = [i for i, c in enumerate(columnas) if usecols(c)]
            else:
                indices_usar = list(range(len(columnas)))
            nombres_usar = [columnas[i] for i in indices_usar]

            chunk: List[Dict[str, Any]] = []
            for row in filas:
                chunk.append({nombres_usar[j]: (row[i] if i < len(row) else None) for j, i in enumerate(indices_usar)})
                if len(chunk) >= chunk_size:
                    yield chunk
                    chunk = []
            if chunk:
                yield chunk
        finally:
            wb.close()

    def ingest_file_streaming(self, file_path: str, profile_id: str, local_name: str = "Local General", chunk_size: int = 5000):
        """Generador — misma lógica de negocio que ingest_file (mismo mapeo de columnas,
        misma función _process_dataframe_vectorizado sin cambiar una línea), pero leyendo
        el archivo con openpyxl en modo streaming real (ver _leer_hoja_openpyxl_streaming)
        y aplicando esa lógica en bloques de chunk_size filas en vez de sobre la hoja
        completa de una sola vez — yield de cada bloque YA procesado (mismo formato de
        fila que devuelve ingest_file), nunca la hoja entera cargada en memoria al mismo
        tiempo. No devuelve `cortes` (ver más abajo, por qué).

        Pensado específicamente para /api/reconcile (ver ese endpoint y
        auditoria/12-certificacion-salud-sistema.md): ingest_file(), sin cambios, seguía
        reteniendo las 200.000 filas de la RG90 como una lista completa en memoria — era
        el remanente de RSS medido después de sacar el cruce libro↔RG90 a SQLite. Esta
        función es una RUTA NUEVA Y PARALELA — ingest_file() no cambió ni una línea, ni su
        firma ni su comportamiento por defecto, y sigue siendo la que usan /api/ingest,
        /api/compras/* y el resto de /api/reconcile (el lado libro, ya resuelto con ijson).

        Alcance deliberadamente más chico que ingest_file(), por diseño, no por descuido:
        - Solo .xls/.xlsx (la RG90 siempre es Excel, ver Minuta 3 — CSV no aplica acá).
        - Solo perfiles SIN section_marker_col (el único que lo usa, Aloha, necesita
          estado entre filas para separar facturas de notas de crédito fila a fila — no
          tiene sentido partido en bloques independientes). rg90_set y universal
          califican (ver sus .json, ninguno declara marker_col).
        - Solo perfiles cuyos column_mappings mapean por source_name (no source_col
          posicional) — rg90_set y universal también califican; Aloha/Hiopos usan
          source_col, pero ya quedan afuera por el punto anterior (ambos declaran
          marker_col también).
        - No corre _repair_embedded_rows: ese arreglo es para celdas con saltos de línea
          por edición manual del archivo — su propio docstring documenta que "en la
          totalidad de los reportes oficiales de la RG90/RG, que no pasan por edición
          manual" no hay nada que reparar. No aplica a este flujo por diseño del reporte
          en sí, no por una limitación de esta función.
        - No calcula `cortes` (saltos de numeración vía cortes/subtotales): ese concepto
          es exclusivo del formato Aloha (con marker_col), que ya está excluido arriba.
        Si en algún momento hace falta usar esto para un perfil que no cumpla estas
        condiciones, hay que extenderlo a propósito — se prefirió no generalizar de más
        sin un caso real que lo pida.
        """
        profile = self.profiles.get(profile_id)
        if not profile:
            raise ValueError(f"Perfil '{profile_id}' no encontrado.")
        if profile.get("section_marker_col") is not None:
            raise ValueError(f"ingest_file_streaming no soporta el perfil '{profile_id}' (usa section_marker_col) — usar ingest_file().")

        ext = os.path.splitext(file_path)[1].lower()
        if ext not in [".xls", ".xlsx"]:
            raise ValueError("ingest_file_streaming solo soporta archivos .xls/.xlsx.")

        mappings = profile.get("column_mappings", [])
        if any("source_col" in m for m in mappings):
            raise ValueError(f"ingest_file_streaming no soporta el perfil '{profile_id}' (mapea columnas por posición, source_col) — usar ingest_file().")

        hdr_idx = profile.get("header_row_index", 0)
        preferred_sheet = profile.get("sheet_index", 0)

        # Selección de hoja candidata: MISMO criterio que ingest_file (firma del perfil,
        # prefer_sheet_name/require_exact_sheet_name, fallback probando el resto de las
        # hojas en orden) — reutiliza las mismas funciones auxiliares, sin duplicar la
        # lógica de decisión, solo cambia CÓMO se lee cada hoja candidata una vez elegida.
        sheet_names = pd.ExcelFile(file_path, engine="calamine").sheet_names
        if not _matches_profile_signature(file_path, sheet_names, profile):
            raise ValueError(
                f"El archivo no parece corresponder al formato de {profile['name']}. "
                f"Verificá que elegiste el sistema correcto para este reporte."
            )

        prefer_name = str(profile.get("prefer_sheet_name", "")).strip().lower()
        candidatos: List[int] = []
        if prefer_name:
            for i, name in enumerate(sheet_names):
                if str(name).strip().lower() == prefer_name:
                    candidatos.append(i)
                    break
        if profile.get("require_exact_sheet_name") and not candidatos:
            raise ValueError(
                f"El archivo no tiene una hoja llamada \"{profile.get('prefer_sheet_name')}\" — "
                f"verificá que sea el archivo correcto para {profile['name']}."
            )
        if profile.get("require_exact_sheet_name"):
            candidatos = candidatos[:1]
        else:
            candidatos += [preferred_sheet] + [i for i in range(len(sheet_names)) if i != preferred_sheet and i not in candidatos]

        usecols = _usecols_para_perfil(profile)
        nombres_esperados = _nombres_columnas_perfil(profile)
        target_fields = {m["target_field"] for m in mappings}
        usa_clasificacion_tasa = "gravada_bruta" in target_fields
        permitidos_norm = (
            {_normalizar_tipo(t) for t in profile.get("tipo_documento_permitidos")}
            if profile.get("tipo_documento_permitidos") is not None else None
        )
        notas_credito_por_tipo = {_normalizar_tipo(t) for t in profile.get("tipo_documento_notas_credito", [])}
        serie_prefijos = profile.get("serie_prefijos_no_fiscales", [])

        for idx in candidatos:
            if idx >= len(sheet_names):
                continue

            # Mismo espionaje barato del encabezado que ingest_file, antes de leer la hoja
            # entera — acá igual de válido: nrows=0 no carga ninguna fila de datos.
            if len(candidatos) > 1 and nombres_esperados is not None:
                try:
                    hdr_df = pd.read_excel(file_path, header=hdr_idx, sheet_name=idx, nrows=0, engine="calamine")
                except Exception:
                    hdr_df = None
                if hdr_df is not None and not (set(hdr_df.columns) & nombres_esperados):
                    continue

            produjo_alguna = False
            for chunk_dicts in self._leer_hoja_openpyxl_streaming(file_path, idx, hdr_idx, usecols, chunk_size):
                extracted_cols = {}
                for col_spec in mappings:
                    target = col_spec["target_field"]
                    c_name = col_spec.get("source_name")
                    extracted_cols[target] = [row.get(c_name) for row in chunk_dicts]
                records_df_chunk = pd.DataFrame(extracted_cols)
                if len(records_df_chunk) == 0:
                    continue
                processed_rows, _cortes = self._process_dataframe_vectorizado(
                    records_df_chunk, profile, local_name, usa_clasificacion_tasa,
                    permitidos_norm, notas_credito_por_tipo, serie_prefijos,
                )
                if processed_rows:
                    produjo_alguna = True
                    yield processed_rows
            if produjo_alguna:
                return
        # Ningún candidato produjo filas -- mismo resultado final que ingest_file cuando
        # ninguna hoja matchea (ahí devuelve rows=[]): acá, simplemente no se yield-ea nada.

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

        # Normalizados UNA sola vez por archivo, no por fila — con un archivo real de
        # 65.535 filas, recalcular estos dos sets en cada iteración (como quedó al agregar
        # el filtro de tipos de comprobante) representaba ~1 millón de llamadas de más a
        # _normalizar_tipo y cerca de un tercio del tiempo total de procesamiento.
        tipos_permitidos = profile.get("tipo_documento_permitidos")
        permitidos_norm = {_normalizar_tipo(t) for t in tipos_permitidos} if tipos_permitidos is not None else None
        notas_credito_por_tipo = {_normalizar_tipo(t) for t in profile.get("tipo_documento_notas_credito", [])}
        serie_prefijos = profile.get("serie_prefijos_no_fiscales", [])

        n = len(records_df)

        if n == 0:
            return processed_rows, cortes

        if marker_col is None:
            return self._process_dataframe_vectorizado(
                records_df, profile, local_name, usa_clasificacion_tasa,
                permitidos_norm, notas_credito_por_tipo, serie_prefijos,
            )

        def _col(name: str) -> list:
            return records_df[name].tolist() if name in records_df.columns else [None] * n

        # Se extrae cada columna UNA sola vez como lista nativa de Python (reemplaza
        # records_df.iterrows()) — medido en la auditoría de performance
        # (/auditoria/05-performance.md): sobre un archivo real de 65.535 filas, iterrows()
        # reconstruye una Series pandas (con coerción de tipo heterogénea) en cada una de
        # esas 65.535 vueltas solo para poder leer r.get(...), y eso solo ya representaba
        # ~46% del tiempo total de ingesta de ese archivo. Iterar sobre listas nativas con
        # índice numérico mantiene EXACTAMENTE la misma lógica fila por fila de abajo (mismos
        # "continue", mismo orden de chequeos, mismas llamadas a las mismas funciones) — no
        # es una reescritura vectorizada de la lógica en sí (eso quedó fuera de alcance por
        # el riesgo de alterar el resultado del state machine de Aloha, ver marker_col más
        # abajo), solo se saca el costo de reconstruir una Series por fila.
        tipo_documento_l = _col("tipo_documento")
        serie_l = _col("serie")
        numero_comprobante_l = _col("numero_comprobante")
        fecha_l = _col("fecha")
        total_l = _col("total")
        gravada_bruta_l = _col("gravada_bruta")
        iva_bruta_l = _col("iva_bruta")
        gravada_10_l = _col("gravada_10")
        iva_10_l = _col("iva_10")
        gravada_5_l = _col("gravada_5")
        iva_5_l = _col("iva_5")
        exenta_l = _col("exenta")
        ruc_l = _col("ruc")
        nombre_cliente_l = _col("nombre_cliente")
        estado_l = _col("estado")

        for idx in range(n):
            if marker_col is not None and marker_col < len(df.columns):
                raw_marker = df.iat[idx, marker_col]
                if pd.notna(raw_marker):
                    marker_txt = str(raw_marker).strip().upper()
                    if credit_marker and marker_txt == credit_marker:
                        current_section = "credito"
                    elif marker_txt == factura_marker:
                        current_section = "factura"

            # Los cortes/subtotales ("Serie: 001 Totales", etc.) son un artefacto propio del
            # formato de Aloha (imprime esas filas al cierre de cada bloque) — para el resto
            # de los perfiles esto es trabajo desperdiciado, y df.iloc[idx] (acceso posicional
            # fila a fila) es particularmente lento dentro de un loop de miles de filas. Se
            # deja intacto (sin optimizar): solo corre para perfiles con marker_col (Aloha),
            # que no es el camino que domina el tiempo total (ver auditoría de performance).
            if marker_col is not None:
                corte = _extract_corte(df.iloc[idx].tolist(), current_section)
                if corte:
                    cortes.append(corte)

            # "Tipo de Comprobante"/"Tipo Documento" crudo, tal como viene del archivo —
            # se captura siempre (no solo cuando hay lista de permitidos) porque más abajo
            # también se usa para armar el tipo_doc que se muestra en la grilla (ver el
            # cierre de este for), en vez de forzar todo a Factura/Nota de Crédito.
            tipo_doc_raw = fix_mojibake(str(tipo_documento_l[idx] or "").strip())

            # Filtro explícito por tipo de comprobante, declarado por perfil: Hiopos solo
            # procesa factura de venta, factura de venta simplificada, abono factura de venta
            # y abono factura de venta simplificada (descarta pedido/albarán/factura de
            # compra, merma, invitación, recuento, filas vacías); la RG90 y el Formato
            # Universal listan los tipos de comprobante oficiales del SET que aplican a
            # Ventas (Factura, Nota de Crédito, Nota de Débito, Boleta de Venta, Ticket
            # Máquina Registradora, etc. — ver perfiles). El resto se descarta acá
            # directamente, sin depender únicamente de que su serie calce con el patrón
            # EEE-PPP.
            if permitidos_norm is not None and _normalizar_tipo(tipo_doc_raw) not in permitidos_norm:
                continue

            serie = str(serie_l[idx] or "").strip()
            serie_norm = fix_mojibake(serie)
            if serie_norm.lower() in ["anulación", "anulacion"]:
                continue

            # Algunos locales de Hiopos anteponen un prefijo sin significado fiscal a la
            # serie (ej. "FE" de "Factura Electrónica": FE045-001) que no forma parte del
            # punto de expedición real y antes hacía que la serie no calzara con el patrón
            # EEE-PPP, descartando comprobantes válidos por completo (confirmado sobre un
            # caso real: 879-1.063 facturas reales de Fabric Sushi se perdían enteras por
            # esto). El prefijo se declara en el perfil, no se hardcodea acá.
            for prefijo in serie_prefijos:
                if serie_norm.upper().startswith(prefijo.upper()):
                    serie_norm = serie_norm[len(prefijo):]
                    break

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

            # Tercera forma de detectar nota de crédito, para perfiles que no tienen ni
            # marcador de sección (Aloha) ni prefijo "NC" en la serie (Hiopos) — el Formato
            # Universal (Minuta) y la RG90 traen un campo "Tipo" por fila ("FACTURA" / "NOTA
            # DE CREDITO") en vez de eso. El monto ya viene en negativo en el archivo de
            # origen para estas filas (Universal), así que acá solo hace falta marcar
            # es_credito; el signo final se recalcula igual (abs + signo) más abajo. Los
            # demás tipos permitidos (Nota de Débito, Boleta de Venta, Ticket Máquina
            # Registradora, etc.) NO entran acá — quedan con signo positivo, igual que una
            # Factura.
            if notas_credito_por_tipo and _normalizar_tipo(tipo_doc_raw) in notas_credito_por_tipo:
                es_credito = True

            raw_doc = numero_comprobante_l[idx]
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

            fecha_iso = parse_date(fecha_l[idx])
            fecha_disp = format_display_date(fecha_iso)

            # Convención del libro propio (no de la comparación contra RG90, que sigue en
            # valor absoluto en reconcile_with_rg90): una nota de crédito resta de la venta,
            # así que sus montos quedan en negativo — es lo que permite que "Total Neto" dé la
            # venta neta real, tal como lo imprime el cliente en su Excel de referencia.
            total = -abs(clean_numeric(total_l[idx])) if es_credito else abs(clean_numeric(total_l[idx]))

            if usa_clasificacion_tasa:
                gravada_bruta = -abs(clean_numeric(gravada_bruta_l[idx])) if es_credito else abs(clean_numeric(gravada_bruta_l[idx]))
                iva_bruta = -abs(clean_numeric(iva_bruta_l[idx])) if es_credito else abs(clean_numeric(iva_bruta_l[idx]))
                gravada, iva, gravada_5, iva_5, exenta = classify_tax_rate(gravada_bruta, iva_bruta)
            else:
                signo = -1 if es_credito else 1
                gravada = signo * abs(clean_numeric(gravada_10_l[idx]))
                iva = signo * abs(clean_numeric(iva_10_l[idx]))
                gravada_5 = signo * abs(clean_numeric(gravada_5_l[idx]))
                iva_5 = signo * abs(clean_numeric(iva_5_l[idx]))
                exenta = signo * abs(clean_numeric(exenta_l[idx]))

                if total != 0 and gravada == 0 and gravada_5 == 0 and exenta == 0:
                    gravada = signo * round(abs(total) / 1.1, 0)
                    iva = total - gravada

            ruc = str(ruc_l[idx] or "").strip()
            if not ruc or ruc.upper() in ["NAN", "NONE", "NULL", "X"]:
                ruc = "X"

            nombre = fix_mojibake(str(nombre_cliente_l[idx] or "").strip())
            if not nombre or nombre.upper() in ["NAN", "NONE", "NULL", "SIN NOMBRE"]:
                nombre = "SIN NOMBRE"

            estado = str(estado_l[idx] or "Válida").strip()
            if estado.upper() == "E":
                estado = "Válida"
            elif estado.upper() == "A":
                estado = "Anulada"
            elif total == 0 and estado.lower() != "anulada":
                estado = "Anulada"

            processed_rows.append({
                "doc": doc,
                "sistema": profile["name"],
                "local": local_name,
                "fecha": fecha_disp,
                "fecha_iso": fecha_iso,
                "ruc": ruc,
                "nombre": nombre,
                "gravadas": fmt_gs(gravada),
                "iva": fmt_gs(iva),
                "gravadas_5": fmt_gs(gravada_5),
                "iva_5": fmt_gs(iva_5),
                "exentas": fmt_gs(exenta),
                "total": fmt_gs(total),
                "gravadas_num": gravada,
                "iva_num": iva,
                "gravadas_5_num": gravada_5,
                "iva_5_num": iva_5,
                "exentas_num": exenta,
                "total_num": total,
                "estado": estado,
                # Se muestra el tipo de comprobante tal como vino del archivo (Boleta de
                # Venta, Nota de Débito, Ticket Máquina Registradora, etc. — ver Tipos de
                # Comprobante del SET) en vez de forzar todo a Factura/Nota de Crédito;
                # Aloha no trae este campo por fila (usa el marcador de sección), así que
                # ahí se sigue infiriendo por es_credito.
                "tipo_doc": tipo_doc_display(tipo_doc_raw, es_credito),
            })

        return processed_rows, cortes

    def _process_dataframe_vectorizado(
        self,
        records_df: pd.DataFrame,
        profile: Dict,
        local_name: str,
        usa_clasificacion_tasa: bool,
        permitidos_norm: Optional[set],
        notas_credito_por_tipo: set,
        serie_prefijos: List[str],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Camino vectorizado de _process_dataframe, para perfiles SIN estado entre filas
        (marker_col es None — ver auditoria/10-vectorizacion-process-dataframe.md). Replica,
        columna por columna en vez de fila por fila, exactamente la misma secuencia de
        chequeos/transformaciones del bucle original (_process_dataframe arriba, camino
        marker_col is not None) — mismo orden de descarte, mismas funciones de limpieza,
        mismo criterio de valor por defecto en cada caso. cortes siempre vacío acá: los
        cortes/subtotales son un artefacto exclusivo del formato de Aloha (único perfil con
        marker_col), que nunca entra a este método."""
        cortes: List[Dict[str, Any]] = []
        n = len(records_df)

        def _col_s(name: str) -> pd.Series:
            return records_df[name] if name in records_df.columns else pd.Series([None] * n, index=records_df.index)

        tipo_documento_s = _col_s("tipo_documento")
        serie_s = _col_s("serie")
        numero_comprobante_s = _col_s("numero_comprobante")
        fecha_s = _col_s("fecha")
        total_s = _col_s("total")
        gravada_bruta_s = _col_s("gravada_bruta")
        iva_bruta_s = _col_s("iva_bruta")
        gravada_10_s = _col_s("gravada_10")
        iva_10_s = _col_s("iva_10")
        gravada_5_s = _col_s("gravada_5")
        iva_5_s = _col_s("iva_5")
        exenta_s = _col_s("exenta")
        ruc_s = _col_s("ruc")
        nombre_cliente_s = _col_s("nombre_cliente")
        estado_s = _col_s("estado")

        # tipo_doc_raw: str(x or "").strip() y después fix_mojibake — se aplica con .map()
        # (no un ufunc numpy) para preservar EXACTO el chequeo "truthy" de Python: None -> "",
        # pero NaN (float) es "truthy" en Python y termina como la string "nan", no "" — una
        # diferencia real que un reemplazo ingenuo con fillna("") rompería.
        tipo_doc_raw_s = tipo_documento_s.map(lambda v: str(v or "").strip()).map(fix_mojibake)
        tipo_doc_raw_norm_s = tipo_doc_raw_s.map(_normalizar_tipo)

        mask = pd.Series(True, index=records_df.index)
        if permitidos_norm is not None:
            mask &= tipo_doc_raw_norm_s.isin(permitidos_norm)

        serie_pre_prefijo_s = serie_s.map(lambda v: str(v or "").strip()).map(fix_mojibake)
        mask &= ~serie_pre_prefijo_s.str.lower().isin(["anulación", "anulacion"])

        # Prefijos no fiscales (ej. "FE" de Hiopos): típicamente 0-2 declarados por perfil,
        # no vale la pena vectorizar el propio for/break — se aplica una vez por fila vía
        # .map() para preservar "el primero que matchea, corta" del original.
        if serie_prefijos:
            def _quitar_prefijo(s: str) -> str:
                for prefijo in serie_prefijos:
                    if s.upper().startswith(prefijo.upper()):
                        return s[len(prefijo):]
                return s
            serie_norm_s = serie_pre_prefijo_s.map(_quitar_prefijo)
        else:
            serie_norm_s = serie_pre_prefijo_s

        serie_no_vacia = serie_norm_s != ""
        mask &= ~(serie_no_vacia & ~serie_norm_s.str.upper().str.match(SERIE_PATTERN))

        es_credito_s = serie_norm_s.str.upper().str.startswith("NC")
        serie_for_doc_s = serie_norm_s.where(~es_credito_s, serie_norm_s.str.slice(2))
        if notas_credito_por_tipo:
            es_credito_s = es_credito_s | tipo_doc_raw_norm_s.isin(notas_credito_por_tipo)

        # doc: misma lógica de 3 ramas (try int(float(...)), except ValueError, o directo)
        # que el original, vía .map() sobre pares (serie_for_doc, raw_doc) — el branching con
        # try/except no tiene un equivalente numpy limpio y seguro, así que se preserva
        # literal, solo aplicado columna por columna en vez de dentro del loop principal.
        def _armar_doc(par: Tuple[str, Any]) -> Optional[str]:
            serie_for_doc, raw_doc = par
            if serie_for_doc and not str(raw_doc).startswith(serie_for_doc):
                try:
                    seq_int = int(float(str(raw_doc)))
                    return f"{serie_for_doc}-{seq_int:07d}"
                except ValueError:
                    return normalize_invoice_number(f"{serie_for_doc}-{raw_doc}")
            return normalize_invoice_number(raw_doc)

        doc_s = pd.Series(
            [_armar_doc(par) for par in zip(serie_for_doc_s, numero_comprobante_s)],
            index=records_df.index,
        )
        mask &= doc_s.map(lambda d: bool(d) and bool(DOC_PATTERN.match(d)))

        idx_keep = records_df.index[mask]
        if len(idx_keep) == 0:
            return [], cortes

        doc_kept = doc_s.loc[idx_keep]
        es_credito_kept = es_credito_s.loc[idx_keep].to_numpy()
        tipo_doc_raw_kept = tipo_doc_raw_s.loc[idx_keep]

        # Fechas — pd.to_datetime en cascada por formato sobre la columna completa (la mayor
        # ganancia medida en la auditoría), con el mismo parse_date original como respaldo
        # fila por fila solo para lo que quede sin resolver (seriales de Excel, nulos).
        fecha_iso_s, fecha_disp_s = _parse_fechas_vectorizado(fecha_s.loc[idx_keep])

        # Montos — pd.to_numeric vectorizado (via _clean_numeric_series, que preserva la
        # rama por tipo de clean_numeric — ver su docstring).
        total_num = _clean_numeric_series(total_s.loc[idx_keep]).to_numpy()
        total_kept = np.where(es_credito_kept, -np.abs(total_num), np.abs(total_num))

        if usa_clasificacion_tasa:
            gravada_bruta_num = _clean_numeric_series(gravada_bruta_s.loc[idx_keep]).to_numpy()
            iva_bruta_num = _clean_numeric_series(iva_bruta_s.loc[idx_keep]).to_numpy()
            gravada_bruta_kept = np.where(es_credito_kept, -np.abs(gravada_bruta_num), np.abs(gravada_bruta_num))
            iva_bruta_kept = np.where(es_credito_kept, -np.abs(iva_bruta_num), np.abs(iva_bruta_num))
            gravada_kept, iva_kept, gravada_5_kept, iva_5_kept, exenta_kept = classify_tax_rate_vec(gravada_bruta_kept, iva_bruta_kept)
        else:
            signo_kept = np.where(es_credito_kept, -1, 1)
            gravada_kept = signo_kept * np.abs(_clean_numeric_series(gravada_10_s.loc[idx_keep]).to_numpy())
            iva_kept = signo_kept * np.abs(_clean_numeric_series(iva_10_s.loc[idx_keep]).to_numpy())
            gravada_5_kept = signo_kept * np.abs(_clean_numeric_series(gravada_5_s.loc[idx_keep]).to_numpy())
            iva_5_kept = signo_kept * np.abs(_clean_numeric_series(iva_5_s.loc[idx_keep]).to_numpy())
            exenta_kept = signo_kept * np.abs(_clean_numeric_series(exenta_s.loc[idx_keep]).to_numpy())

            fallback_mask = (total_kept != 0) & (gravada_kept == 0) & (gravada_5_kept == 0) & (exenta_kept == 0)
            gravada_fallback = signo_kept * np.round(np.abs(total_kept) / 1.1, 0)
            iva_fallback = total_kept - gravada_fallback
            gravada_kept = np.where(fallback_mask, gravada_fallback, gravada_kept)
            iva_kept = np.where(fallback_mask, iva_fallback, iva_kept)

        ruc_kept = ruc_s.loc[idx_keep].map(lambda v: str(v or "").strip())
        ruc_bad = ruc_kept.eq("") | ruc_kept.str.upper().isin(["NAN", "NONE", "NULL", "X"])
        ruc_kept = ruc_kept.where(~ruc_bad, "X")

        nombre_kept = nombre_cliente_s.loc[idx_keep].map(lambda v: str(v or "").strip()).map(fix_mojibake)
        nombre_bad = nombre_kept.eq("") | nombre_kept.str.upper().isin(["NAN", "NONE", "NULL", "SIN NOMBRE"])
        nombre_kept = nombre_kept.where(~nombre_bad, "SIN NOMBRE")

        # estado: default "Válida" con el mismo chequeo truthy de Python (str(x or "Válida")
        # — un NaN, al ser truthy, NO cae en el default, termina como la string "nan", igual
        # que en el original), después E/A/derivado-de-total==0 con la misma prioridad
        # if/elif del original vía np.select.
        estado_kept = estado_s.loc[idx_keep].map(lambda v: str(v or "Válida").strip())
        estado_upper = estado_kept.str.upper().to_numpy()
        estado_lower = estado_kept.str.lower().to_numpy()
        cond_e = estado_upper == "E"
        cond_a = estado_upper == "A"
        cond_total_cero = (total_kept == 0) & (estado_lower != "anulada")
        estado_final = np.select([cond_e, cond_a, cond_total_cero], ["Válida", "Anulada", "Anulada"], default=estado_kept.to_numpy())

        tipo_doc_kept = [tipo_doc_display(t, bool(ec)) for t, ec in zip(tipo_doc_raw_kept, es_credito_kept)]

        # .iat[i]/.loc[i] dentro del listcomp de abajo tienen overhead real por llamada (cada
        # acceso pasa por la maquinaria de indexado de pandas) — se extraen a listas nativas
        # UNA sola vez antes del loop, mismo criterio que _col() ya usaba en el camino
        # original para evitar precisamente ese costo fila por fila.
        doc_l = doc_kept.tolist()
        fecha_disp_l = fecha_disp_s.tolist()
        fecha_iso_l = fecha_iso_s.tolist()
        ruc_l = ruc_kept.tolist()
        nombre_l = nombre_kept.tolist()

        sistema = profile["name"]
        processed_rows = [
            {
                "doc": doc_l[i],
                "sistema": sistema,
                "local": local_name,
                "fecha": fecha_disp_l[i],
                "fecha_iso": fecha_iso_l[i],
                "ruc": ruc_l[i],
                "nombre": nombre_l[i],
                "gravadas": fmt_gs(gravada_kept[i]),
                "iva": fmt_gs(iva_kept[i]),
                "gravadas_5": fmt_gs(gravada_5_kept[i]),
                "iva_5": fmt_gs(iva_5_kept[i]),
                "exentas": fmt_gs(exenta_kept[i]),
                "total": fmt_gs(total_kept[i]),
                "gravadas_num": float(gravada_kept[i]),
                "iva_num": float(iva_kept[i]),
                "gravadas_5_num": float(gravada_5_kept[i]),
                "iva_5_num": float(iva_5_kept[i]),
                "exentas_num": float(exenta_kept[i]),
                "total_num": float(total_kept[i]),
                "estado": estado_final[i],
                "tipo_doc": tipo_doc_kept[i],
            }
            for i in range(len(idx_keep))
        ]
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

    Para filas de compras (que traen "ruc_proveedor") se agrega el RUC del proveedor a la
    clave de agrupación: a diferencia de ventas, donde todos los documentos los emite la
    misma entidad, en la RG de compras el mismo establecimiento-punto de expedición puede
    repetirse entre proveedores distintos (cada uno con su propia numeración) — sin esto,
    dos facturas de proveedores distintos con esos mismos tres dígitos se verían, por
    error, como parte de una única secuencia con un salto entre ellas.
    """
    grouped = {}
    for r in rows:
        doc = r["doc"]
        m = re.match(r"^(\d{3}-\d{3})-(\d{7})$", doc)
        if not m:
            continue
        tipo = r.get("tipo_doc", "Factura")
        proveedor_prefix = f"{r['ruc_proveedor']} — " if r.get("ruc_proveedor") else ""
        series = f"{proveedor_prefix}{r['local']} ({m.group(1)}) — {tipo}"
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
                gap: Dict[str, Any] = {
                    "local": curr_r["local"],
                    "sistema": curr_r["sistema"],
                    "tipo_doc": curr_r.get("tipo_doc", "Factura"),
                    "ultimo": next_doc,
                    "salto": gap_label,
                    "cantidad": diff - 1,
                    "estado": "Pendiente de revisión"
                }
                if curr_r.get("proveedor"):
                    gap["proveedor"] = curr_r["proveedor"]
                gaps.append(gap)

    return gaps


def _monto_diff(a: float, b: float) -> Optional[float]:
    diff = round(a - b, 2)
    return None if abs(diff) <= MONTO_TOLERANCE_DIFERENCIA else diff


def reconcile_with_rg90(libro_rows: List[Dict[str, Any]], rg90_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Runs FULL OUTER JOIN reconciliation between POS Sales Book and RG90 SET Report.

    El control por campo (Total, IVA 10%, IVA 5%, Exenta) replica la planilla real del
    cliente ("Check RG Total / IVA 10% / IVA 5% / Exentas"), en vez de comparar solo el
    total — la RG90 no permite validar por "gravada" porque esa columna viene mal
    calculada (incluye el IVA, según Minuta 4).

    Wrapper de compatibilidad sobre reconcile_with_rg90_iter (abajo): agota el generador en
    una lista. /api/reconcile (main.py) ya NO llama a esta función a 200.000 filas —usa el
    generador directamente para poder transmitir el resultado en streaming sin retener la
    lista completa de diffs enriquecidos en memoria (ver auditoria/12, hallazgo de OOM:
    un pedido de 200k filas hacía que un worker pasara de 130MB a 1,83GB de RSS y muriera).
    Se deja esta función tal cual para no romper otros llamadores futuros que sí quieran la
    lista completa de una vez.
    """
    libro_map = {r["doc"]: r for r in libro_rows}
    rg90_map = {r["doc"]: r for r in rg90_rows}
    return list(reconcile_with_rg90_iter(libro_map, rg90_map))


def _comparar_par(doc: str, pos_rec: Optional[Dict[str, Any]], rg_rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """La lógica de negocio de la comparación, para UN comprobante a la vez — exactamente
    las mismas ramas y criterios que tenía reconcile_with_rg90 desde siempre (ver el
    historial de esa función: control por campo, Anulada, Rechazada, etc.), ahora aisladas
    en su propia función para poder alimentarlas tanto desde dos mapas completos en memoria
    (reconcile_with_rg90_iter, abajo) como desde un cursor de un JOIN hecho en SQLite
    (reconcile_with_rg90_iter_pares, y app/api/main.py) sin duplicar ni una condición.
    Nunca se movió ningún cálculo a SQL — esta función es la única que decide qué es una
    diferencia y de qué tipo; SQLite solo empareja `doc`, no sabe nada de montos.
    """
    if pos_rec and not rg_rec:
        if pos_rec["estado"].lower() == "anulada":
            # Un comprobante anulado nunca llega a informarse a la RG90: es el
            # comportamiento esperado, no una diferencia a revisar (dato confirmado con
            # archivos reales: 81 de 657 anuladas en Aloha Sheraton, ninguna en RG90).
            return {
                "doc": doc,
                "tipo_doc": pos_rec.get("tipo_doc", ""),
                "sistema": pos_rec["sistema"],
                "local": pos_rec["local"],
                "libro": _lado_diff_ventas(pos_rec),
                "rg90": _lado_diff_ventas(None),
                "diferencia": "Anulada"
            }
        return {
            "doc": doc,
            "tipo_doc": pos_rec.get("tipo_doc", ""),
            "sistema": pos_rec["sistema"],
            "local": pos_rec["local"],
            "libro": _lado_diff_ventas(pos_rec),
            "rg90": _lado_diff_ventas(None),
            "diferencia": "No llegó a la interfaz"
        }
    elif rg_rec and not pos_rec:
        return {
            "doc": doc,
            "tipo_doc": rg_rec.get("tipo_doc", ""),
            "sistema": rg_rec.get("sistema", "RG90"),
            "local": rg_rec.get("local", "Desconocido"),
            "libro": _lado_diff_ventas(None),
            "rg90": _lado_diff_ventas(rg_rec),
            "diferencia": "No en libro propio"
        }
    else:
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
            return {
                "doc": doc,
                "tipo_doc": pos_rec.get("tipo_doc", ""),
                "sistema": pos_rec["sistema"],
                "local": pos_rec["local"],
                "libro": _lado_diff_ventas(pos_rec),
                "rg90": _lado_diff_ventas(rg_rec),
                "diferencia": "Diferencia de monto",
                "diferencias_detalle": campo_diffs,
            }
        elif rg_rec.get("estado", "").lower() == "rechazada":
            return {
                "doc": doc,
                "tipo_doc": pos_rec.get("tipo_doc", ""),
                "sistema": pos_rec["sistema"],
                "local": pos_rec["local"],
                "libro": _lado_diff_ventas(pos_rec),
                "rg90": _lado_diff_ventas(rg_rec),
                "diferencia": "Rechazada"
            }
        elif pos_rec["estado"].lower() == "anulada" or rg_rec.get("estado", "").lower() == "anulada":
            return {
                "doc": doc,
                "tipo_doc": pos_rec.get("tipo_doc", ""),
                "sistema": pos_rec["sistema"],
                "local": pos_rec["local"],
                "libro": _lado_diff_ventas(pos_rec),
                "rg90": _lado_diff_ventas(rg_rec),
                "diferencia": "Anulada"
            }
        else:
            # El comprobante coincide (mismo doc en ambos lados, sin diferencia de
            # monto ni motivo de descarte) — antes no se guardaba nada acá, así que la
            # tarjeta "Coinciden" no tenía ninguna fila real detrás: se calculaba por
            # resta (total - el resto de las categorías) y al presionarla no mostraba
            # nada. Ahora queda como una categoría más de "diferencia", igual que las
            # demás, para que el botón funcione igual que el resto de las tarjetas.
            return {
                "doc": doc,
                "tipo_doc": pos_rec.get("tipo_doc", ""),
                "sistema": pos_rec["sistema"],
                "local": pos_rec["local"],
                "libro": _lado_diff_ventas(pos_rec),
                "rg90": _lado_diff_ventas(rg_rec),
                "diferencia": "Coincide"
            }


def reconcile_with_rg90_iter(libro_map: Dict[str, Dict[str, Any]], rg90_map: Dict[str, Dict[str, Any]]):
    """Misma lógica de comparación que reconcile_with_rg90, fila por fila, como generador
    en vez de una lista — recibe los mapas ya armados (doc -> fila) en vez de las listas.
    Wrapper delgado sobre _comparar_par, para quien todavía tenga ambos mapas completos en
    memoria (ya no es el caso de /api/reconcile, que desde el cruce por SQLite usa
    reconcile_with_rg90_iter_pares en su lugar — se deja esta función para no romper otros
    posibles llamadores que sí quieran pasar los mapas directo)."""
    all_docs = set(libro_map.keys()).union(set(rg90_map.keys()))
    for doc in sorted(all_docs):
        yield _comparar_par(doc, libro_map.get(doc), rg90_map.get(doc))


def reconcile_with_rg90_iter_pares(pares):
    """Misma lógica de comparación, para pares (doc, pos_rec_o_None, rg_rec_o_None) ya
    emparejados de antemano — pensado para recibir directo el cursor de un JOIN hecho en
    SQLite (ver /api/reconcile en main.py), sin necesitar los dos mapas completos en
    memoria de Python al mismo tiempo. Es la misma lógica de negocio que
    reconcile_with_rg90_iter, solo cambia de dónde vienen los pares ya casados."""
    for doc, pos_rec, rg_rec in pares:
        yield _comparar_par(doc, pos_rec, rg_rec)


def _lado_diff_ventas(rec: Dict[str, Any] | None) -> Dict[str, str]:
    """Arma el desglose (gravada 10%/5%, IVA 10%/5%, exenta, total) de un lado de la
    comparación (libro de ventas o RG90) — mismo criterio que _lado_diff() en
    compras_engine.py. Vacío ('—') cuando ese lado no tiene el comprobante."""
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
