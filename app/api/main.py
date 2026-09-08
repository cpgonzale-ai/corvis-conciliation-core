"""
FastAPI REST API Service for SISCOM RG90 Core.
Provides endpoints for profile management, file ingestion, sequence gap detection, and RG90 reconciliation.
"""

import asyncio
import os
import shutil
import tempfile
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.api.auditoria import router as auditoria_router
from app.api.auth import router as auth_router
from app.api.compras import router as compras_router
from app.api.locales import router as locales_router
from app.api.roles import router as roles_router
from app.core.config import settings
from app.core.deps import get_current_user
from app.core.engine import IngestionEngine, detect_sequence_gaps, reconcile_with_rg90
from app.db.database import get_db
from app.db.models import ArchivoProcesado, EventoAuditoria, LoteProcesamiento, ResultadoRG90, Usuario

PROFILES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "profiles")
engine = IngestionEngine(PROFILES_DIR)

# Starlette limita a 1MB cada "parte" de un multipart/form-data por defecto — incluidos los
# campos de texto, no solo los archivos. pos_data_json (el libro propio, serializado para
# mandarlo a /reconcile) supera ese límite con libros de varios miles de comprobantes (ej.
# 6.246 filas de compras ya pesan ~4MB como JSON; un libro de ventas grande, bastante más).
# Se sube el límite acá en vez de declarar pos_data_json como Form(...) directo, porque
# FastAPI no expone ese parámetro a través del descriptor Form().
FORM_MAX_PART_SIZE = 80 * 1024 * 1024

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
app.include_router(locales_router)
app.include_router(roles_router)
app.include_router(auditoria_router)


def _log_evento(db: Session, usuario_id: int, accion: str, request: Request, lote_id: Optional[int] = None, detalle: Optional[dict] = None):
    db.add(EventoAuditoria(
        usuario_id=usuario_id,
        accion=accion,
        lote_id=lote_id,
        detalle=detalle,
        ip_origen=request.client.host if request.client else None,
    ))
    db.commit()


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
            tmp_path = os.path.join(tmp_dir, file.filename)
            with open(tmp_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

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
    descarga en reportes separados por tipo de comprobante."""
    import json
    form = await request.form(max_part_size=FORM_MAX_PART_SIZE)
    rg90_files = form.getlist("rg90_files")
    pos_data_json = form.get("pos_data_json")
    if not rg90_files or pos_data_json is None:
        raise HTTPException(status_code=422, detail="Faltan los archivos de RG90 o el libro a comparar.")
    lote_id_raw = form.get("lote_id")
    lote_id = int(lote_id_raw) if lote_id_raw else None
    pos_rows = json.loads(pos_data_json)

    # Envuelve todo el cuerpo (no solo la ingesta de la RG90): antes, cualquier excepción
    # no prevista acá (ej. sobre un archivo real más grande o con datos atípicos que no
    # aparecieron en los archivos de prueba) devolvía un 500 genérico de Starlette sin
    # ningún detalle, indistinguible en el frontend de cualquier otro error — quedaba
    # imposible de diagnosticar sin acceso al log del servidor. Con esto, el mensaje real
    # de la excepción llega hasta la pantalla.
    try:
        rg90_rows = []
        loop = asyncio.get_event_loop()
        with tempfile.TemporaryDirectory() as tmp_dir:
            for rg90_file in rg90_files:
                tmp_path = os.path.join(tmp_dir, rg90_file.filename)
                with open(tmp_path, "wb") as buffer:
                    shutil.copyfileobj(rg90_file.file, buffer)
                # Mismo criterio que /api/compras/reconcile: un archivo que no corresponde
                # al formato de la RG90 (firma no encontrada, hoja inesperada, etc.) no debe
                # tumbar el pedido entero con un 500 genérico — se informa qué archivo falló
                # y por qué.
                try:
                    rg90_file_rows, _rg90_cortes = await loop.run_in_executor(None, engine.ingest_file, tmp_path, "rg90_set", "RG90 SET")
                except ValueError as e:
                    raise HTTPException(status_code=422, detail=f"Error al procesar el archivo RG90 '{rg90_file.filename}': {e}")
                rg90_rows.extend(rg90_file_rows)

        diffs = reconcile_with_rg90(pos_rows, rg90_rows)

        # Saltos de numeración DENTRO de la RG90 misma (no contra el libro propio) — mismo
        # detector que ya usa /api/ingest sobre el libro propio, para el Paso 3 (Adjuntar RG90).
        rg90_gaps = detect_sequence_gaps(rg90_rows)

        # Calculate breakdown cards
        no_en_rg90 = len([d for d in diffs if d["diferencia"] == "No llegó a la interfaz"])
        no_en_libro = len([d for d in diffs if d["diferencia"] == "No en libro propio"])
        saltos = len([d for d in diffs if d["diferencia"] == "Salto de numeración"])
        anuladas = len([d for d in diffs if d["diferencia"] == "Anulada"])
        diferencia_monto = len([d for d in diffs if d["diferencia"] == "Diferencia de monto"])
        coinciden = max(0, len(pos_rows) - no_en_rg90 - diferencia_monto - anuladas)

        lote = db.get(LoteProcesamiento, lote_id) if lote_id else None
        if lote is None:
            # Sin lote de origen (llamada directa a /reconcile): se crea uno mínimo para trazabilidad.
            lote = LoteProcesamiento(
                usuario_id=usuario.id,
                tipo_libro="venta",
                sistema_origen="rg90",
                cantidad_comprobantes=len(pos_rows),
                estado="comparado_rg90",
            )
            db.add(lote)
            db.flush()
        else:
            lote.estado = "comparado_rg90"

        nombres_rg90 = ", ".join(f.filename for f in rg90_files)
        db.add(ResultadoRG90(
            lote_id=lote.id,
            archivo_rg90_nombre=nombres_rg90,
            total_coinciden=coinciden,
            total_no_en_rg90=no_en_rg90,
            total_no_en_libro=no_en_libro,
            total_saltos=saltos,
        ))
        db.commit()

        _log_evento(db, usuario.id, "comparacion_rg90", request, lote_id=lote.id, detalle={"archivos_rg90": [f.filename for f in rg90_files], "total_diferencias": len(diffs)})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error inesperado al comparar contra la RG90 ({type(e).__name__}): {e}")

    return {
        "success": True,
        "lote_id": lote.id,
        "rg90_total_rows": len(rg90_rows),
        # Para el nuevo Paso 3 (Adjuntar RG90), que ahora lista los registros de la RG90 tal
        # como se parsearon, antes de mostrar el resultado de la comparación en el Paso 4 —
        # mismo criterio que /api/compras/reconcile con rg_rows.
        "rg90_rows": rg90_rows,
        # Saltos detectados dentro de la RG90 (Paso 3) — no confundir con "saltos" del
        # summary de arriba, que cuenta diffs "Salto de numeración" entre libro y RG90.
        "rg90_gaps": rg90_gaps,
        "summary": {
            "coinciden": coinciden,
            "no_en_rg90": no_en_rg90,
            "no_en_libro": no_en_libro,
            "saltos": saltos,
            "anuladas": anuladas,
            "diferencia_monto": diferencia_monto
        },
        "diffs": diffs
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.api.main:app", host="0.0.0.0", port=8000, reload=True)
