"""Router del Libro de Compras (Minuta 5): carga del export del sistema y comparación
contra la RG. Mismo patrón que /api/ingest y /api/reconcile (ventas), pero usando
ComprasEngine — ver app/core/compras_engine.py sobre por qué compras necesita su propio
motor en vez de reusar el de ventas."""

import asyncio
import gc
import json
import os
import shutil
import tempfile
from typing import List, Optional

import ijson
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.core.audit import log_evento as _log_evento
from app.core.compras_engine import ComprasEngine, reconcile_compras_with_rg_iter_pares
from app.core.concurrencia import adquirir_operacion_pesada, liberar_operacion_pesada
from app.core.engine import detect_sequence_gaps
from app.core.deps import get_current_user
from app.core.sqlite_cruce import armar_error_duplicados, insertar_lote_diagnosticando_duplicados, nueva_sqlite_temporal
from app.core.uploads import guardar_archivo_seguro
from app.db.database import get_db
from app.db.models import ArchivoProcesado, LoteProcesamiento, ResultadoRG90, Usuario

PROFILES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "profiles")
engine = ComprasEngine(PROFILES_DIR)

# Mismo mensaje/criterio que MSG_ARCHIVO_NO_VALIDO en app/api/main.py (no se importa desde
# ahí para no crear un import circular -- main.py ya importa este router).
MSG_ARCHIVO_NO_VALIDO = "El archivo subido no es un Excel válido o está corrupto."

# Ver la misma constante en app/api/main.py: Starlette limita a 1MB cada "parte" de un
# multipart/form-data por defecto (también los campos de texto, no solo los archivos), y
# pos_data_json puede superar eso ampliamente con libros de miles de comprobantes.
FORM_MAX_PART_SIZE = 80 * 1024 * 1024

router = APIRouter(prefix="/api/compras", tags=["compras"])

# Mismo criterio que _CATEGORIA_A_CONTADOR_RECONCILE en app/api/main.py (Ventas) -- Compras
# no tiene "Salto de numeración" ni "Anulada" como categorías propias del cruce (ver
# docstring de ComprasEngine: sin control de correlatividad contra el libro, sin campo de
# estado real).
_CATEGORIA_A_CONTADOR_RECONCILE_COMPRAS = {
    "Coincide": "coinciden",
    "No llegó a la interfaz": "no_en_rg",
    "No existe en el libro": "no_en_libro",
    "Diferencia de importe": "diferencia_importe",
    "Diferencias en tasas": "diferencias_tasas",
}


async def _cargar_libro_compras_en_sqlite(con, pos_data_file: StarletteUploadFile, acumulador: list | None = None) -> int:
    """Carga el libro de compras (streaming vía ijson) en la tabla `libro` de una base
    SQLite temporal -- mismo patrón que _cargar_libro_en_sqlite en app/api/main.py
    (Ventas), incluido correr en un thread del executor (ver el fix de esta misma sesión
    al event loop de Ventas: ese mismo bloqueo aplica acá igual).

    Compras no tiene clave compuesta (doc, tipo_doc) como Ventas -- su clave de
    comparación ya es un string único (`clave` = documento + RUC proveedor sin DV, ver
    compras_engine.py). Se reusa igual el esquema de dos columnas de
    insertar_lote_diagnosticando_duplicados guardando `clave` en la columna `doc` y
    dejando `tipo_doc` siempre en "" -- la PK compuesta (doc, "") se comporta como una
    clave simple sin tener que generalizar esa función (ver app/core/sqlite_cruce.py)."""
    loop = asyncio.get_event_loop()

    def _leer_e_insertar_libro() -> int:
        INSERT_BATCH = 5000
        cantidad_comprobantes = 0
        lote_insert: list[tuple] = []
        for row in ijson.items(pos_data_file.file, "item", use_float=True):
            lote_insert.append((row["clave"], "", json.dumps(row)))
            cantidad_comprobantes += 1
            if len(lote_insert) >= INSERT_BATCH:
                insertar_lote_diagnosticando_duplicados(con, "libro", lote_insert, acumulador, origen_default="RG")
                lote_insert.clear()
        if lote_insert:
            insertar_lote_diagnosticando_duplicados(con, "libro", lote_insert, acumulador, origen_default="RG")
            lote_insert.clear()
        return cantidad_comprobantes

    try:
        pos_data_file.file.seek(0)
        return await loop.run_in_executor(None, _leer_e_insertar_libro)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=422, detail=f"El libro de compras enviado para comparar no tiene el formato esperado: {e}")
    finally:
        await pos_data_file.close()


