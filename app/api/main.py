"""
FastAPI REST API Service for SISCOM RG90 Core.
Provides endpoints for profile management, file ingestion, sequence gap detection, and RG90 reconciliation.
"""

import asyncio
import gc
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from typing import List, Optional

import ijson
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.api.auditoria import router as auditoria_router
from app.api.auth import router as auth_router
from app.api.compras import router as compras_router
from app.api.export import router as export_router
from app.api.locales import router as locales_router
from app.api.roles import router as roles_router
from app.core.audit import log_evento as _log_evento
from app.core.config import settings
from app.core.deps import get_current_user
from app.core.engine import IngestionEngine, detect_sequence_gaps, reconcile_with_rg90_iter_pares
from app.core.middleware import GzipRequestDecompressionMiddleware
from app.core.uploads import guardar_archivo_seguro
from app.db.database import get_db
from app.db.models import ArchivoProcesado, LoteProcesamiento, ResultadoRG90, Usuario

_logger = logging.getLogger("app.api.reconcile")

try:
    import ctypes
    _libc = ctypes.CDLL("libc.so.6")

    def _malloc_trim():
        """Le pide a glibc que devuelva al sistema operativo la memoria que su allocator
        tiene libre pero retenida (no la libera Python -- eso ya lo hizo el refcounting/gc
        normal; esto libera lo que malloc() se quedó "por las dudas" para la próxima
        asignación). Best-effort: si no es Linux/glibc, no hace nada."""
        _libc.malloc_trim(0)
except (OSError, AttributeError):
    def _malloc_trim():
        pass


def _insertar_lote_diagnosticando_duplicados(con: sqlite3.Connection, tabla: str, lote: list) -> None:
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
        origen = "Libro" if tabla == "libro" else "RG90"
        raise HTTPException(
            status_code=422,
            detail={
                "tipo": "comprobantes_duplicados",
                "origen": origen,
                "titulo": "Se detectaron comprobantes duplicados",
                "mensaje": f"El archivo adjuntado del {origen} contiene comprobantes duplicados. Verificá los siguientes registros antes de continuar.",
                "resumen": [{"origen": origen, "cantidad": len(items)}],
                # Todos los items, sin cortar en 5 -- "cantidad" queda como número cuando se
                # sabe exacto (duplicado dentro del mismo lote) o como texto ("2 o más")
                # cuando es entre lotes distintos del archivo, igual que antes.
                "detalle": [
                    {
                        "comprobante": doc,
                        "tipo": tipo_doc,
                        "cantidad": n if isinstance(n, int) else "2 o más",
                        "origen": origen,
                    }
                    for (doc, tipo_doc), n in items
                ],
                "aclaracion": (
                    "La comparación contra la RG90 requiere una única fila por comprobante y "
                    "tipo (Factura o Nota de Crédito), con los montos totalizados, tal como son "
                    "reportados por la RG90/SET.\n\nSi el archivo del Libro contiene varias filas "
                    "correspondientes a los ítems o líneas de detalle de un mismo comprobante "
                    "(por ejemplo, por distintas tasas de IVA), consolidá los importes en un "
                    "único total por comprobante antes de subir el archivo."
                ) if tabla == "libro" else None,
            },
        )

# /api/reconcile categoriza cada diff en una de estas etiquetas (ver
# reconcile_with_rg90_iter en engine.py) -- solo estas 6 se resumen como contador en
# "summary" (así era también antes de streaming: "Rechazada" queda en el detalle de
# "diffs" pero nunca tuvo su propio contador en el resumen, y "Salto de numeración" no la
# produce hoy esta función — se deja el mapeo igual para no cambiar ese comportamiento).
_CATEGORIA_A_CONTADOR_RECONCILE = {
    "Coincide": "coinciden",
    "No llegó a la interfaz": "no_en_rg90",
    "No en libro propio": "no_en_libro",
    "Salto de numeración": "saltos",
    "Anulada": "anuladas",
    "Diferencia de monto": "diferencia_monto",
}

PROFILES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "profiles")
engine = IngestionEngine(PROFILES_DIR)

# Starlette limita a 1MB cada "parte" de un multipart/form-data por defecto — incluidos los
# campos de texto, no solo los archivos. pos_data_json (el libro propio, serializado para
# mandarlo a /reconcile) supera ese límite con libros de varios miles de comprobantes (ej.
# 6.246 filas de compras ya pesan ~4MB como JSON; un libro de ventas grande, bastante más).
# Se sube el límite acá en vez de declarar pos_data_json como Form(...) directo, porque
# FastAPI no expone ese parámetro a través del descriptor Form().
FORM_MAX_PART_SIZE = 150 * 1024 * 1024

