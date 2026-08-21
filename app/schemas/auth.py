from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    rol: str


class UsuarioCreate(BaseModel):
    nombre: str = Field(min_length=1, max_length=150)
    email: EmailStr
    password: str = Field(min_length=8)
    rol_id: int


class UsuarioUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=150)
    rol_id: int | None = None
    activo: bool | None = None
    password: str | None = Field(default=None, min_length=8)


class UsuarioOut(BaseModel):
    id: int
    nombre: str
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
    permisos: list[str] = []

    model_config = {"from_attributes": True}


class RolCreate(BaseModel):
    nombre: str = Field(min_length=1, max_length=50)
    descripcion: str | None = Field(default=None, max_length=255)
    permisos: list[str] = []


class RolUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=50)
    descripcion: str | None = Field(default=None, max_length=255)
    permisos: list[str] | None = None  # si viene, reemplaza el conjunto completo de permisos


class LocalCreate(BaseModel):
    nombre: str = Field(min_length=1, max_length=150)
    punto_expedicion: str = Field(min_length=1, max_length=10)
    codigo: str | None = Field(default=None, max_length=30)
    estado: str = Field(default="activo", pattern="^(activo|inactivo)$")


class LocalUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=150)
    punto_expedicion: str | None = Field(default=None, min_length=1, max_length=10)
    codigo: str | None = Field(default=None, max_length=30)
    estado: str | None = Field(default=None, pattern="^(activo|inactivo)$")


class LocalOut(BaseModel):
    id: int
    nombre: str
    punto_expedicion: str
    codigo: str | None
    estado: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