async def _cargar_rg_compras_en_sqlite(con, rg_files: list, acumulador: list | None = None) -> list:
    """Lee la(s) RG de compras (engine.ingest_file, sin cambios -- ya corre en un thread
    del executor, ver más abajo) e inserta cada fila en la tabla `rg` de la base SQLite
    temporal. Devuelve la lista completa de filas (todavía hace falta una sola vez, para
    detect_sequence_gaps justo después de esta llamada en reconcile_compras) -- se
    descarta con `del` inmediatamente después de usarla, antes de armar la respuesta, para
    no mantenerla en memoria junto con el resto del cruce.

    A diferencia de Ventas (ingest_file_streaming, lee el .xlsx en bloques), acá la RG
    completa sí pasa un instante por una lista Python -- ComprasEngine no tiene todavía el
    equivalente a ingest_file_streaming (ver Pilar 2 de la auditoría del 02/10, Fase 2
    separada, no es parte de este cambio). Los volúmenes reales de Compras vistos esta
    sesión son muy por debajo del umbral que requirió ese refactor en Ventas."""
    loop = asyncio.get_event_loop()
    rg_rows: list = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        for rg_file in rg_files:
            tmp_path = await guardar_archivo_seguro(rg_file, tmp_dir)
            try:
                file_rows = await loop.run_in_executor(None, engine.ingest_file, tmp_path, "rg_compras", "RG")
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
            except Exception:
                raise HTTPException(status_code=422, detail=MSG_ARCHIVO_NO_VALIDO)
            rg_rows.extend(file_rows)

    lote_insert = [(r["clave"], "", json.dumps(r)) for r in rg_rows]
    INSERT_BATCH = 5000
    for i in range(0, len(lote_insert), INSERT_BATCH):
        insertar_lote_diagnosticando_duplicados(con, "rg", lote_insert[i:i + INSERT_BATCH], acumulador, origen_default="RG")
    return rg_rows


