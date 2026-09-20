"""SQLAlchemy engine/session setup."""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings

# Tamaño de pool por defecto de SQLAlchemy (pool_size=5, max_overflow=10 -> 15 conexiones
# simultáneas para todo el proceso) medido como la causa raíz de un colapso real: 100
# peticiones concurrentes a endpoints de lectura simples devolvían 80% de errores 500
# (sqlalchemy.exc.TimeoutError: QueuePool limit... connection timed out) — ver
# auditoria/11-auditoria-360-completa.md, Fase 4. pool_pre_ping ya estaba (recicla
# conexiones muertas); se suman los tamaños explícitos para soportar la concurrencia real
# medida sin agotar el pool.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=30,
    pool_timeout=30,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