# Mensaje único para "el archivo no se pudo ni abrir como Excel" (PDF renombrado, archivo
# corrupto, etc.) -- distinto del mensaje de "no coincide con el perfil esperado" (ese sí
# es específico por perfil/columnas). Usado tanto en /api/ingest como en /api/reconcile,
# ver auditoria/14-go-live-readiness.md, Fase 1, hallazgo crítico.
MSG_ARCHIVO_NO_VALIDO = "El archivo subido no es un Excel válido o está corrupto."

# Auto-detección del sistema de ventas en /api/ingest: el Paso 1 del frontend ya no pide
# elegir el sistema antes de adjuntar (mismo criterio que /api/compras/ingest, que nunca lo
# pidió) — se prueba cada perfil, en este orden, hasta encontrar el que matchea la firma de
# columnas del archivo.
PERFILES_VENTAS_AUTO = ["aloha", "hiopos_ventas", "universal"]
PERFIL_VENTAS_LABEL = {"aloha": "Aloha", "hiopos_ventas": "Hiopos", "universal": "Universal"}
# El frontend (SYSTEMS_META) usa "hiopos" como key, no "hiopos_ventas" (ese es el profile_id
# interno del motor) — se traduce acá, en el borde de la API, para no tener que sincronizar
# ese nombre en dos lugares.
PERFIL_VENTAS_FRONTEND_KEY = {"aloha": "aloha", "hiopos_ventas": "hiopos", "universal": "universal"}