@router.post("/ingest")
async def ingest_compras(
    request: Request,
    files: List[UploadFile] = File(...),
    local_name: Optional[str] = Form("Local General"),
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    """Límite de concurrencia (Plan de Acción del 02/10, Pilar 5): mismo criterio que
    /api/ingest (Ventas) -- sin streaming de salida acá, así que adquirir/liberar
    alrededor de la función entera alcanza. El cuerpo real está en
    _ingest_compras_impl, sin cambios."""
    await adquirir_operacion_pesada()
    try:
        return await _ingest_compras_impl(request, files, local_name, db, usuario)
    finally:
        liberar_operacion_pesada()


async def _ingest_compras_impl(
    request: Request,
    files: List[UploadFile],
    local_name: Optional[str],
    db: Session,
    usuario: Usuario,
):
    all_rows = []
    loop = asyncio.get_event_loop()
    profile_usado = "compras_sistema"

    with tempfile.TemporaryDirectory() as tmp_dir:
        for file in files:
            tmp_path = await guardar_archivo_seguro(file, tmp_dir)
            # Auto-detección de formato: primero se intenta el export del sistema
            # habitual (compras_sistema); si la firma de columnas no coincide, se
            # reintenta con el Formato Universal (Minuta) antes de fallar.
            try:
                rows = await loop.run_in_executor(None, engine.ingest_file, tmp_path, "compras_sistema", local_name)
            except ValueError as e_sistema:
                try:
                    rows = await loop.run_in_executor(None, engine.ingest_file, tmp_path, "compras_universal", local_name)
                    profile_usado = "compras_universal"
                except ValueError as e_universal:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"El archivo '{file.filename}' no coincide con ningún formato de compras conocido. "
                            f"Sistema: {e_sistema} | Universal: {e_universal}"
                        ),
                    )
                except Exception:
                    # El archivo ni siquiera se pudo ABRIR como Excel (PDF renombrado,
                    # archivo corrupto, etc.) -- mismo caso que en /api/ingest (ventas, ver
                    # main.py): python_calamine.CalamineError no es ValueError, así que sin
                    # este except se escapaba hasta un 500 genérico (o, peor, tumbaba la
                    # conexión sin respuesta HTTP válida) en vez del 422 explícito que el
                    # frontend ya sabe mostrar.
                    raise HTTPException(status_code=422, detail=MSG_ARCHIVO_NO_VALIDO)
            except Exception:
                # Mismo caso que arriba, para cuando falla ya el primer intento
                # (compras_sistema) y ni siquiera llega a intentar compras_universal.
                raise HTTPException(status_code=422, detail=MSG_ARCHIVO_NO_VALIDO)
            all_rows.extend(rows)

    lote = LoteProcesamiento(
        usuario_id=usuario.id,
        tipo_libro="compra",
        sistema_origen=profile_usado,
        cantidad_comprobantes=len(all_rows),
        cantidad_saltos=0,
        estado="cargado",
    )
    db.add(lote)
    db.flush()

    for file in files:
        db.add(ArchivoProcesado(lote_id=lote.id, nombre_archivo=file.filename, perfil=profile_usado))
    db.commit()
    db.refresh(lote)

    _log_evento(db, usuario.id, "carga_archivo", request, lote_id=lote.id, detalle={"archivos": [f.filename for f in files], "sistema": profile_usado})
    _log_evento(db, usuario.id, "conversion", request, lote_id=lote.id, detalle={"cantidad_comprobantes": len(all_rows)})

    return {
        "success": True,
        "lote_id": lote.id,
        "total_rows": len(all_rows),
        "rows": all_rows,
    }


