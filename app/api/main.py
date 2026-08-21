"""
FastAPI REST API Service for SISCOM RG90 Core.
Provides endpoints for profile management, file ingestion, sequence gap detection, and RG90 reconciliation.
"""

import os
import shutil
import tempfile
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.api.auditoria import router as auditoria_router
from app.api.auth import router as auth_router
from app.api.locales import router as locales_router
from app.api.roles import router as roles_router
from app.core.config import settings
from app.core.deps import get_current_user
from app.core.engine import IngestionEngine, detect_sequence_gaps, reconcile_with_rg90
from app.db.database import get_db
from app.db.models import ArchivoProcesado, EventoAuditoria, LoteProcesamiento, ResultadoRG90, Usuario

PROFILES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "profiles")
engine = IngestionEngine(PROFILES_DIR)

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
    system_key: str = Form(...),
    local_name: Optional[str] = Form("Local General"),
    tipo_libro: str = Form("venta"),
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    profile_id = "aloha" if "aloha" in system_key.lower() else ("hiopos_ventas" if "hiopos" in system_key.lower() else "universal")
    all_rows = []
    all_cortes = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        for file in files:
            ext = os.path.splitext(file.filename)[1].lower()
            tmp_path = os.path.join(tmp_dir, file.filename)
            with open(tmp_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            try:
                rows, cortes = engine.ingest_file(tmp_path, profile_id, local_name)
            except ValueError as e:
                # El archivo no corresponde al sistema elegido (firma no encontrada) — se
                # bloquea acá, antes de crear ningún lote, para que el Paso 1 no avance.
                raise HTTPException(status_code=422, detail=str(e))
            all_rows.extend(rows)
            for c in cortes:
                all_cortes.append({**c, "archivo": file.filename, "local": local_name})

    gaps = detect_sequence_gaps(all_rows)

    lote = LoteProcesamiento(
        usuario_id=usuario.id,
        tipo_libro=tipo_libro,
        sistema_origen=system_key,
        cantidad_comprobantes=len(all_rows),
        cantidad_saltos=len(gaps),
        estado="cargado",
    )
    db.add(lote)
    db.flush()  # obtiene lote.id sin cerrar la transacción

    for file in files:
        db.add(ArchivoProcesado(lote_id=lote.id, nombre_archivo=file.filename, perfil=profile_id))
    db.commit()
    db.refresh(lote)

    _log_evento(db, usuario.id, "carga_archivo", request, lote_id=lote.id, detalle={"archivos": [f.filename for f in files], "sistema": system_key})
    _log_evento(db, usuario.id, "conversion", request, lote_id=lote.id, detalle={"cantidad_comprobantes": len(all_rows), "cantidad_saltos": len(gaps)})

    return {
        "success": True,
        "lote_id": lote.id,
        "total_rows": len(all_rows),
        "gaps_count": len(gaps),
        "rows": all_rows,
        "gaps": gaps,
        "cortes": all_cortes
    }


@app.post("/api/reconcile")
async def reconcile(
    request: Request,
    rg90_files: List[UploadFile] = File(...),
    pos_data_json: str = Form(...),
    lote_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    """Compara el libro propio contra la RG90. Acepta uno o dos archivos de RG90 (venta y
    nota de crédito), que se consolidan antes de comparar — ver Minuta 3: la RG90 se
    descarga en reportes separados por tipo de comprobante."""
    import json
    pos_rows = json.loads(pos_data_json)

    rg90_rows = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        for rg90_file in rg90_files:
            tmp_path = os.path.join(tmp_dir, rg90_file.filename)
            with open(tmp_path, "wb") as buffer:
                shutil.copyfileobj(rg90_file.file, buffer)
            rg90_file_rows, _rg90_cortes = engine.ingest_file(tmp_path, "rg90_set", "RG90 SET")
            rg90_rows.extend(rg90_file_rows)

    diffs = reconcile_with_rg90(pos_rows, rg90_rows)

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

    return {
        "success": True,
        "lote_id": lote.id,
        "rg90_total_rows": len(rg90_rows),
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
