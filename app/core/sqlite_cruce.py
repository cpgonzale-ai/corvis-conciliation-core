"""Infraestructura compartida para el cruce Libro↔RG vía SQLite temporal, usada tanto por
/api/reconcile (Ventas, app/api/main.py) como por /api/compras/reconcile (Compras,
app/api/compras.py).

Extraído de main.py (sin cambiar ninguna línea de lógica) al portar el mismo refactor
anti-OOM de Ventas a Compras -- antes estas 3 funciones vivían ahí y Compras las hubiera
tenido que duplicar entero para tener el mismo streaming. Nada acá conoce reglas de
negocio de ningún módulo: solo arma/llena la base SQLite temporal y diagnostica
duplicados por clave -- qué es una diferencia, de qué tipo, sigue siendo Python puro en
cada motor (_comparar_par en engine.py, _comparar_par_compras en compras_engine.py).

tabla_a/tabla_b y origen_default tienen como valor por defecto lo que Ventas ya usaba
("libro"/"rg90", "RG90") para no cambiarle el comportamiento -- Compras pasa
("libro", "rg", "RG") explícitamente en sus propios call sites.
"""

import os
import sqlite3
import tempfile

from fastapi import HTTPException


def armar_error_duplicados(items: list[dict], origen_default: str = "RG90") -> dict:
    """Arma el detail estructurado de comprobantes_duplicados (tipo/origen/titulo/mensaje/
    resumen/detalle/aclaracion) a partir de una lista ya detectada de items -- cada uno con
    su propio origen ('Libro' o el valor de origen_default, ej. 'RG90'/'RG'). Puede venir de
    un solo origen (las validaciones de un archivo aislado, /api/validar-duplicados-libro y
    /api/validar-duplicados-rg90, o el caso de siempre donde solo un lado tiene duplicados)
    o de AMBOS orígenes juntos (ver /api/reconcile: cuando el libro y la RG90 tienen
    duplicados a la vez, se sigue cargando y diagnosticando la RG90 aunque el libro ya haya
    fallado, para reportar TODO de una sola vez -- antes se cortaba en el primer origen con
    problemas y la RG90 ni se llegaba a leer, dejando sus duplicados sin reportar hasta una
    segunda vuelta)."""
    origenes_presentes = [o for o in ("Libro", origen_default) if any(it["origen"] == o for it in items)]
    resumen = [{"origen": o, "cantidad": sum(1 for it in items if it["origen"] == o)} for o in origenes_presentes]
    if len(origenes_presentes) == 1:
        origen_unico = origenes_presentes[0]
        mensaje = f"El archivo adjuntado del {origen_unico} contiene comprobantes duplicados. Verificá los siguientes registros antes de continuar."
    else:
        origen_unico = None
        mensaje = f"Los archivos adjuntados del Libro y de la {origen_default} contienen comprobantes duplicados. Verificá los siguientes registros antes de continuar."
    return {
        "tipo": "comprobantes_duplicados",
        "origen": origen_unico or "Ambos",
        "titulo": "Se detectaron comprobantes duplicados",
        "mensaje": mensaje,
        "resumen": resumen,
        "detalle": items,
        "aclaracion": (
            f"La comparación contra la {origen_default} requiere una única fila por comprobante y "
            "tipo (Factura o Nota de Crédito), con los montos totalizados, tal como son "
            f"reportados por la {origen_default}/SET.\n\nSi el archivo del Libro contiene varias filas "
            "correspondientes a los ítems o líneas de detalle de un mismo comprobante "
            "(por ejemplo, por distintas tasas de IVA), consolidá los importes en un "
            "único total por comprobante antes de subir el archivo."
        ) if "Libro" in origenes_presentes else None,
    }


