"""Router de administración de Roles y Permisos (motor de permisos dinámico).

'admin' y 'operador' son roles de sistema (es_sistema=True): no se pueden renombrar ni
borrar porque el backend depende de que 'admin' exista siempre como válvula de seguridad
(ver require_permission en app.core.deps) y porque el login/JWT actual sigue guardando el
nombre del rol como string. Sus permisos sí se pueden editar libremente."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_permission
from app.db.database import get_db
from app.db.models import EventoAuditoria, Permiso, Rol, Usuario
from app.schemas.auth import PermisoOut, RolCreate, RolOut, RolUpdate

router = APIRouter(prefix="/api/roles", tags=["roles"])


def _log_evento(db: Session, usuario_id: int, accion: str, request: Request, detalle: dict | None = None):
    db.add(EventoAuditoria(
        usuario_id=usuario_id,
        accion=accion,
        detalle=detalle,
        ip_origen=request.client.host if request.client else None,
    ))
    db.commit()


def _rol_out(rol: Rol) -> RolOut:
    return RolOut(
        id=rol.id, nombre=rol.nombre, descripcion=rol.descripcion, es_sistema=rol.es_sistema,
        estado=rol.estado, permisos=[p.clave for p in rol.permisos],
    )


@router.get("/permisos", response_model=list[PermisoOut])
def listar_permisos(db: Session = Depends(get_db), usuario: Usuario = Depends(get_current_user)):
    """Catálogo completo de permisos disponibles (pantallas y botones) — lo consulta la
    pantalla de administración de roles para armar la matriz de checkboxes."""
    return db.query(Permiso).order_by(Permiso.pantalla, Permiso.tipo, Permiso.clave).all()


@router.get("", response_model=list[RolOut])
def listar_roles(db: Session = Depends(get_db), usuario: Usuario = Depends(get_current_user)):
    # Lectura abierta a cualquier usuario autenticado (no solo pantalla:roles): la
    # pantalla de Usuarios también necesita esta lista para el selector de rol al
    # crear/editar, aunque ese rol no tenga acceso a la administración de roles en sí.
    return [_rol_out(r) for r in db.query(Rol).order_by(Rol.nombre).all()]


@router.post("", response_model=RolOut, status_code=status.HTTP_201_CREATED)
def crear_rol(
    datos: RolCreate,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(require_permission("boton:roles.crear")),
):
    if db.query(Rol).filter(Rol.nombre == datos.nombre).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ya existe un rol con ese nombre")

    permisos = db.query(Permiso).filter(Permiso.clave.in_(datos.permisos)).all() if datos.permisos else []
    nuevo = Rol(nombre=datos.nombre, descripcion=datos.descripcion, es_sistema=False, estado=datos.estado, permisos=permisos)
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    _log_evento(db, usuario.id, "alta_rol", request, detalle={"rol_id": nuevo.id, "nombre": nuevo.nombre, "permisos": datos.permisos})
    return _rol_out(nuevo)


@router.put("/{rol_id}", response_model=RolOut)
def editar_rol(
    rol_id: int,
    datos: RolUpdate,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(require_permission("boton:roles.editar")),
):
    rol = db.get(Rol, rol_id)
    if not rol:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rol no encontrado")

    if rol.es_sistema and datos.nombre is not None and datos.nombre != rol.nombre:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se puede renombrar un rol de sistema (admin/operador)")
    if rol.es_sistema and datos.estado == "inactivo":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se puede desactivar un rol de sistema (admin/operador)")

    if datos.nombre is not None:
        rol.nombre = datos.nombre
    if datos.descripcion is not None:
        rol.descripcion = datos.descripcion
    if datos.estado is not None:
        rol.estado = datos.estado
    if datos.permisos is not None:
        rol.permisos = db.query(Permiso).filter(Permiso.clave.in_(datos.permisos)).all()

    db.commit()
    db.refresh(rol)
    _log_evento(db, usuario.id, "edicion_rol", request, detalle={"rol_id": rol.id, "nombre": rol.nombre, "permisos": datos.permisos})
    return _rol_out(rol)


@router.delete("/{rol_id}", status_code=status.HTTP_204_NO_CONTENT)
def eliminar_rol(
    rol_id: int,
    request: Request,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(require_permission("boton:roles.eliminar")),
):
    rol = db.get(Rol, rol_id)
    if not rol:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rol no encontrado")
    if rol.es_sistema:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se puede eliminar un rol de sistema (admin/operador)")
    if rol.usuarios:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"El rol tiene {len(rol.usuarios)} usuario(s) asignado(s) — reasigná esos usuarios antes de borrarlo")

    detalle = {"rol_id": rol.id, "nombre": rol.nombre}
    db.delete(rol)
    db.commit()
    _log_evento(db, usuario.id, "baja_rol", request, detalle=detalle)
