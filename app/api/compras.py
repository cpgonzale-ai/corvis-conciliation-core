"""Router del Libro de Compras (Minuta 5): carga del export del sistema y comparación
contra la RG. Mismo patrón que /api/ingest y /api/reconcile (ventas), pero usando
ComprasEngine — ver app/core/compras_engine.py sobre por qué compras necesita su propio
motor en vez de reusar el de ventas."""

import asyncio
import json
import os
import tempfile
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.core.audit import log_evento as _log_evento
from app.core.compras_engine import ComprasEngine, reconcile_compras_with_rg
from app.core.engine import detect_sequence_gaps
from app.core.deps import get_current_user
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
            tmp_path = await guardar_archivo_seguro(rg_file, tmp_dir)
            try:
                file_rows = await loop.run_in_executor(None, engine.ingest_file, tmp_path, "rg_compras", "RG")
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
            rg_rows.extend(file_rows)

    diffs = reconcile_compras_with_rg(pos_rows, rg_rows)

    # Saltos de numeración DENTRO de la RG de compras misma (agrupados por proveedor — ver
    # detect_sequence_gaps), no contra el libro propio: acá no hay control de correlatividad
    # del libro propio, no es responsabilidad del comprador que un proveedor salte
    # numeración (ver docstring de ComprasEngine).
    rg_gaps = detect_sequence_gaps(rg_rows)

    no_en_rg = len([d for d in diffs if d["diferencia"] == "No llegó a la interfaz"])
    no_en_libro = len([d for d in diffs if d["diferencia"] == "No existe en el libro"])
    diferencia_monto = len([d for d in diffs if d["diferencia"] == "Diferencia de monto"])
    # Ahora que reconcile_compras_with_rg informa "Coincide" como categoría real (antes no
    # guardaba nada para esos comprobantes), se cuenta directo en vez de por resta.
    coinciden = len([d for d in diffs if d["diferencia"] == "Coincide"])

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
        total_saltos=len(rg_gaps),
    ))
    db.commit()

    _log_evento(db, usuario.id, "comparacion_rg", request, lote_id=lote.id, detalle={
        "archivos_rg": [f.filename for f in rg_files],
        "coinciden": coinciden,
        "no_en_rg": no_en_rg,
        "no_en_libro": no_en_libro,
        "diferencia_monto": diferencia_monto,
        "saltos_rg": len(rg_gaps),
    })

    return {
        "success": True,
        "lote_id": lote.id,
        "rg_total_rows": len(rg_rows),
        # Filas de la RG ya parseadas — el frontend las lista en el paso 2 (igual que el
        # libro propio en el paso 1), para poder consultar ambos lados antes de ver el
        # resultado de la comparación en el paso 3.
        "rg_rows": rg_rows,
        # Saltos de numeración dentro de la RG misma (ver comentario arriba) — mismo
        # criterio que rg90_gaps en /api/reconcile (ventas).
        "rg_gaps": rg_gaps,
        "diffs": diffs,
        "summary": {
            "coinciden": coinciden,
            "no_en_rg": no_en_rg,
            "no_en_libro": no_en_libro,
            "diferencia_monto": diferencia_monto,
        },
    }