app = FastAPI(
    title="SISCOM RG90 Core API",
    description="Motor de Ingesta, Conciliación y Reglas Fiscales para Libros de Venta vs RG90 (SET Paraguay)",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Descomprime pedidos con Content-Encoding: gzip antes de que lleguen a cualquier endpoint
# -- ver el porqué en app/core/middleware.py. allow_headers=["*"] de arriba ya deja pasar
# Content-Encoding sin nada extra que configurar del lado de CORS.
app.add_middleware(GzipRequestDecompressionMiddleware)

app.include_router(auth_router)
app.include_router(compras_router)
app.include_router(export_router)
app.include_router(locales_router)
app.include_router(roles_router)
app.include_router(auditoria_router)


@app.get("/api/health")
def health_check():
    return {"status": "ok", "service": "corvis-conciliation-core", "profiles_loaded": len(engine.profiles)}


@app.get("/api/profiles")
def get_profiles(usuario: Usuario = Depends(get_current_user)):
    return list(engine.profiles.values())


@app.post("/api/ingest")
async def ingest_files(
    request: Request,
    files: List[UploadFile] = File(...),
    system_key: str = Form("auto"),
    local_name: Optional[str] = Form("Local General"),
    tipo_libro: str = Form("venta"),
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    # "auto" (el Paso 1 ya no tiene selector): se prueba cada perfil de venta conocido, por
    # archivo, hasta encontrar el que matchea. Un system_key explícito (aloha/hiopos/
    # universal) se sigue aceptando por compatibilidad, pero el frontend actual ya no lo
    # manda.
    auto_detectar = system_key.lower() == "auto"
    profile_id_fijo = None
    if not auto_detectar:
        profile_id_fijo = "aloha" if "aloha" in system_key.lower() else ("hiopos_ventas" if "hiopos" in system_key.lower() else "universal")

    all_rows = []
    all_cortes = []
    perfil_por_archivo: dict = {}
    loop = asyncio.get_event_loop()

    with tempfile.TemporaryDirectory() as tmp_dir:
        for file in files:
            tmp_path = await guardar_archivo_seguro(file, tmp_dir)

            # engine.ingest_file (pandas/xlrd) es trabajo sincrónico y puede tardar varios
            # segundos por archivo — se corre en un thread aparte para no bloquear el event
            # loop mientras se procesa un lote con varios reportes (si no, el backend queda
            # "colgado" para cualquier otro pedido, incluidos los health checks, hasta
            # terminar todo el lote).
            if auto_detectar:
                rows = cortes = None
                profile_id = None
                errores = []
                for candidato in PERFILES_VENTAS_AUTO:
                    try:
                        rows, cortes = await loop.run_in_executor(None, engine.ingest_file, tmp_path, candidato, local_name)
                        profile_id = candidato
                        break
                    except ValueError as e:
                        errores.append(f"{PERFIL_VENTAS_LABEL.get(candidato, candidato)}: {e}")
                    except Exception:
                        # El archivo ni siquiera se pudo ABRIR como Excel (ej. un PDF
                        # renombrado a .xlsx, un archivo corrupto) -- distinto de "no
                        # coincide con ningún perfil" (ValueError, arriba): acá no tiene
                        # sentido seguir probando los demás perfiles, todos van a fallar
                        # con el mismo error de formato. Antes esto se escapaba sin
                        # atrapar (python_calamine.CalamineError no es ValueError) y
                        # terminaba en un 500 genérico -- ver hallazgo crítico de
                        # auditoria/14-go-live-readiness.md, Fase 1.
                        raise HTTPException(status_code=422, detail=MSG_ARCHIVO_NO_VALIDO)
                if profile_id is None:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"El archivo '{file.filename}' no coincide con ningún formato de ventas conocido "
                            f"(Aloha, Hiopos, Universal). {' | '.join(errores)}"
                        ),
                    )
            else:
                profile_id = profile_id_fijo
                try:
                    rows, cortes = await loop.run_in_executor(None, engine.ingest_file, tmp_path, profile_id, local_name)
                except ValueError as e:
                    # El archivo no corresponde al sistema elegido (firma no encontrada) —
                    # se bloquea acá, antes de crear ningún lote, para que el Paso 1 no avance.
                    raise HTTPException(status_code=422, detail=str(e))
                except Exception:
                    # Mismo caso que arriba (rama auto_detectar): el archivo no se pudo
                    # abrir como Excel en absoluto.
                    raise HTTPException(status_code=422, detail=MSG_ARCHIVO_NO_VALIDO)

            perfil_por_archivo[file.filename] = profile_id
            all_rows.extend(rows)
            for c in cortes:
                all_cortes.append({**c, "archivo": file.filename, "local": local_name})

    gaps = detect_sequence_gaps(all_rows)

    # "mixto" (ver el comentario del campo en models.py) cuando el lote combina archivos de
    # más de un sistema — posible ahora que ya no hace falta agruparlos de antemano.
    perfiles_distintos = set(perfil_por_archivo.values())
    sistema_origen = next(iter(perfiles_distintos)) if len(perfiles_distintos) == 1 else "mixto"

    lote = LoteProcesamiento(
        usuario_id=usuario.id,
        tipo_libro=tipo_libro,
        sistema_origen=sistema_origen,
        cantidad_comprobantes=len(all_rows),
        cantidad_saltos=len(gaps),
        estado="cargado",
    )
    db.add(lote)
    db.flush()  # obtiene lote.id sin cerrar la transacción

    for file in files:
        db.add(ArchivoProcesado(lote_id=lote.id, nombre_archivo=file.filename, perfil=perfil_por_archivo.get(file.filename, "")))
    db.commit()
    db.refresh(lote)

    _log_evento(db, usuario.id, "carga_archivo", request, lote_id=lote.id, detalle={"archivos": [f.filename for f in files], "sistema": sistema_origen})
    _log_evento(db, usuario.id, "conversion", request, lote_id=lote.id, detalle={"cantidad_comprobantes": len(all_rows), "cantidad_saltos": len(gaps)})

    return {
        "success": True,
        "lote_id": lote.id,
        "total_rows": len(all_rows),
        "gaps_count": len(gaps),
        "rows": all_rows,
        "gaps": gaps,
        "cortes": all_cortes,
        # Para que el Paso 1 pueda mostrar, por archivo adjuntado, qué sistema se detectó
        # (campo de solo lectura, se completa solo después de analizar).
        "archivos_detectados": [
            {
                "archivo": fn,
                "sistema_key": PERFIL_VENTAS_FRONTEND_KEY.get(pid, pid),
                "sistema_label": PERFIL_VENTAS_LABEL.get(pid, pid),
            }
            for fn, pid in perfil_por_archivo.items()
        ],
    }


