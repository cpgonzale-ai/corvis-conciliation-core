"""Router de consulta de auditoría (CU nuevo: botón "ojito" en Locales/Usuarios/Roles).

No agrega logging nuevo — eventos_auditoria ya se completa desde cada router (locales.py,
roles.py, auth.py) vía _log_evento(). Esto solo expone una lectura filtrada por acción,
con una descripción legible armada a partir del campo `detalle` (JSONB) de cada evento."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.core.deps import get_current_user
from app.db.database import get_db
from app.db.models import EventoAuditoria, Usuario

router = APIRouter(prefix="/api/auditoria", tags=["auditoria"])

# A qué pantalla de administración pertenece cada acción — determina qué permiso hace
# falta para poder consultarla (salvo admin, que siempre puede ver todo).
ACCION_MODULO = {
    "alta_local": "locales", "edicion_local": "locales", "baja_local": "locales",
    "alta_usuario": "usuarios", "edicion_usuario": "usuarios", "baja_usuario": "usuarios",
    "alta_rol": "roles", "edicion_rol": "roles", "baja_rol": "roles",
}

_DESCRIPCIONES = {
    "alta_local": lambda d: f"Creó el local \"{d.get('nombre', '?')}\" (punto de expedición {d.get('punto_expedicion', '?')})",
    "edicion_local": lambda d: f"Editó el local \"{d.get('nombre', '?')}\"",
    "baja_local": lambda d: f"Eliminó el local \"{d.get('nombre', '?')}\" (punto de expedición {d.get('punto_expedicion', '?')})",
    "alta_usuario": lambda d: f"Creó el usuario {d.get('usuario_creado', '?')} (rol {d.get('rol', '?')})",
    "edicion_usuario": lambda d: f"Editó el usuario {d.get('email', '?')}",
    "baja_usuario": lambda d: f"Desactivó el usuario {d.get('email', '?')}",
    "alta_rol": lambda d: f"Creó el rol \"{d.get('nombre', '?')}\" con {len(d.get('permisos') or [])} permiso(s)",
    "edicion_rol": lambda d: f"Editó los permisos del rol \"{d.get('nombre', '?')}\"",
    "baja_rol": lambda d: f"Eliminó el rol \"{d.get('nombre', '?')}\"",
}


class EventoAuditoriaOut(BaseModel):
    id: int
    fecha: datetime
    accion: str
    descripcion: str
    usuario: str


def _descripcion(accion: str, detalle: dict | None) -> str:
    fn = _DESCRIPCIONES.get(accion)
    if not fn:
        return accion
    try:
        return fn(detalle or {})
    except Exception:
        return accion


@router.get("", response_model=list[EventoAuditoriaOut])
def listar_auditoria(
    acciones: str = Query(..., description="Acciones separadas por coma, ej. 'alta_local,edicion_local,baja_local'"),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    lista_acciones = [a.strip() for a in acciones.split(",") if a.strip()]
    if not lista_acciones:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Falta indicar qué acciones consultar")

    if usuario.rol != "admin":
        permisos_usuario = {p.clave for p in usuario.rol_obj.permisos} if usuario.rol_obj else set()
        modulos_requeridos = {ACCION_MODULO[a] for a in lista_acciones if a in ACCION_MODULO}
        for modulo in modulos_requeridos:
            if f"pantalla:{modulo}" not in permisos_usuario:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No tenés permiso para ver esta auditoría.")

    eventos = (
        db.query(EventoAuditoria)
        .options(joinedload(EventoAuditoria.usuario))
        .filter(EventoAuditoria.accion.in_(lista_acciones))
        .order_by(EventoAuditoria.timestamp.desc())
        .limit(limit)
        .all()
    )
    return [
        EventoAuditoriaOut(
            id=e.id,
            fecha=e.timestamp,
            accion=e.accion,
            descripcion=_descripcion(e.accion, e.detalle),
            usuario=f"{e.usuario.nombre} ({e.usuario.email})" if e.usuario else "—",
        )
        for e in eventos
    ]
