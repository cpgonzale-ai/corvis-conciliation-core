"""
FastAPI REST API Service for SISCOM RG90 Core.
Provides endpoints for profile management, file ingestion, sequence gap detection, and RG90 reconciliation.
"""

import asyncio
import gc
import json
import logging
import os
import tempfile
from typing import List, Optional

import ijson
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.auditoria import router as auditoria_router
from app.api.auth import router as auth_router
from app.api.compras import router as compras_router
from app.api.export import router as export_router
from app.api.locales import router as locales_router
from app.api.roles import router as roles_router
from app.core.audit import log_evento as _log_evento
from app.core.config import settings
from app.core.deps import get_current_user
from app.core.engine import IngestionEngine, detect_sequence_gaps, reconcile_with_rg90_iter
from app.core.uploads import guardar_archivo_seguro
from app.db.database import get_db
from app.db.models import ArchivoProcesado, LoteProcesamiento, ResultadoRG90, Usuario

_logger = logging.getLogger("app.api.reconcile")

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


@app.post("/api/reconcile")
async def reconcile(
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    """Compara el libro propio contra la RG90. Acepta uno o dos archivos de RG90 (venta y
    nota de crédito), que se consolidan antes de comparar — ver Minuta 3: la RG90 se
    descarga en reportes separados por tipo de comprobante.

    REFACTOR POR OOM (ver auditoria/12-certificacion-salud-sistema.md, "Hallazgo crítico"):
    un pedido de 200.000 filas hacía que este endpoint construyera, todas a la vez, varias
    copias completas del mismo volumen de datos — el JSON crudo recibido, la lista
    `pos_rows`, la lista `rg90_rows`, la lista `diffs` (que además duplica campos de ambos
    lados) y por último el JSON de la respuesta entera — medido en un worker real: RSS de
    130MB a 1,83GB, muerte sin traceback (OOM-kill). Ahora:
    - `pos_data_json` llega como ARCHIVO (Blob, ver services/api.ts), no como campo de texto
      plano — Starlette lo puede recibir en streaming a un spool en disco en vez de
      bufferearlo entero como un string en RAM.
    - Se parsea con `ijson` (streaming) directo a `libro_map`, sin pasar por una lista
      `pos_rows` intermedia ni por `json.loads()` del documento completo.
    - `reconcile_with_rg90_iter` (engine.py) es la MISMA lógica de comparación de siempre,
      pero como generador: nunca existe una lista `diffs` con las 200.000 filas enriquecidas
      completas al mismo tiempo.
    - La respuesta se arma con `StreamingResponse`, escribiendo el JSON manualmente a medida
      que se van generando los diffs (chunks), en vez de construir el dict de respuesta
      entero y dejar que FastAPI lo serialice de una sola vez.
    - `gc.collect()` explícito cada 20.000 filas, pedido así aunque el CPython de referencia
      ya libera por refcounting la mayoría de esto sin ayuda — sirve igual para forzar la
      recolección de basura cíclica y no cuesta casi nada frente al resto del trabajo.
    La lógica de negocio (qué se considera diferencia, cómo se arma cada campo) no cambió
    una sola línea — ver reconcile_with_rg90_iter en engine.py, son las mismas ramas.
    """
    form = await request.form(max_part_size=FORM_MAX_PART_SIZE)
    rg90_files = form.getlist("rg90_files")
    pos_data_file = form.get("pos_data_json")
    if not rg90_files or pos_data_file is None:
        raise HTTPException(status_code=422, detail="Faltan los archivos de RG90 o el libro a comparar.")
    lote_id_raw = form.get("lote_id")
    lote_id = int(lote_id_raw) if lote_id_raw else None

    # Envuelve la parte NO transmitida todavía (lectura del libro + de la RG90): acá sí se
    # puede seguir devolviendo un HTTPException limpio, porque todavía no se mandó ningún
    # byte de la respuesta. Una vez que arranca el streaming (más abajo) ya no se puede.
    try:
        # Parseo en streaming de pos_data_json directo a libro_map -- nunca existe una
        # lista `pos_rows` de 200.000 dicts por separado del mapa, y el archivo se lee en
        # bloques desde su spool en disco (SpooledTemporaryFile), no como un string de
        # ~100MB ya reconstruido entero en memoria.
        libro_map: dict = {}
        cantidad_comprobantes = 0
        try:
            pos_data_file.file.seek(0)
            # use_float=True: por defecto ijson devuelve los números como decimal.Decimal
            # (para no perder precisión al parsear en streaming) en vez de float como hacía
            # json.loads() -- sin esto, _monto_diff() revienta con TypeError al restar un
            # Decimal (de acá) contra un float (de engine.ingest_file, sin cambios, sigue
            # devolviendo float) apenas se compara un comprobante que existe en ambos lados.
            for row in ijson.items(pos_data_file.file, "item", use_float=True):
                libro_map[row["doc"]] = row
                cantidad_comprobantes += 1
        except (ValueError, KeyError) as e:
            raise HTTPException(status_code=422, detail=f"El libro enviado para comparar no tiene el formato esperado: {e}")
        finally:
            await pos_data_file.close()

        rg90_rows = []
        loop = asyncio.get_event_loop()
        with tempfile.TemporaryDirectory() as tmp_dir:
            for rg90_file in rg90_files:
                tmp_path = await guardar_archivo_seguro(rg90_file, tmp_dir)
                # Mismo criterio que /api/compras/reconcile: un archivo que no corresponde
                # al formato de la RG90 (firma no encontrada, hoja inesperada, etc.) no debe
                # tumbar el pedido entero con un 500 genérico — se informa qué archivo falló
                # y por qué.
                try:
                    rg90_file_rows, _rg90_cortes = await loop.run_in_executor(None, engine.ingest_file, tmp_path, "rg90_set", "RG90 SET")
                except ValueError as e:
                    raise HTTPException(status_code=422, detail=f"Error al procesar el archivo RG90 '{rg90_file.filename}': {e}")
                rg90_rows.extend(rg90_file_rows)

        rg90_map = {r["doc"]: r for r in rg90_rows}

        # Saltos de numeración DENTRO de la RG90 misma (no contra el libro propio) — mismo
        # detector que ya usa /api/ingest sobre el libro propio, para el Paso 3 (Adjuntar RG90).
        rg90_gaps = detect_sequence_gaps(rg90_rows)
    except HTTPException:
        raise
    except Exception as e:
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

            _push('{"success": true, "rg90_total_rows": %d, "rg90_rows": [' % len(rg90_rows))
            for idx, r in enumerate(rg90_rows):
                if idx:
                    _push(",")
                _push(json.dumps(r))
                chunk = _flush_si_corresponde()
                if chunk is not None:
                    yield chunk
            _push('], "rg90_gaps": ')
            _push(json.dumps(rg90_gaps))
            _push(', "diffs": [')

            total_diffs = 0
            for total_diffs, d in enumerate(reconcile_with_rg90_iter(libro_map, rg90_map), start=1):
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
            libro_map.clear()
            gc.collect()

    return StreamingResponse(_generar_respuesta(), media_type="application/json")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.api.main:app", host="0.0.0.0", port=8000, reload=True)