async def _cargar_libro_en_sqlite(con: sqlite3.Connection, pos_data_file: StarletteUploadFile) -> int:
    """Carga el libro propio (streaming vía ijson) en la tabla `libro` de una base SQLite
    temporal, diagnosticando duplicados lote a lote (ver
    _insertar_lote_diagnosticando_duplicados). Extraída de /api/reconcile SIN CAMBIOS de
    comportamiento (mismo código, ahora reutilizable) para que /api/validar-duplicados-libro
    pueda validar el libro apenas se adjunta, sin esperar a la RG90 ni a la comparación."""
    try:
        INSERT_BATCH = 5000
        cantidad_comprobantes = 0
        lote_insert: list[tuple] = []
        pos_data_file.file.seek(0)
        # use_float=True: por defecto ijson devuelve los números como decimal.Decimal en
        # vez de float como hacía json.loads() -- sin esto, _monto_diff() revienta con
        # TypeError al restar un Decimal (de acá) contra un float (del lado RG90).
        for row in ijson.items(pos_data_file.file, "item", use_float=True):
            lote_insert.append((row["doc"], row.get("tipo_doc", ""), json.dumps(row)))
            cantidad_comprobantes += 1
            if len(lote_insert) >= INSERT_BATCH:
                _insertar_lote_diagnosticando_duplicados(con, "libro", lote_insert)
                lote_insert.clear()
        if lote_insert:
            _insertar_lote_diagnosticando_duplicados(con, "libro", lote_insert)
            lote_insert.clear()
        return cantidad_comprobantes
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=422, detail=f"El libro enviado para comparar no tiene el formato esperado: {e}")
    finally:
        await pos_data_file.close()


async def _cargar_rg90_en_sqlite(con: sqlite3.Connection, rg90_files: list) -> tuple:
    """Carga la(s) RG90 (streaming vía engine.ingest_file_streaming) en la tabla `rg90` de
    una base SQLite temporal, diagnosticando duplicados lote a lote. Extraída de
    /api/reconcile SIN CAMBIOS de comportamiento -- ver docstring de
    _cargar_libro_en_sqlite, mismo motivo (reutilizarla desde
    /api/validar-duplicados-rg90)."""
    loop = asyncio.get_event_loop()

    def _leer_e_insertar_rg90(tmp_path: str) -> tuple:
        """Corre en un thread del executor -- ver comentario original en /api/reconcile
        (docstring previo a esta extracción, sin cambios)."""
        total = 0
        gaps_input = []
        for i, chunk in enumerate(engine.ingest_file_streaming(tmp_path, "rg90_set", "RG90 SET"), start=1):
            _insertar_lote_diagnosticando_duplicados(con, "rg90", [(r["doc"], r.get("tipo_doc", ""), json.dumps(r)) for r in chunk])
            gaps_input.extend({"doc": r["doc"], "tipo_doc": r["tipo_doc"], "local": r["local"], "sistema": r["sistema"]} for r in chunk)
            total += len(chunk)
            if i % 10 == 0:
                gc.collect()
                _malloc_trim()
        gc.collect()
        _malloc_trim()
        return total, gaps_input

    rg90_total_rows = 0
    rg90_gaps_input: list = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        for rg90_file in rg90_files:
            tmp_path = await guardar_archivo_seguro(rg90_file, tmp_dir)
            try:
                total_archivo, gaps_input_archivo = await loop.run_in_executor(None, _leer_e_insertar_rg90, tmp_path)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=f"Error al procesar el archivo RG90 '{rg90_file.filename}': {e}")
            except HTTPException:
                # Bug real encontrado al agregar /api/validar-duplicados-rg90: cuando
                # _insertar_lote_diagnosticando_duplicados detecta un duplicado del lado
                # RG90, la HTTPException(422, detail={...}) estructurada que arma viaja a
                # través de run_in_executor y llegaba hasta acá -- sin este except, caía en
                # el "except Exception" genérico de abajo (HTTPException es Exception) y se
                # REEMPLAZABA por MSG_ARCHIVO_NO_VALIDO, perdiendo la grilla de duplicados
                # (el usuario veía "El archivo subido no es un Excel válido o está
                # corrupto." en vez del detalle real). Ya pasaba también en /api/reconcile
                # antes de esta extracción -- no es un bug nuevo de esta refactorización,
                # solo recién se hizo visible al ejercitar este camino en aislamiento.
                raise
            except Exception:
                raise HTTPException(status_code=422, detail=MSG_ARCHIVO_NO_VALIDO)
            rg90_total_rows += total_archivo
            rg90_gaps_input.extend(gaps_input_archivo)
    return rg90_total_rows, rg90_gaps_input


