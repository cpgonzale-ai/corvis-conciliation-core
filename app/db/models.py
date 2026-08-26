"""SQLAlchemy models — ver docs/modelo_datos.md para el diseño y las razones.

Nota: por diseño no existe ningún modelo que persista comprobantes fila por
fila (doc, ruc, nombre, montos). Esos datos viven solo en memoria durante la
sesión activa (ver app/core/engine.py). Aquí solo se guardan metadatos y
agregados de auditoría.
"""

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String, Boolean, Integer, DateTime, Index, Table, Column, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Tabla puente rol <-> permiso (roles y permisos dinámicos, ver Rol/Permiso más abajo).
rol_permisos = Table(
    "rol_permisos",
    Base.metadata,
    Column("rol_id", ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permiso_id", ForeignKey("permisos.id", ondelete="CASCADE"), primary_key=True),
)


class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(150), nullable=False)
    # Nombre de usuario para el login (ver docs/analisis_funcional.md CU-01) — ya no el
    # email, que se mantiene solo como dato de contacto.
    nro_documento: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Se mantiene como string (además de rol_id) para no romper el JWT ni el resto del
    # código existente que ya compara usuario.rol == "admin" (ej. require_admin). Se
    # mantiene sincronizado con roles.nombre cada vez que se asigna un rol_id.
    rol: Mapped[str] = mapped_column(String(20), nullable=False, default="operador")
    rol_id: Mapped[int | None] = mapped_column(ForeignKey("roles.id"), nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    lotes: Mapped[list["LoteProcesamiento"]] = relationship(back_populates="usuario")
    eventos: Mapped[list["EventoAuditoria"]] = relationship(back_populates="usuario")
    rol_obj: Mapped["Rol | None"] = relationship(back_populates="usuarios")


class Rol(Base):
    """Rol dinámico: se crea/edita desde la pantalla de administración de permisos, no está
    fijo en código (salvo 'admin' y 'operador', sembrados como roles de sistema)."""
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    descripcion: Mapped[str | None] = mapped_column(String(255), nullable=True)
    es_sistema: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    estado: Mapped[str] = mapped_column(String(10), nullable=False, default="activo")  # activo | inactivo
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    usuarios: Mapped[list["Usuario"]] = relationship(back_populates="rol_obj")
    permisos: Mapped[list["Permiso"]] = relationship(secondary=rol_permisos, back_populates="roles")


class Permiso(Base):
    """Un permiso representa una pantalla o un botón protegible. `clave` es el identificador
    estable que usa el backend (require_permission) y el frontend (para mostrar/ocultar) —
    ej. 'pantalla:locales', 'boton:locales.eliminar'."""
    __tablename__ = "permisos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clave: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    nombre: Mapped[str] = mapped_column(String(150), nullable=False)
    tipo: Mapped[str] = mapped_column(String(10), nullable=False)  # 'pantalla' | 'boton'
    pantalla: Mapped[str] = mapped_column(String(50), nullable=False)  # agrupa botones bajo su pantalla

    roles: Mapped[list["Rol"]] = relationship(secondary=rol_permisos, back_populates="permisos")


class Local(Base):
    """Locales/puntos de venta del cliente, administrados desde la pantalla de Locales.
    Un comprobante de ventas numera como Establecimiento-PuntoExpedición-Número (ej. '030'
    y '001' en 030-001-0017598) — la combinación de las dos primeras columnas identifica a
    qué local corresponde cada comprobante del libro de ventas (Paso 2); un mismo
    establecimiento puede tener más de un punto de expedición (y viceversa: un mismo
    punto de expedición se repite entre establecimientos distintos), por eso son dos
    columnas separadas y la unicidad es sobre el par, no sobre cada una por sí sola.
    Ninguna de las dos aplica a los locales "de marca" que usa Compras (ver `codigo`) —
    quedan en NULL para esas filas."""
    __tablename__ = "locales"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(150), nullable=False)
    establecimiento: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    punto_expedicion: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    codigo: Mapped[str | None] = mapped_column(String(30), nullable=True, unique=True)
    # Abreviatura de referencia (ej. "JV" para Juan Valdez, ver Minuta 5 — códigos de
    # sucursal de compras) — solo informativa, no se usa en ninguna validación ni matching.
    abreviatura: Mapped[str | None] = mapped_column(String(20), nullable=True)
    estado: Mapped[str] = mapped_column(String(10), nullable=False, default="activo")  # activo | inactivo
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("establecimiento", "punto_expedicion", name="uq_locales_establecimiento_punto_expedicion"),
    )


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
