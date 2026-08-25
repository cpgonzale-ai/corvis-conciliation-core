"""permisos libro de compras

Revision ID: d88c37f2ad02
Revises: 258c62d463fb
Create Date: 2026-08-25 02:40:33.349245

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd88c37f2ad02'
down_revision: Union[str, Sequence[str], None] = '258c62d463fb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Nuevo módulo (Minuta 5): Libro de Compras y su comparación contra la RG. Reutiliza la
# tabla `locales` existente (campo `codigo`, ya presente desde 430a01f1509f) para el mapeo
# de sucursal — no hace falta tabla nueva, solo el catálogo de permisos.
PANTALLAS = [
    ("pantalla:compras", "Carga y libro de compras", "compras"),
]

BOTONES = [
    ("boton:compras.convertir", "Analizar y convertir libro de compras", "compras"),
    ("boton:compras.eliminar_todos", "Eliminar todos los archivos adjuntados", "compras"),
    ("boton:compras.borrar_libro", "Borrar libro de compras", "compras"),
    ("boton:compras.descargar_csv", "Descargar CSV del libro de compras", "compras"),
    ("boton:compras.comparar", "Analizar y comparar contra la RG", "compras"),
    ("boton:compras.quitar_archivo", "Quitar archivo RG de compras", "compras"),
]

# Igual que el resto del uso diario (ver PERMISOS_OPERADOR en 430a01f1509f): operador
# también puede cargar y comparar el libro de compras.
PERMISOS_OPERADOR = {clave for clave, _, _ in PANTALLAS} | {clave for clave, _, _ in BOTONES}


def upgrade() -> None:
    conn = op.get_bind()

    roles_tbl = sa.table('roles', sa.column('id', sa.Integer), sa.column('nombre', sa.String))
    permisos_tbl = sa.table(
        'permisos', sa.column('id', sa.Integer), sa.column('clave', sa.String),
        sa.column('nombre', sa.String), sa.column('tipo', sa.String), sa.column('pantalla', sa.String),
    )
    rol_permisos_tbl = sa.table('rol_permisos', sa.column('rol_id', sa.Integer), sa.column('permiso_id', sa.Integer))

    admin_id = conn.execute(sa.select(roles_tbl.c.id).where(roles_tbl.c.nombre == 'admin')).scalar()
    operador_id = conn.execute(sa.select(roles_tbl.c.id).where(roles_tbl.c.nombre == 'operador')).scalar()

    clave_a_id = {}
    for clave, nombre, pantalla in PANTALLAS:
        pid = conn.execute(
            permisos_tbl.insert().values(clave=clave, nombre=nombre, tipo='pantalla', pantalla=pantalla).returning(permisos_tbl.c.id)
        ).scalar()
        clave_a_id[clave] = pid
    for clave, nombre, pantalla in BOTONES:
        pid = conn.execute(
            permisos_tbl.insert().values(clave=clave, nombre=nombre, tipo='boton', pantalla=pantalla).returning(permisos_tbl.c.id)
        ).scalar()
        clave_a_id[clave] = pid

    if admin_id is not None:
        conn.execute(rol_permisos_tbl.insert(), [{"rol_id": admin_id, "permiso_id": pid} for pid in clave_a_id.values()])
    if operador_id is not None:
        conn.execute(rol_permisos_tbl.insert(), [
            {"rol_id": operador_id, "permiso_id": clave_a_id[clave]} for clave in PERMISOS_OPERADOR
        ])


def downgrade() -> None:
    conn = op.get_bind()
    permisos_tbl = sa.table('permisos', sa.column('id', sa.Integer), sa.column('clave', sa.String))
    claves = [c for c, _, _ in PANTALLAS] + [c for c, _, _ in BOTONES]
    conn.execute(permisos_tbl.delete().where(permisos_tbl.c.clave.in_(claves)))
