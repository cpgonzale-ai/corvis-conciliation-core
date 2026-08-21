"""Router de autenticación y gestión de usuarios (CU-01)."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_admin
from app.core.security import create_access_token, hash_password, verify_password
from app.db.database import get_db
from app.db.models import EventoAuditoria, Usuario
from app.schemas.auth import TokenResponse, UsuarioCreate, UsuarioOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _log_evento(db: Session, usuario_id: int, accion: str, request: Request | None = None, detalle: dict | None = None):
    evento = EventoAuditoria(
        usuario_id=usuario_id,
        accion=accion,
        detalle=detalle,
        ip_origen=request.client.host if request and request.client else None,
    )
    db.add(evento)
    db.commit()


@router.post("/login", response_model=TokenResponse)
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    usuario = db.query(Usuario).filter(Usuario.email == form_data.username).first()
    if not usuario or not usuario.activo or not verify_password(form_data.password, usuario.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales inválidas")

    token = create_access_token(subject=usuario.email, rol=usuario.rol)
    _log_evento(db, usuario.id, "login", request)
    return TokenResponse(access_token=token, rol=usuario.rol)


@router.get("/me", response_model=UsuarioOut)
def me(usuario: Usuario = Depends(get_current_user)):
    return usuario


@router.post("/usuarios", response_model=UsuarioOut, status_code=status.HTTP_201_CREATED)
def crear_usuario(
    datos: UsuarioCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: Usuario = Depends(require_admin),
):
    if db.query(Usuario).filter(Usuario.email == datos.email).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="El email ya está registrado")

    nuevo = Usuario(
        nombre=datos.nombre,
        email=datos.email,
        password_hash=hash_password(datos.password),
        rol=datos.rol,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    _log_evento(db, admin.id, "alta_usuario", request, detalle={"usuario_creado": nuevo.email})
    return nuevo


@router.get("/usuarios", response_model=list[UsuarioOut])
def listar_usuarios(db: Session = Depends(get_db), admin: Usuario = Depends(require_admin)):
    return db.query(Usuario).order_by(Usuario.id).all()