def _nueva_sqlite_temporal() -> tuple:
    """Crea la base SQLite temporal de un solo uso (mismo patrón que /api/reconcile) con
    las tablas `libro`/`rg90` vacías, lista para insertar. Devuelve (con, tmp_dir_sqlite);
    el caller es responsable de con.close() y shutil.rmtree(tmp_dir_sqlite) al terminar."""
    tmp_dir_sqlite = tempfile.mkdtemp(prefix="reconcile_sqlite_")
    db_path = os.path.join(tmp_dir_sqlite, "cruce.sqlite3")
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("CREATE TABLE libro (doc TEXT NOT NULL, tipo_doc TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (doc, tipo_doc))")
    con.execute("CREATE TABLE rg90 (doc TEXT NOT NULL, tipo_doc TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (doc, tipo_doc))")
    return con, tmp_dir_sqlite


@app.post("/api/validar-duplicados-libro")
async def validar_duplicados_libro(
    request: Request,
    usuario: Usuario = Depends(get_current_user),
):
    """Valida el libro propio en busca de comprobantes duplicados, de forma independiente
    a la RG90 y a la comparación -- pensado para correr apenas se adjunta el archivo en el
    Paso 1 del frontend (ver doConvert en App.tsx), para que el usuario vea el error de
    inmediato en vez de recién al ejecutar la comparación en el Paso 3.

    Reutiliza _cargar_libro_en_sqlite (idéntica a la que usa /api/reconcile) sin alterar
    ninguna regla de detección de duplicados ni de comparación -- esta ruta solo adelanta
    EN EL TIEMPO la misma validación que /api/reconcile ya hacía, no agrega ni cambia
    ninguna regla de negocio."""
    form = await request.form(max_part_size=FORM_MAX_PART_SIZE)
    pos_data_file = form.get("pos_data_json")
    if pos_data_file is None or not isinstance(pos_data_file, StarletteUploadFile):
        raise HTTPException(status_code=422, detail="Falta el libro a validar (pos_data_json), o no llegó como archivo.")

    con, tmp_dir_sqlite = _nueva_sqlite_temporal()
    try:
        await _cargar_libro_en_sqlite(con, pos_data_file)
    finally:
        con.close()
        shutil.rmtree(tmp_dir_sqlite, ignore_errors=True)
    return {"success": True}


@app.post("/api/validar-duplicados-rg90")
async def validar_duplicados_rg90(
    request: Request,
    usuario: Usuario = Depends(get_current_user),
):
    """Valida la(s) RG90 en busca de comprobantes duplicados, de forma independiente al
    libro propio y a la comparación -- pensado para correr apenas se adjunta el archivo en
    el Paso 3 del frontend (ver handleRg90FileUpload en App.tsx). Ver docstring de
    validar_duplicados_libro, mismo criterio (misma validación de siempre, solo que
    adelantada en el tiempo)."""
    form = await request.form(max_part_size=FORM_MAX_PART_SIZE)
    rg90_files = form.getlist("rg90_files")
    if not rg90_files:
        raise HTTPException(status_code=422, detail="Faltan los archivos de RG90 a validar.")

    con, tmp_dir_sqlite = _nueva_sqlite_temporal()
    try:
        await _cargar_rg90_en_sqlite(con, rg90_files)
    finally:
        con.close()
        shutil.rmtree(tmp_dir_sqlite, ignore_errors=True)
    return {"success": True}


