"""Dependencias de FastAPI: sesión de DB, usuario autenticado y permisos dinámicos."""

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.security import decode_access_token
from app.db.database import get_db
from app.db.models import Permiso, Rol, Usuario

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Usuario:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudo validar la credencial",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
    except ValueError as exc:
        raise credentials_error from exc

    nro_documento = payload.get("sub")
    if not nro_documento:
        raise credentials_error

    # joinedload (N-a-1, usuario->rol_obj) + selectinload (N-a-N, rol->permisos) en la misma
    # consulta -- antes cada pedido autenticado que tocara usuario.rol_obj.permisos (ej.
    # /api/auth/me) disparaba 2 consultas extra por separado, en el camino más transitado de
    # toda la API. Ver auditoria/11-auditoria-360-completa.md, Fase 3.
    usuario = (
        db.query(Usuario)
        .options(joinedload(Usuario.rol_obj).selectinload(Rol.permisos))
        .filter(Usuario.nro_documento == nro_documento)
        .first()
    )
    if usuario is None or not usuario.activo:
        raise credentials_error
    return usuario


def require_admin(usuario: Usuario = Depends(get_current_user)) -> Usuario:
    if usuario.rol != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere rol de administrador",
        )
    return usuario


def require_permission(clave: str):
    """Genera una dependencia que exige que el rol del usuario tenga asignado el permiso
    `clave` (ej. 'pantalla:locales', 'boton:locales.eliminar') en la tabla rol_permisos.

    El rol 'admin' siempre pasa, sin importar lo que tenga cargado en rol_permisos — es la
    válvula de seguridad para que un admin nunca pueda quedar bloqueado de la propia
    pantalla de roles por una mala edición de permisos (por eso 'admin' y 'operador' están
    protegidos de borrado/renombre en el router de roles: son roles de sistema)."""

    def _dependency(usuario: Usuario = Depends(get_current_user), db: Session = Depends(get_db)) -> Usuario:
        if usuario.rol == "admin":
            return usuario
        if usuario.rol_id is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No tenés permiso para esta acción.")
        tiene_permiso = (
            db.query(Permiso)
            .join(Permiso.roles)
            .filter(Rol.id == usuario.rol_id, Rol.estado == "activo", Permiso.clave == clave)
            .first()
        )
        if not tiene_permiso:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No tenés permiso para esta acción.")
        return usuario

    return _dependency