def insertar_lote_diagnosticando_duplicados(con: sqlite3.Connection, tabla: str, lote: list, acumulador: list | None = None, origen_default: str = "RG90") -> None:
    """Inserta un lote de (doc, tipo_doc, data) en `tabla` (libro o rg90 -- ver más abajo).
    (doc, tipo_doc) es PRIMARY KEY compuesta: el algoritmo de comparación (_comparar_par,
    engine.py) identifica un comprobante único por esa combinación, no por doc solo — un
    mismo número de comprobante puede repetirse legítimamente entre una Factura y su Nota
    de Crédito asociada (confirmado con un archivo real). Con clave (doc, tipo_doc), un
    duplicado real dentro de la misma tabla significa que el archivo trae más de una fila
    para el MISMO comprobante Y el mismo tipo (ej. líneas de detalle por ítem/tasa de IVA
    de una misma factura) — eso sí rompería la invariante 1:1 que asume _comparar_par
    (permitirlo sin tocar el join lo convertiría en un producto cartesiano, un bug de
    negocio silencioso, peor que fallar ruidosamente). Si el insert choca, se identifica
    exactamente qué comprobante(s) vienen repetidos y se corta con un 422 explícito (en
    vez del IntegrityError de SQLite, que no expone ningún valor concreto).

    Compras reusa este mismo esquema de dos columnas con "clave" (documento + RUC
    proveedor sin DV) en `doc` y `tipo_doc` siempre en "" -- su clave de comparación ya es
    un string único por sí solo, así que la PK compuesta (doc, "") se comporta como una
    clave simple sin tener que generalizar el esquema (ver app/api/compras.py).

    acumulador=None (default): comportamiento de siempre, corta ACÁ MISMO con un 422 apenas
    encuentra el primer lote con duplicados (usado por /api/validar-duplicados-libro y
    /api/validar-duplicados-rg90, que validan un solo archivo aislado). Con acumulador=[]
    (ver /api/reconcile), en cambio, NO corta -- solo agrega los duplicados de este lote a
    la lista compartida y sigue, para poder terminar de cargar libro Y rg90 completos y
    reportar los duplicados de ambos orígenes en un solo resultado consolidado (ver
    armar_error_duplicados).

    No usa ROLLBACK ni SAVEPOINT para deshacer el lote fallido a propósito: con
    journal_mode=OFF (ver más abajo, en la conexión) SQLite deja de poder revertir nada —
    confirmado, "ROLLBACK TO" simplemente no tiene efecto con el journal desactivado, lo
    que en un primer intento con esa técnica hacía que el conteo de duplicados diera de
    más (contaba también la primera fila, que había quedado insertada por el executemany
    fallido). No hace falta deshacer nada igual: el pedido entero corta con un 422 más
    abajo, y esta base SQLite temporal se borra completa en el finally del caller — dejar
    filas insertadas de un lote que terminó fallando no tiene ningún efecto persistente.
    """
    # Bug real encontrado y corregido acá: cuando executemany falla a mitad de camino por una
    # clave repetida, las filas ANTERIORES a la que falló (dentro de ESTE MISMO lote) quedan
    # insertadas en la tabla igual -- confirmado con un test aislado (sqlite3 estándar, sin
    # nada particular de este código: executemany no es atómico fila por fila, un
    # IntegrityError a mitad de una tanda no revierte las que ya se habían insertado antes de
    # esa fila). Sin este rowid_antes, el diagnóstico de "clave repetida entre lotes
    # distintos" (más abajo) confundía esas filas recién insertadas por ESTE lote con
    # duplicados genuinos de un lote ANTERIOR -- con un archivo de 100.000 filas y un solo
    # duplicado real cerca del final de un lote, esto podía reportar miles de comprobantes
    # como "repetidos" sin estarlo. rowid refleja el orden real de inserción (esta tabla solo
    # inserta, nunca borra ni hace VACUUM antes de leerse) -- todo lo insertado ANTES de
    # intentar este lote tiene rowid <= rowid_antes; lo que haya quedado insertado por el
    # propio lote fallido, no.
    rowid_antes = con.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {tabla}").fetchone()[0]
    try:
        con.executemany(f"INSERT INTO {tabla} (doc, tipo_doc, data) VALUES (?, ?, ?)", lote)
    except sqlite3.IntegrityError:
        # Se cuenta cada (doc, tipo_doc) dentro de ESTE lote (Python puro, sin tocar la
        # tabla) -- para el caso común (líneas de un mismo comprobante seguidas en el
        # archivo, cayendo en el mismo lote de INSERT_BATCH) esto ya da el conteo exacto
        # de apariciones. El caso menos común -- una clave repetida pero separada entre
        # dos lotes distintos, que acá se ve como "aparece 1 sola vez en este lote" -- se
        # detecta aparte: si esa clave ya existía en la tabla (insertada por un lote
        # anterior), también se reporta, sin conteo exacto (no vale la pena la complejidad
        # extra por un caso raro).
        vistos: dict = {}
        for doc, tipo_doc, _ in lote:
            clave = (doc, tipo_doc)
            vistos[clave] = vistos.get(clave, 0) + 1
        conteos = {clave: n for clave, n in vistos.items() if n > 1}
        claves_solo_una_vez_en_lote = [clave for clave, n in vistos.items() if n == 1]
        if claves_solo_una_vez_en_lote:
            # Bug real encontrado con un archivo de menos de 10.000 filas: antes acá se
            # armaba un WHERE con un "OR (doc = ? AND tipo_doc = ?)" por cada clave
            # candidata -- con INSERT_BATCH=5000, un lote sin duplicados internos pero que
            # sí choca contra un lote anterior podía encadenar miles de OR en una sola
            # consulta, superando el límite de profundidad de expresión de SQLite (1000):
            # "OperationalError: Expression tree is too large (maximum depth 1000)". La
            # consulta de diagnóstico (un caso ya de por sí infrecuente) terminaba
            # crasheando con un 500 genérico en vez de devolver el 422 explícito que esta
            # función existe para dar.
            #
            # Se reemplaza por una tabla temporal + JOIN, mismo patrón que ya usa este
            # endpoint para el cruce principal (libro/rg90) -- sin ninguna cadena de OR, el
            # tamaño de la consulta no depende de cuántas claves se estén diagnosticando.
            con.execute("CREATE TEMP TABLE IF NOT EXISTS tmp_claves_diag (doc TEXT, tipo_doc TEXT)")
            con.execute("DELETE FROM tmp_claves_diag")
            con.executemany("INSERT INTO tmp_claves_diag (doc, tipo_doc) VALUES (?, ?)", claves_solo_una_vez_en_lote)
            # WHERE t.rowid <= rowid_antes: solo cuenta como "ya existía" lo que estaba en la
            # tabla ANTES de intentar este lote (un lote genuinamente anterior) -- lo que el
            # propio lote fallido llegó a insertar antes de chocar (rowid > rowid_antes) no
            # cuenta, es la fila real (única) de este mismo archivo, no un duplicado.
            ya_en_lote_anterior = con.execute(
                f"SELECT t.doc, t.tipo_doc FROM {tabla} t "
                f"JOIN tmp_claves_diag c ON t.doc = c.doc AND t.tipo_doc = c.tipo_doc "
                f"WHERE t.rowid <= ?",
                (rowid_antes,),
            ).fetchall()
            con.execute("DROP TABLE tmp_claves_diag")
            for doc, tipo_doc in ya_en_lote_anterior:
                conteos[(doc, tipo_doc)] = "más de una vez, en bloques distintos del archivo"
        if not conteos:
            raise
        # conteos mezcla int (conteo exacto, duplicado dentro del mismo lote) con str
        # (duplicado entre lotes distintos, sin conteo exacto) -- se ordena poniendo los
        # conteos exactos más altos primero, dejando los aproximados al final. Mismo cálculo
        # de siempre, sin tocar nada de esto -- lo único que cambia más abajo es CÓMO se arma
        # el detail de la excepción (estructurado en vez de un párrafo armado a mano con
        # solo 5 ejemplos), para que el frontend lo muestre en una grilla ordenada y con
        # el listado COMPLETO, no truncado.
        items = sorted(conteos.items(), key=lambda kv: kv[1] if isinstance(kv[1], int) else -1, reverse=True)
        origen = "Libro" if tabla == "libro" else origen_default
        # Todos los items, sin cortar en 5 -- "cantidad" queda como número cuando se sabe
        # exacto (duplicado dentro del mismo lote) o como texto ("2 o más") cuando es entre
        # lotes distintos del archivo, igual que antes.
        detalle_items = [
            {
                "comprobante": doc,
                "tipo": tipo_doc,
                "cantidad": n if isinstance(n, int) else "2 o más",
                "origen": origen,
            }
            for (doc, tipo_doc), n in items
        ]
        if acumulador is not None:
            acumulador.extend(detalle_items)
            return
        raise HTTPException(status_code=422, detail=armar_error_duplicados(detalle_items, origen_default))


