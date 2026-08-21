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
    rol: str = Field(default="operador", pattern="^(admin|operador)$")


class UsuarioOut(BaseModel):
    id: int
    nombre: str
    email: EmailStr
    rol: str
    activo: bool
    created_at: datetime

    model_config = {"from_attributes": True}
