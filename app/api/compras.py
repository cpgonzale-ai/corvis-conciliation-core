"""Router del Libro de Compras (Minuta 5): carga del export del sistema y comparación
contra la RG. Mismo patrón que /api/ingest y /api/reconcile (ventas), pero usando
ComprasEngine — ver app/core/compras_engine.py sobre por qué compras necesita su propio
motor en vez de reusar el de ventas."""

import asyncio
import json
import os
import shutil
import tempfile
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.core.compras_engine import ComprasEngine, reconcile_compras_with_rg
from app.core.deps import get_current_user
from app.db.database import get_db
from app.db.models import ArchivoProcesado, EventoAuditoria, LoteProcesamiento, ResultadoRG90, Usuario

PROFILES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "profiles")
engine = ComprasEngine(PROFILES_DIR)

# Ver la misma constante en app/api/main.py: Starlette limita a 1MB cada "parte" de un
# multipart/form-data por defecto (también los campos de texto, no solo los archivos), y
# pos_data_json puede superar eso ampliamente con libros de miles de comprobantes.
FORM_MAX_PART_SIZE = 80 * 1024 * 1024

router = APIRouter(prefix="/api/compras", tags=["compras"])


def _log_evento(db: Session, usuario_id: int, accion: str, request: Request, lote_id: Optional[int] = None, detalle: Optional[dict] = None):
    db.add(EventoAuditoria(
        usuario_id=usuario_id,
        accion=accion,
        lote_id=lote_id,
        detalle=detalle,
        ip_origen=request.client.host if request.client else None,
    ))
    db.commit()


@router.post("/ingest")
async def ingest_compras(
    request: Request,
    files: List[UploadFile] = File(...),
    local_name: Optional[str] = Form("Local General"),
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    all_rows = []
    loop = asyncio.get_event_loop()

    with tempfile.TemporaryDirectory() as tmp_dir:
        for file in files:
            tmp_path = os.path.join(tmp_dir, file.filename)
            with open(tmp_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            try:
                rows = await loop.run_in_executor(None, engine.ingest_file, tmp_path, "compras_sistema", local_name)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
            all_rows.extend(rows)

    lote = LoteProcesamiento(
        usuario_id=usuario.id,
        tipo_libro="compra",
        sistema_origen="compras_sistema",
        cantidad_comprobantes=len(all_rows),
        cantidad_saltos=0,
        estado="cargado",
    )
    db.add(lote)
    db.flush()

    for file in files:
        db.add(ArchivoProcesado(lote_id=lote.id, nombre_archivo=file.filename, perfil="compras_sistema"))
    db.commit()
    db.refresh(lote)

    _log_evento(db, usuario.id, "carga_archivo", request, lote_id=lote.id, detalle={"archivos": [f.filename for f in files], "sistema": "compras_sistema"})
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
    form = await request.form(max_part_size=FORM_MAX_PART_SIZE)
    rg_files = form.getlist("rg_files")
    pos_data_json = form.get("pos_data_json")
    if not rg_files or pos_data_json is None:
        raise HTTPException(status_code=422, detail="Faltan los archivos de RG o el libro de compras a comparar.")
    lote_id_raw = form.get("lote_id")
    lote_id = int(lote_id_raw) if lote_id_raw else None
    pos_rows = json.loads(pos_data_json)

    rg_rows = []
    loop = asyncio.get_event_loop()
    with tempfile.TemporaryDirectory() as tmp_dir:
        for rg_file in rg_files:
            tmp_path = os.path.join(tmp_dir, rg_file.filename)
            with open(tmp_path, "wb") as buffer:
                shutil.copyfileobj(rg_file.file, buffer)
            try:
                file_rows = await loop.run_in_executor(None, engine.ingest_file, tmp_path, "rg_compras", "RG")
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
            rg_rows.extend(file_rows)

    diffs = reconcile_compras_with_rg(pos_rows, rg_rows)

    no_en_rg = len([d for d in diffs if d["diferencia"] == "No llegó a la interfaz"])
    no_en_libro = len([d for d in diffs if d["diferencia"] == "No en libro propio"])
    diferencia_monto = len([d for d in diffs if d["diferencia"] == "Diferencia de monto"])
    coinciden = max(0, len(pos_rows) - no_en_rg - diferencia_monto)

    lote = db.get(LoteProcesamiento, lote_id) if lote_id else None
    if lote is None:
        lote = LoteProcesamiento(
            usuario_id=usuario.id,
            tipo_libro="compra",
            sistema_origen="rg_compras",
            cantidad_comprobantes=len(pos_rows),
            estado="comparado_rg",
        )
        db.add(lote)
        db.flush()
    else:
        lote.estado = "comparado_rg"

    nombres_rg = ", ".join(f.filename for f in rg_files)
    db.add(ResultadoRG90(
        lote_id=lote.id,
        archivo_rg90_nombre=nombres_rg,
        total_coinciden=coinciden,
        total_no_en_rg90=no_en_rg,
        total_no_en_libro=no_en_libro,
        total_saltos=0,
    ))
    db.commit()

    _log_evento(db, usuario.id, "comparacion_rg", request, lote_id=lote.id, detalle={
        "archivos_rg": [f.filename for f in rg_files],
        "coinciden": coinciden,
        "no_en_rg": no_en_rg,
        "no_en_libro": no_en_libro,
        "diferencia_monto": diferencia_monto,
    })

    return {
        "success": True,
        "lote_id": lote.id,
        "rg_total_rows": len(rg_rows),
        "diffs": diffs,
        "summary": {
            "coinciden": coinciden,
            "no_en_rg": no_en_rg,
            "no_en_libro": no_en_libro,
            "diferencia_monto": diferencia_monto,
        },
    }