@router.post("/reconcile")
async def reconcile_compras(
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    """REFACTOR ANTI-OOM (Plan de Acción de la auditoría del 02/10, Pilar 2): mismo patrón
    que /api/reconcile en main.py (Ventas) -- el libro llega como archivo (Blob) y se carga
    vía ijson a una base SQLite temporal en vez de json.loads() de una sola vez, el cruce
    Libro↔RG se resuelve con un JOIN en SQLite en vez de dos mapas completos en memoria, y
    la respuesta se arma con StreamingResponse en vez de un dict único. Ver
    app/core/sqlite_cruce.py (helpers compartidos) y compras_engine.py
    (reconcile_compras_with_rg_iter_pares). La lectura de la RG en sí
    (engine.ingest_file) queda sin streaming propio por ahora -- Fase 2 separada, ver el
    plan de esta tarea."""
    form = await request.form(max_part_size=FORM_MAX_PART_SIZE)
    rg_files = form.getlist("rg_files")
    pos_data_file = form.get("pos_data_json")
    if not rg_files or pos_data_file is None:
        raise HTTPException(status_code=422, detail="Faltan los archivos de RG o el libro de compras a comparar.")
    # pos_data_json tiene que llegar como ARCHIVO (Blob), no como campo de texto plano --
    # mismo chequeo y mismo motivo que /api/reconcile en main.py (Ventas): un frontend
    # desplegado antes de este cambio seguía mandando JSON.stringify(...) como texto plano.
    if not isinstance(pos_data_file, StarletteUploadFile):
        raise HTTPException(
            status_code=422,
            detail=(
                "El libro de compras a comparar (pos_data_json) llegó como texto plano, no como "
                "archivo. Esto pasa si el frontend desplegado es anterior al commit que lo manda "
                "como Blob (services/api.ts, reconcileComprasApi) — verificá que el build de "
                "siscom-rg90 en /var/www/siscom incluya ese cambio."
            ),
        )
    lote_id_raw = form.get("lote_id")
    lote_id = int(lote_id_raw) if lote_id_raw else None
    # IDOR corregido acá (mismo fix que /api/reconcile en main.py): lote_id viaja como
    # campo de formulario controlado por el cliente -- sin este chequeo, cualquier usuario
    # autenticado podía reescribir el estado y el resultado de un lote de OTRO usuario con
    # solo mandar su id. 404 en vez de 403 para no confirmar si ese lote existe.
    if lote_id is not None:
        lote_ajeno = db.get(LoteProcesamiento, lote_id)
        if lote_ajeno is not None and lote_ajeno.usuario_id != usuario.id:
            raise HTTPException(status_code=404, detail="Lote no encontrado.")

    # Límite de concurrencia (Plan de Acción del 02/10, Pilar 5): mismo criterio que
    # /api/reconcile (main.py) -- se adquiere acá, después de las validaciones rápidas de
    # arriba, y se libera recién en el finally de _generar_respuesta() más abajo (el
    # trabajo pesado real sigue corriendo después de que esta función retorne el
    # StreamingResponse). Si algo falla antes de llegar al generador, se libera en los dos
    # except de abajo.
    await adquirir_operacion_pesada()

    con, tmp_dir_sqlite = nueva_sqlite_temporal(tabla_a="libro", tabla_b="rg")
    try:
        # acumulador compartido entre libro y RG -- mismo criterio que /api/reconcile:
        # si el libro ya tiene duplicados, se sigue cargando la RG igual para reportar los
        # duplicados de ambos orígenes en un solo 422 consolidado, en vez de cortar en el
        # primero y que la RG ni se llegue a leer.
        duplicados_acumulados: list = []

        cantidad_comprobantes = await _cargar_libro_compras_en_sqlite(con, pos_data_file, duplicados_acumulados)

        # rg_rows todavía se necesita una sola vez acá para detect_sequence_gaps -- se
        # descarta con `del` apenas se usa, antes de armar la respuesta (ver docstring de
        # _cargar_rg_compras_en_sqlite).
        rg_rows = await _cargar_rg_compras_en_sqlite(con, rg_files, duplicados_acumulados)

        if duplicados_acumulados:
            raise HTTPException(status_code=422, detail=armar_error_duplicados(duplicados_acumulados, origen_default="RG"))

        con.commit()
        # Saltos de numeración DENTRO de la RG de compras misma (agrupados por proveedor —
        # ver detect_sequence_gaps), no contra el libro propio: acá no hay control de
        # correlatividad del libro propio, no es responsabilidad del comprador que un
        # proveedor salte numeración (ver docstring de ComprasEngine).
        rg_gaps = detect_sequence_gaps(rg_rows)
        rg_total_rows = len(rg_rows)
        del rg_rows
        gc.collect()
    except HTTPException:
        con.close()
        shutil.rmtree(tmp_dir_sqlite, ignore_errors=True)
        liberar_operacion_pesada()
        raise
    except Exception as e:
        con.close()
        shutil.rmtree(tmp_dir_sqlite, ignore_errors=True)
        liberar_operacion_pesada()
        raise HTTPException(status_code=500, detail=f"Error inesperado al leer los archivos para comparar ({type(e).__name__}): {e}")

    nombres_rg = ", ".join(f.filename for f in rg_files)

    def _generar_respuesta():
        try:
            counts = {"coinciden": 0, "no_en_rg": 0, "no_en_libro": 0, "diferencia_importe": 0, "diferencias_tasas": 0}

            # Mismo buffer de ~64KB que /api/reconcile (ver ese comentario en main.py) --
            # baja el número de writes de socket sin retener todas las filas en memoria.
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

            _push('{"success": true, "rg_total_rows": %d, "rg_rows": [' % rg_total_rows)
            # Las filas de RG se leen DE VUELTA desde SQLite (no de la lista Python, ya
            # borrada arriba) -- mismo criterio que rg90_rows en /api/reconcile. ORDER BY
            # rowid preserva el orden de inserción (= orden del archivo original).
            for idx, (data,) in enumerate(con.execute("SELECT data FROM rg ORDER BY rowid")):
                if idx:
                    _push(",")
                _push(data)
                chunk = _flush_si_corresponde()
                if chunk is not None:
                    yield chunk
            _push('], "rg_gaps": ')
            _push(json.dumps(rg_gaps))
            _push(', "diffs": [')

            # EL CRUCE: FULL OUTER JOIN por `doc` (acá es la `clave` de compras, ver
            # _cargar_libro_compras_en_sqlite) -- simulado con LEFT JOIN + UNION ALL, mismo
            # motivo que /api/reconcile (esta versión de SQLite es anterior a la 3.39).
            # tipo_doc no hace falta en el JOIN: siempre es "" de los dos lados.
            cursor_join = con.execute("""
                SELECT l.doc, l.data, r.data
                FROM libro l LEFT JOIN rg r ON l.doc = r.doc
                UNION ALL
                SELECT r.doc, NULL, r.data
                FROM rg r LEFT JOIN libro l ON l.doc = r.doc
                WHERE l.doc IS NULL
                ORDER BY 1
            """)

            def _pares_desde_sqlite():
                for clave, libro_json, rg_json in cursor_join:
                    pos_rec = json.loads(libro_json) if libro_json is not None else None
                    rg_rec = json.loads(rg_json) if rg_json is not None else None
                    yield clave, pos_rec, rg_rec

            total_diffs = 0
            for total_diffs, d in enumerate(reconcile_compras_with_rg_iter_pares(_pares_desde_sqlite()), start=1):
                if total_diffs > 1:
                    _push(",")
                _push(json.dumps(d))
                clave_contador = _CATEGORIA_A_CONTADOR_RECONCILE_COMPRAS.get(d["diferencia"])
                if clave_contador:
                    counts[clave_contador] += 1
                chunk = _flush_si_corresponde()
                if chunk is not None:
                    yield chunk
                if total_diffs % 20000 == 0:
                    gc.collect()

            _push('], "summary": ')
            _push(json.dumps(counts))
            if buffer:
                yield "".join(buffer)

            lote = db.get(LoteProcesamiento, lote_id) if lote_id else None
            if lote is None:
                lote = LoteProcesamiento(
                    usuario_id=usuario.id,
                    tipo_libro="compra",
                    sistema_origen="rg_compras",
                    cantidad_comprobantes=cantidad_comprobantes,
                    estado="comparado_rg",
                )
                db.add(lote)
                db.flush()
            else:
                lote.estado = "comparado_rg"

            db.add(ResultadoRG90(
                lote_id=lote.id,
                archivo_rg90_nombre=nombres_rg,
                total_coinciden=counts["coinciden"],
                total_no_en_rg90=counts["no_en_rg"],
                total_no_en_libro=counts["no_en_libro"],
                total_saltos=len(rg_gaps),
            ))
            db.commit()

            _log_evento(db, usuario.id, "comparacion_rg", request, lote_id=lote.id, detalle={
                "archivos_rg": [f.filename for f in rg_files],
                "coinciden": counts["coinciden"],
                "no_en_rg": counts["no_en_rg"],
                "no_en_libro": counts["no_en_libro"],
                "diferencia_importe": counts["diferencia_importe"],
                "diferencias_tasas": counts["diferencias_tasas"],
                "saltos_rg": len(rg_gaps),
            })

            yield ', "lote_id": %d}' % lote.id
        finally:
            # Limpieza obligatoria de la base SQLite temporal -- mismo criterio que
            # /api/reconcile, pase lo que pase durante el streaming.
            con.close()
            shutil.rmtree(tmp_dir_sqlite, ignore_errors=True)
            gc.collect()
            # Recién ACÁ se libera el slot del semáforo de concurrencia -- ver dónde se
            # adquirió, más arriba, y el docstring de liberar_operacion_pesada.
            liberar_operacion_pesada()

    return StreamingResponse(_generar_respuesta(), media_type="application/json")
