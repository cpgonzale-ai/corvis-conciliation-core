"""Caché en memoria (por proceso) para el catálogo de roles/permisos.

Decisión de diseño (ver auditoria/11-auditoria-360-completa.md): el catálogo de
roles/permisos cambia poco (se edita a mano, desde la pantalla de administración) pero se
consulta en CADA pedido autenticado (get_current_user, require_permission) y en cada listado
de la pantalla de Roles — exactamente el patrón que conviene cachear con TTL en vez de ir a
la base de datos siempre.

Sin Redis a propósito (para no sumarle infraestructura a la VPS): el caché es un
cachetools.TTLCache en memoria del propio proceso. Con --workers 2 en producción, cada
worker tiene su PROPIA copia — invalidar el caché en el worker que atendió un
crear_rol/editar_rol/eliminar_rol no le llega al otro worker, que puede seguir sirviendo
datos viejos hasta que le venza el TTL. Por eso el TTL elegido es el extremo más corto del
rango pedido (5 minutos, no 15): acota esa ventana de inconsistencia entre workers a un
máximo razonable sin necesidad de un caché compartido.

cachetools.TTLCache no es thread-safe por sí solo (así lo documenta la propia librería) --
FastAPI corre las dependencias sync (get_current_user, require_permission tal como están
escritas hoy) en un threadpool, así que SÍ puede haber más de un thread tocando este caché
al mismo tiempo dentro de un mismo worker. Se protege con un threading.Lock.
"""

import threading
from typing import Any, Callable, Optional

from cachetools import TTLCache

_LOCK = threading.Lock()
_TTL_SEGUNDOS = 300  # 5 minutos

_cache_permisos_catalogo: TTLCache = TTLCache(maxsize=1, ttl=_TTL_SEGUNDOS)
_cache_roles_listado: TTLCache = TTLCache(maxsize=1, ttl=_TTL_SEGUNDOS)
_cache_rol_por_id: TTLCache = TTLCache(maxsize=256, ttl=_TTL_SEGUNDOS)


def _get_or_set(cache: TTLCache, key: Any, fabricar: Callable[[], Any]) -> Any:
    with _LOCK:
        if key in cache:
            return cache[key]
    # fabricar() corre FUERA del lock (puede tardar por la consulta a la BD) -- si dos
    # threads pisan un miss al mismo tiempo, en el peor caso se hace la consulta dos veces
    # en vez de una (no rompe nada, solo un miss redundante ocasional en el caso frío).
    valor = fabricar()
    with _LOCK:
        cache[key] = valor
    return valor


def invalidar_todo() -> None:
    """Se llama después de cualquier commit que cambie roles o sus permisos asignados
    (crear_rol/editar_rol/eliminar_rol) -- solo afecta al worker que atendió ese pedido, ver
    docstring del módulo."""
    with _LOCK:
        _cache_permisos_catalogo.clear()
        _cache_roles_listado.clear()
        _cache_rol_por_id.clear()


def permisos_catalogo_rows(db) -> list[tuple[int, str, str, str, str]]:
    """(id, clave, nombre, tipo, pantalla) de cada permiso del catálogo, cacheado."""
    from app.db.models import Permiso

    def _cargar():
        filas = db.query(Permiso).order_by(Permiso.pantalla, Permiso.tipo, Permiso.clave).all()
        return [(p.id, p.clave, p.nombre, p.tipo, p.pantalla) for p in filas]

    return _get_or_set(_cache_permisos_catalogo, "catalogo", _cargar)


def roles_listado_rows(db) -> list[dict]:
    """Un dict por rol (mismos campos que RolOut, permisos ya como lista de claves), cacheado."""
    from sqlalchemy.orm import selectinload

    from app.db.models import Rol

    def _cargar():
        roles = db.query(Rol).options(selectinload(Rol.permisos)).order_by(Rol.nombre).all()
        return [
            {
                "id": r.id, "nombre": r.nombre, "descripcion": r.descripcion,
                "es_sistema": r.es_sistema, "estado": r.estado,
                "permisos": [p.clave for p in r.permisos],
            }
            for r in roles
        ]

    return _get_or_set(_cache_roles_listado, "listado", _cargar)


def rol_info(db, rol_id: int) -> Optional[dict]:
    """{"estado": ..., "permisos": frozenset(claves)} para un rol_id puntual, cacheado --
    esto es lo que reemplaza a usuario.rol_obj.permisos (get_current_user/require_permission/
    /me), la consulta más repetida de toda la API (se resuelve en cada pedido autenticado)."""
    from app.db.models import Rol

    def _cargar():
        rol = db.get(Rol, rol_id)
        if rol is None:
            return None
        return {"estado": rol.estado, "permisos": frozenset(p.clave for p in rol.permisos)}

    return _get_or_set(_cache_rol_por_id, rol_id, _cargar)
