"""SQLAlchemy models — ver docs/modelo_datos.md para el diseño y las razones.

Nota: por diseño no existe ningún modelo que persista comprobantes fila por
fila (doc, ruc, nombre, montos). Esos datos viven solo en memoria durante la
sesión activa (ver app/core/engine.py). Aquí solo se guardan metadatos y
agregados de auditoría.
"""

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String, Boolean, Integer, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    rol: Mapped[str] = mapped_column(String(20), nullable=False, default="operador")
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    lotes: Mapped[list["LoteProcesamiento"]] = relationship(back_populates="usuario")
    eventos: Mapped[list["EventoAuditoria"]] = relationship(back_populates="usuario")


class LoteProcesamiento(Base):
    __tablename__ = "lotes_procesamiento"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), nullable=False)
    tipo_libro: Mapped[str] = mapped_column(String(10), nullable=False)  # venta | compra
    sistema_origen: Mapped[str] = mapped_column(String(20), nullable=False)  # aloha | hiopos | universal | mixto
    cantidad_comprobantes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cantidad_saltos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="cargado")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    usuario: Mapped["Usuario"] = relationship(back_populates="lotes")
    archivos: Mapped[list["ArchivoProcesado"]] = relationship(back_populates="lote", cascade="all, delete-orphan")
    resultados_rg90: Mapped[list["ResultadoRG90"]] = relationship(back_populates="lote", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_lotes_usuario_created", "usuario_id", "created_at"),)


class ArchivoProcesado(Base):
    __tablename__ = "archivos_procesados"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lote_id: Mapped[int] = mapped_column(ForeignKey("lotes_procesamiento.id"), nullable=False)
    nombre_archivo: Mapped[str] = mapped_column(String(255), nullable=False)
    perfil: Mapped[str] = mapped_column(String(30), nullable=False)
    fecha_carga: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    lote: Mapped["LoteProcesamiento"] = relationship(back_populates="archivos")


class ResultadoRG90(Base):
    __tablename__ = "resultados_rg90"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lote_id: Mapped[int] = mapped_column(ForeignKey("lotes_procesamiento.id"), nullable=False)
    archivo_rg90_nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    total_coinciden: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_no_en_rg90: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_no_en_libro: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_saltos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fecha_comparacion: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    lote: Mapped["LoteProcesamiento"] = relationship(back_populates="resultados_rg90")


class EventoAuditoria(Base):
    __tablename__ = "eventos_auditoria"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), nullable=False)
    accion: Mapped[str] = mapped_column(String(30), nullable=False)
    lote_id: Mapped[int | None] = mapped_column(ForeignKey("lotes_procesamiento.id"), nullable=True)
    detalle: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ip_origen: Mapped[str | None] = mapped_column(String(45), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    usuario: Mapped["Usuario"] = relationship(back_populates="eventos")

    __table_args__ = (
        Index("ix_eventos_usuario_timestamp", "usuario_id", "timestamp"),
        Index("ix_eventos_accion_timestamp", "accion", "timestamp"),
    )
