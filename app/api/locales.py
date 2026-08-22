"""Router de administración de Locales (CU nuevo: pantalla de Locales).

punto_expedicion son los 3 primeros dígitos del número de documento (ej. '030' en
030-001-0017598) — el frontend los usa para determinar a qué local corresponde cada
comprobante del libro de ventas en el Paso 2."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_permission
from app.db.database import get_db
from app.db.models import EventoAuditoria, Local, Usuario
from app.schemas.auth import LocalCreate, LocalOut, LocalUpdate

router = APIRouter(prefix="/api/locales", tags=["locales"])


def _log_evento(db: Session, usuario_id: int, accion: str, request: Request, detalle: dict | None = None):
    db.add(EventoAuditoria(
        usuario_id=usuario_id,
        accion=accion,
        detalle=detalle,
        ip_origen=request.client.host if request.client else None,
    ))
    db.commit()


def _validar_unicidad(db: Session, punto_expedicion: str | None, codigo: str | None, excluir_id: int | None = None):
    """punto_expedicion y código no pueden repetirse entre locales (dos locales con el
    mismo punto de expedición harían ambigua la resolución automática del local en el Paso
    2). código sí puede quedar vacío en varios locales — solo se valida cuando viene con
    valor."""
    if punto_expedicion:
        q = db.query(Local).filter(Local.punto_expedicion == punto_expedicion)
        if excluir_id is not None:
            q = q.filter(Local.id != excluir_id)
        existente = q.first()
        if existente:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"El punto de expedición \"{punto_expedicion}\" ya está asignado al local \"{existente.nombre}\".",
            )
    if codigo:
        q = db.query(Local).filter(Local.codigo == codigo)
        if excluir_id is not None:
            q = q.filter(Local.id != excluir_id)
        existente = q.first()
        if existente:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"El código \"{codigo}\" ya está asignado al local \"{existente.nombre}\".",
            )


@router.get("", response_model=list[LocalOut])
def listar_locales(db: Session = Depends(get_db), usuario: Usuario = Depends(get_current_user)):
    # Lectura disponible para cualquier usuario autenticado: el Paso 2 la necesita para
    # resolver a qué local corresponde cada comprobante, no solo la pantalla de admin.
    return db.query(Local).order_by(Local.nombre).all()


@router.post("", response_model=LocalOut, status_code=status.HTTP_201_CREATED)
def crear_local(
    datos: LocalCreate,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(require_permission("boton:locales.crear")),
):
    _validar_unicidad(db, datos.punto_expedicion, datos.codigo)
    nuevo = Local(**datos.model_dump())
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    _log_evento(db, usuario.id, "alta_local", request, detalle={"local_id": nuevo.id, "nombre": nuevo.nombre, "punto_expedicion": nuevo.punto_expedicion})
    return nuevo


@router.put("/{local_id}", response_model=LocalOut)
def editar_local(
    local_id: int,
    datos: LocalUpdate,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(require_permission("boton:locales.editar")),
):
    local = db.get(Local, local_id)
    if not local:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local no encontrado")
    cambios = datos.model_dump(exclude_unset=True)
    _validar_unicidad(
        db,
        cambios.get("punto_expedicion", local.punto_expedicion),
        cambios.get("codigo", local.codigo),
        excluir_id=local.id,
    )
    for campo, valor in cambios.items():
        setattr(local, campo, valor)
    db.commit()
    db.refresh(local)
    _log_evento(db, usuario.id, "edicion_local", request, detalle={"local_id": local.id, "nombre": local.nombre})
    return local


@router.delete("/{local_id}", status_code=status.HTTP_204_NO_CONTENT)
def eliminar_local(
    local_id: int,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(require_permission("boton:locales.eliminar")),
):
    local = db.get(Local, local_id)
    if not local:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Local no encontrado")
    detalle = {"local_id": local.id, "nombre": local.nombre, "punto_expedicion": local.punto_expedicion}
    db.delete(local)
    db.commit()
    _log_evento(db, usuario.id, "baja_local", request, detalle=detalle)