@app.post("/api/reconcile")
async def reconcile(
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    """Compara el libro propio contra la RG90. Acepta uno o dos archivos de RG90 (venta y
    nota de crédito), que se consolidan antes de comparar — ver Minuta 3: la RG90 se
    descarga en reportes separados por tipo de comprobante.

    REFACTOR POR OOM, segunda vuelta (ver auditoria/12-certificacion-salud-sistema.md,
    "Hallazgo crítico" y su primera "Actualización"): la primera vuelta (streaming con
    ijson + generador + StreamingResponse) eliminó el OOM-kill (0 caídas en varias
    corridas de 200.000 filas, contra 3/3 antes) pero el pico de RAM solo bajó de 1,83GB a
    ~1,49GB — quedaba lejos del objetivo. La razón: el JOIN en sí (`libro_map`/`rg90_map`
    completos y simultáneos en Python, para poder hacer `.get(doc)` de cada lado) es un
    piso de memoria que ningún streaming de la SALIDA podía bajar.

    Esta vuelta saca el CRUCE (el emparejamiento por `doc`) a una base SQLite temporal, de
    un solo uso por pedido, en disco (no `:memory:` — un SQLite en memoria seguiría
    contando como RSS del proceso, no resolvería nada). SQLite solo empareja `doc` con
    `doc`; la lógica de negocio (qué es una diferencia, de qué tipo, cómo se arma cada
    campo) sigue siendo Python puro, sin cambios — ver `_comparar_par` en engine.py, la
    misma función que ya usaban las dos vueltas anteriores del refactor.
    """
    form = await request.form(max_part_size=FORM_MAX_PART_SIZE)
    rg90_files = form.getlist("rg90_files")
    pos_data_file = form.get("pos_data_json")
    if not rg90_files or pos_data_file is None:
        raise HTTPException(status_code=422, detail="Faltan los archivos de RG90 o el libro a comparar.")
    # pos_data_json tiene que llegar como ARCHIVO (Blob), no como campo de texto plano --
    # FormData.get() de Starlette devuelve un str si el campo llegó como texto (ej. un
    # frontend desplegado ANTES de este cambio, que todavía hace
    # formData.append('pos_data_json', JSON.stringify(...)) sin envolverlo en un Blob).
    # Sin este chequeo, ese caso fallaba más abajo con un AttributeError confuso
    # ('str' object has no attribute 'close', en el finally que cierra el UploadFile) en
    # vez de decir con claridad qué es lo que realmente está mal.
    #
    # OJO: se chequea contra starlette.datastructures.UploadFile, NO fastapi.UploadFile
    # (importado arriba, usado en la firma de otros endpoints que reciben File(...) por
    # inyección de dependencias) -- fastapi.UploadFile es una SUBCLASE de la de Starlette,
    # y acá el archivo se lee directo de request.form() (sin pasar por esa inyección), que
    # siempre entrega instancias de la clase BASE de Starlette. isinstance() contra la
    # subclase de FastAPI da falso incluso con un archivo real (bug propio, encontrado y
    # corregido en la misma corrida en que se agregó este chequeo).
    if not isinstance(pos_data_file, StarletteUploadFile):
        raise HTTPException(
            status_code=422,
            detail=(
                "El libro a comparar (pos_data_json) llegó como texto plano, no como archivo. "
                "Esto pasa si el frontend desplegado es anterior al commit que lo manda como Blob "
                "(services/api.ts, reconcileApi) — verificá que el build de siscom-rg90 en /var/www/siscom "
                "incluya ese cambio."
            ),
        )
    lote_id_raw = form.get("lote_id")
    lote_id = int(lote_id_raw) if lote_id_raw else None

    # Base SQLite temporal, un directorio por pedido, borrado siempre en el finally de
    # _generar_respuesta() más abajo (o acá mismo si algo falla antes de llegar a esa
    # parte). Nunca ':memory:' -- eso seguiría siendo RAM del proceso, no bajaría nada.
    con, tmp_dir_sqlite = _nueva_sqlite_temporal()

    try:
        # Carga en streaming: ijson va entregando filas del archivo (nunca el documento
        # completo en memoria) y se insertan en lotes de INSERT_BATCH filas por
        # executemany -- nunca existe un `libro_map`/lista de 200.000 dicts en Python al
        # mismo tiempo, solo el lote chico que se está por insertar. Ver
        # _cargar_libro_en_sqlite (extraída de acá, sin cambios, para poder reutilizarla
        # también desde /api/validar-duplicados-libro).
        cantidad_comprobantes = await _cargar_libro_en_sqlite(con, pos_data_file)

        # Lectura de la(s) RG90 -- SIN CAMBIOS, sigue siendo engine.ingest_file tal cual
        # (motor de parseo de Excel), sin cambiar ni una línea de ese archivo. En vez de
        # ingest_file() (que devuelve la RG90 completa como una lista, el remanente de RSS
        # medido en la vuelta anterior de este refactor), se usa ingest_file_streaming()
        # (nueva, ver engine.py): lee el .xlsx con openpyxl en modo streaming real (nunca
        # la hoja completa en memoria) y aplica la MISMA lógica vectorizada por bloques.
        # Cada bloque se inserta en SQLite y se descarta antes de leer el siguiente --
        # nunca existe una lista de 200.000 filas completas de la RG90 en memoria. Ver
        # _cargar_rg90_en_sqlite (extraída de acá, sin cambios, mismo motivo de arriba).
        rg90_total_rows, rg90_gaps_input = await _cargar_rg90_en_sqlite(con, rg90_files)

        con.commit()
        # Saltos de numeración DENTRO de la RG90 misma (no contra el libro propio) — mismo
        # detector que ya usa /api/ingest sobre el libro propio, para el Paso 3 (Adjuntar
        # RG90). Sin cambios en detect_sequence_gaps -- solo recibe una versión más chica
        # de cada fila (ver comentario de gaps_input arriba).
        rg90_gaps = detect_sequence_gaps(rg90_gaps_input)
        del rg90_gaps_input
        gc.collect()
    except HTTPException:
        con.close()
        shutil.rmtree(tmp_dir_sqlite, ignore_errors=True)
        raise
    except Exception as e:
        con.close()
        shutil.rmtree(tmp_dir_sqlite, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Error inesperado al leer los archivos para comparar ({type(e).__name__}): {e}")

    nombres_rg90 = ", ".join(f.filename for f in rg90_files)

    def _generar_respuesta():
        # Todo lo que sigue corre DESPUÉS de que ya se mandó el primer byte de la
        # respuesta — un error acá ya no puede convertirse en un HTTPException limpio (los
        # headers ya salieron). Se loguea y se corta el stream; el cliente recibe un JSON
        # truncado, que falla al parsear — mismo resultado de cara al usuario que el
        # ERR_EMPTY_RESPONSE de antes, pero con el motivo real en el log del servidor en
        # vez de un worker muerto sin rastro.
        try:
            counts = {"coinciden": 0, "no_en_rg90": 0, "no_en_libro": 0, "saltos": 0, "anuladas": 0, "diferencia_monto": 0}

            # Yield-ear pieza por pieza (una por fila) funciona para el objetivo de memoria,
            # pero cada yield de un StreamingResponse termina en un write() de socket propio
            # — con 200.000 yields eso midió ~4 minutos por el overhead de red/framing, no
            # por CPU ni por memoria. Se junta en un buffer de texto y se yield-ea recién
            # cuando junta ~64KB (CHUNK_BYTES): sigue sin retener las 200.000 filas
            # completas (el buffer nunca crece más allá de ese tamaño), pero baja el número
            # de writes de ~200.000 a unos pocos miles.
            CHUNK_BYTES = 65536
            buffer: list[str] = []
            buffer_len = 0

            def _push(pieza: str):
                nonlocal buffer_len
                buffer.append(pieza)
                buffer_len += len(pieza)

            def _flush_si_corresponde():
                nonlocal buffer, buffer_len
                if buffer_len >= CHUNK_BYTES:
                    resultado = "".join(buffer)
                    buffer = []
                    buffer_len = 0
                    return resultado
                return None

            _push('{"success": true, "rg90_total_rows": %d, "rg90_rows": [' % rg90_total_rows)
            # ORDER BY rowid preserva el orden de inserción (= orden original del archivo
            # RG90) -- "doc" no está garantizado alfabético/cronológico, y el Paso 3 del
            # frontend espera ver la lista tal como venía del archivo.
            for idx, (data,) in enumerate(con.execute("SELECT data FROM rg90 ORDER BY rowid")):
                if idx:
                    _push(",")
                _push(data)  # ya es JSON serializado (se guardó con json.dumps al insertar)
                chunk = _flush_si_corresponde()
                if chunk is not None:
                    yield chunk
            _push('], "rg90_gaps": ')
            _push(json.dumps(rg90_gaps))
            _push(', "diffs": [')

            # EL CRUCE: FULL OUTER JOIN por (doc, tipo_doc) -- no doc solo, ver el
            # docstring de _insertar_lote_diagnosticando_duplicados: un mismo número de
            # comprobante puede repetirse legítimamente entre una Factura y su Nota de
            # Crédito asociada. Simulado con LEFT JOIN + UNION ALL porque esta instancia
            # de SQLite (3.37) es anterior a la 3.39, que agregó soporte nativo para FULL
            # OUTER JOIN. UNION ALL (no UNION) porque (doc, tipo_doc) ya es PRIMARY KEY
            # compuesta en ambas tablas -- no hay riesgo de duplicados, y evita el costo
            # extra de deduplicar que haría un UNION simple. SQLite solo empareja filas acá
            # adentro: la comparación de montos/estados sigue pasando en Python, fila por
            # fila, más abajo (_comparar_par en engine.py) -- nada de lógica de negocio se
            # movió a SQL.
            cursor_join = con.execute("""
                SELECT l.doc, l.tipo_doc, l.data, r.data
                FROM libro l LEFT JOIN rg90 r ON l.doc = r.doc AND l.tipo_doc = r.tipo_doc
                UNION ALL
                SELECT r.doc, r.tipo_doc, NULL, r.data
                FROM rg90 r LEFT JOIN libro l ON l.doc = r.doc AND l.tipo_doc = r.tipo_doc
                WHERE l.doc IS NULL
                ORDER BY 1, 2
            """)  # ORDER BY 1, 2 (posición, no nombre): SQLite no siempre resuelve el
            # nombre de columna de un UNION ALL de forma confiable ("1st ORDER BY term
            # does not match any column in the result set" con ORDER BY doc) -- por
            # posición funciona siempre. Por ambas columnas (doc, tipo_doc) para que el
            # orden entre, por ejemplo, la Factura y la Nota de Crédito de un mismo doc
            # sea determinístico.

            def _pares_desde_sqlite():
                for doc, _tipo_doc, libro_json, rg90_json in cursor_join:
                    pos_rec = json.loads(libro_json) if libro_json is not None else None
                    rg_rec = json.loads(rg90_json) if rg90_json is not None else None
                    yield doc, pos_rec, rg_rec

            total_diffs = 0
            for total_diffs, d in enumerate(reconcile_with_rg90_iter_pares(_pares_desde_sqlite()), start=1):
                if total_diffs > 1:
                    _push(",")
                _push(json.dumps(d))
                clave = _CATEGORIA_A_CONTADOR_RECONCILE.get(d["diferencia"])
                if clave:
                    counts[clave] += 1
                chunk = _flush_si_corresponde()
                if chunk is not None:
                    yield chunk
                if total_diffs % 20000 == 0:
                    gc.collect()
                    _malloc_trim()

            _push('], "summary": ')
            _push(json.dumps(counts))
            if buffer:
                yield "".join(buffer)
                buffer = []
                buffer_len = 0

            # El bookkeeping en base (ResultadoRG90 + estado del lote + evento de
            # auditoría) se hace acá, al final, porque recién ahora se conocen los
            # conteos finales -- antes del refactor se hacía ANTES de devolver cualquier
            # byte de la respuesta, así que quedaba garantizado incluso si el envío de la
            # respuesta fallaba después; ahora, si el cliente corta la conexión a mitad
            # del streaming, este bloque no llega a correr y no queda ResultadoRG90 ni
            # evento de auditoría para ese intento -- trade-off aceptado a cambio de no
            # tener que mantener 200.000 filas enriquecidas en memoria para poder escribir
            # esto antes de empezar a transmitir.
            lote = db.get(LoteProcesamiento, lote_id) if lote_id else None
            if lote is None:
                # Sin lote de origen (llamada directa a /reconcile): se crea uno mínimo para trazabilidad.
                lote = LoteProcesamiento(
                    usuario_id=usuario.id,
                    tipo_libro="venta",
                    sistema_origen="rg90",
                    cantidad_comprobantes=cantidad_comprobantes,
                    estado="comparado_rg90",
                )
                db.add(lote)
                db.flush()
            else:
                lote.estado = "comparado_rg90"

            db.add(ResultadoRG90(
                lote_id=lote.id,
                archivo_rg90_nombre=nombres_rg90,
                total_coinciden=counts["coinciden"],
                total_no_en_rg90=counts["no_en_rg90"],
                total_no_en_libro=counts["no_en_libro"],
                total_saltos=counts["saltos"],
            ))
            db.commit()

            _log_evento(db, usuario.id, "comparacion_rg90", request, lote_id=lote.id, detalle={"archivos_rg90": [f.filename for f in rg90_files], "total_diferencias": total_diffs})

            yield ', "lote_id": %d}' % lote.id
        except Exception:
            _logger.exception("Error durante el streaming de /api/reconcile (respuesta quedó truncada para el cliente)")
            raise
        finally:
            # Limpieza obligatoria de la base SQLite temporal -- se cierra la conexión y se
            # borra el directorio entero (el .sqlite3 y cualquier -journal/-wal/-shm que
            # SQLite haya llegado a crear), pase lo que pase.
            con.close()
            shutil.rmtree(tmp_dir_sqlite, ignore_errors=True)
            gc.collect()

    return StreamingResponse(_generar_respuesta(), media_type="application/json")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.api.main:app", host="0.0.0.0", port=8000, reload=True)