def nueva_sqlite_temporal(tabla_a: str = "libro", tabla_b: str = "rg90") -> tuple:
    """Crea la base SQLite temporal de un solo uso (mismo patrón que /api/reconcile) con
    dos tablas vacías (esquema (doc, tipo_doc, data) PK compuesta), listas para insertar.
    Devuelve (con, tmp_dir_sqlite); el caller es responsable de con.close() y
    shutil.rmtree(tmp_dir_sqlite) al terminar.

    tabla_a/tabla_b: nombres de las dos tablas -- Ventas usa "libro"/"rg90" (default, sin
    cambios), Compras pasa "libro"/"rg" explícitamente. Es un archivo nuevo por pedido, así
    que el nombre en sí no colisiona entre módulos -- se parametriza solo para que cada
    quien lea su propio esquema con nombres que tengan sentido."""
    tmp_dir_sqlite = tempfile.mkdtemp(prefix="reconcile_sqlite_")
    db_path = os.path.join(tmp_dir_sqlite, "cruce.sqlite3")
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute(f"CREATE TABLE {tabla_a} (doc TEXT NOT NULL, tipo_doc TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (doc, tipo_doc))")
    con.execute(f"CREATE TABLE {tabla_b} (doc TEXT NOT NULL, tipo_doc TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (doc, tipo_doc))")
    return con, tmp_dir_sqlite
