"""Router de autenticación y gestión de usuarios (CU-01)."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core import permisos_cache
from app.core.audit import log_evento as _log_evento
from app.core.deps import get_current_user, require_permission
from app.core.security import create_access_token, hash_password, verify_password
from app.db.database import get_db
from app.db.models import Rol, Usuario
from app.schemas.auth import MeOut, TokenResponse, UsuarioCreate, UsuarioOut, UsuarioUpdate

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # El usuario para loguearse es el número de documento (CU-01), no el email — el campo
    # se sigue llamando "username" porque así lo pide el formato estándar OAuth2 del form.
    usuario = db.query(Usuario).filter(Usuario.nro_documento == form_data.username.strip()).first()
    if not usuario or not usuario.activo or not verify_password(form_data.password, usuario.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales inválidas")

    token = create_access_token(subject=usuario.nro_documento, rol=usuario.rol)
    _log_evento(db, usuario.id, "login", request)
    return TokenResponse(access_token=token, rol=usuario.rol)


@router.get("/me", response_model=MeOut)
def me(db: Session = Depends(get_db), usuario: Usuario = Depends(get_current_user)):
    if usuario.rol == "admin":
        # El rol admin siempre tiene acceso total (ver require_permission), así que se
        # informa el catálogo completo de permisos aunque a rol_permisos le falte alguno
        # por una mala edición — el frontend no debe ocultarle nada al admin.
        permisos = [clave for _id, clave, _nombre, _tipo, _pantalla in permisos_cache.permisos_catalogo_rows(db)]
    else:
        # Un rol inactivo no habilita ningún permiso (mismo criterio que require_permission).
        # permisos_cache.rol_info en vez de usuario.rol_obj.permisos -- ver
        # app/core/permisos_cache.py y auditoria/11-auditoria-360-completa.md, Fase 3/4.
        info = permisos_cache.rol_info(db, usuario.rol_id) if usuario.rol_id else None
        permisos = list(info["permisos"]) if info and info["estado"] == "activo" else []
    return MeOut(**UsuarioOut.model_validate(usuario).model_dump(), permisos=permisos)


@router.post("/usuarios", response_model=UsuarioOut, status_code=status.HTTP_201_CREATED)
def crear_usuario(
    datos: UsuarioCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(require_permission("boton:usuarios.crear")),
):
    if db.query(Usuario).filter(Usuario.email == datos.email).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="El email ya está registrado")
    if db.query(Usuario).filter(Usuario.nro_documento == datos.nro_documento).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ese número de documento ya está registrado")

    rol = db.get(Rol, datos.rol_id)
    if not rol:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El rol indicado no existe")

    nuevo = Usuario(
        nombre=datos.nombre,
        nro_documento=datos.nro_documento,
        email=datos.email,
        password_hash=hash_password(datos.password),
        rol=rol.nombre,
        rol_id=rol.id,
        activo=datos.activo,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    _log_evento(db, admin.id, "alta_usuario", request, detalle={"usuario_id": nuevo.id, "usuario_creado": nuevo.nro_documento, "rol": rol.nombre})
    return nuevo


@router.get("/usuarios", response_model=list[UsuarioOut])
def listar_usuarios(db: Session = Depends(get_db), usuario: Usuario = Depends(require_permission("pantalla:usuarios"))):
    return db.query(Usuario).order_by(Usuario.id).all()


@router.put("/usuarios/{usuario_id}", response_model=UsuarioOut)
def editar_usuario(
    usuario_id: int,
    datos: UsuarioUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(require_permission("boton:usuarios.editar")),
):
    objetivo = db.get(Usuario, usuario_id)
    if not objetivo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")

    if datos.nro_documento is not None and datos.nro_documento != objetivo.nro_documento:
        if db.query(Usuario).filter(Usuario.nro_documento == datos.nro_documento, Usuario.id != usuario_id).first():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ese número de documento ya está registrado")
        objetivo.nro_documento = datos.nro_documento
    if datos.nombre is not None:
        objetivo.nombre = datos.nombre
    if datos.activo is not None:
        objetivo.activo = datos.activo
    if datos.password:
        objetivo.password_hash = hash_password(datos.password)
    if datos.rol_id is not None:
        rol = db.get(Rol, datos.rol_id)
        if not rol:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El rol indicado no existe")
        objetivo.rol = rol.nombre
        objetivo.rol_id = rol.id

    db.commit()
    db.refresh(objetivo)
    _log_evento(db, admin.id, "edicion_usuario", request, detalle={"usuario_id": objetivo.id, "email": objetivo.email})
    return objetivo


@router.delete("/usuarios/{usuario_id}", status_code=status.HTTP_204_NO_CONTENT)
def desactivar_usuario(
    usuario_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(require_permission("boton:usuarios.eliminar")),
):
    """Baja lógica (activo=False), no se borra el registro — mantiene la auditoría e
    integridad referencial con lotes_procesamiento/eventos_auditoria existentes."""
    if usuario_id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No podés desactivar tu propio usuario")

    objetivo = db.get(Usuario, usuario_id)
    if not objetivo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")

    objetivo.activo = False
    db.commit()
    _log_evento(db, admin.id, "baja_usuario", request, detalle={"usuario_id": objetivo.id, "email": objetivo.email})
