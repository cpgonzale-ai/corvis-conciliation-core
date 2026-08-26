import re
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

# Estándar mínimo de contraseña (alta de usuario y reseteo): 8+ caracteres, al menos una
# mayúscula, una minúscula, un número y un carácter especial.
_PASSWORD_PATTERN = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9]).{8,}$')
_PASSWORD_MSG = "La contraseña debe tener al menos 8 caracteres, con mayúscula, minúscula, número y un carácter especial."


def _validar_password(v: str) -> str:
    if not _PASSWORD_PATTERN.match(v):
        raise ValueError(_PASSWORD_MSG)
    return v


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    rol: str


class UsuarioCreate(BaseModel):
    nombre: str = Field(min_length=1, max_length=150)
    nro_documento: str = Field(min_length=1, max_length=20)
    email: EmailStr
    password: str = Field(min_length=8)
    rol_id: int
    activo: bool = True

    _validar_password = field_validator("password")(_validar_password)


class UsuarioUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=150)
    nro_documento: str | None = Field(default=None, min_length=1, max_length=20)
    rol_id: int | None = None
    activo: bool | None = None
    password: str | None = Field(default=None, min_length=8)

    @field_validator("password")
    @classmethod
    def _validar_password_opcional(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validar_password(v)


class UsuarioOut(BaseModel):
    id: int
    nombre: str
    nro_documento: str
    email: EmailStr
    rol: str
    rol_id: int | None
    activo: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class MeOut(UsuarioOut):
    permisos: list[str] = []


class PermisoOut(BaseModel):
    id: int
    clave: str
    nombre: str
    tipo: str
    pantalla: str

    model_config = {"from_attributes": True}


class RolOut(BaseModel):
    id: int
    nombre: str
    descripcion: str | None
    es_sistema: bool
    estado: str
    permisos: list[str] = []

    model_config = {"from_attributes": True}


class RolCreate(BaseModel):
    nombre: str = Field(min_length=1, max_length=50)
    descripcion: str | None = Field(default=None, max_length=255)
    estado: str = Field(default="activo", pattern="^(activo|inactivo)$")
    permisos: list[str] = []


class RolUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=50)
    descripcion: str | None = Field(default=None, max_length=255)
    estado: str | None = Field(default=None, pattern="^(activo|inactivo)$")
    permisos: list[str] | None = None  # si viene, reemplaza el conjunto completo de permisos


class LocalCreate(BaseModel):
    nombre: str = Field(min_length=1, max_length=150)
    establecimiento: str | None = Field(default=None, max_length=10)
    punto_expedicion: str | None = Field(default=None, max_length=10)
    codigo: str | None = Field(default=None, max_length=30)
    abreviatura: str | None = Field(default=None, max_length=20)
    estado: str = Field(default="activo", pattern="^(activo|inactivo)$")


class LocalUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=150)
    establecimiento: str | None = Field(default=None, max_length=10)
    punto_expedicion: str | None = Field(default=None, max_length=10)
    codigo: str | None = Field(default=None, max_length=30)
    abreviatura: str | None = Field(default=None, max_length=20)
    estado: str | None = Field(default=None, pattern="^(activo|inactivo)$")


class LocalOut(BaseModel):
    id: int
    nombre: str
    establecimiento: str | None
    punto_expedicion: str | None
    codigo: str | None
    abreviatura: str | None
    estado: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
