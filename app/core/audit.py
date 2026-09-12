"""Registro de auditoría (tabla eventos_auditoria) — un único punto compartido por todos
los routers, en vez de que cada uno tenga su propia copia de la misma función (main.py,
compras.py, locales.py, roles.py y auth.py la tenían duplicada, byte por byte salvo por
lote_id, que solo aplica a los routers de conciliación)."""

from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session

from app.db.models import EventoAuditoria


def log_evento(
    db: Session,
    usuario_id: int,
    accion: str,
    request: Optional[Request] = None,
    lote_id: Optional[int] = None,
    detalle: Optional[dict] = None,
) -> None:
    db.add(EventoAuditoria(
        usuario_id=usuario_id,
        accion=accion,
        lote_id=lote_id,
        detalle=detalle,
        ip_origen=request.client.host if request and request.client else None,
    ))
    db.commit()
