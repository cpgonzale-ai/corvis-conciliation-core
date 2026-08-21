"""roles y permisos dinámicos + locales

Revision ID: 430a01f1509f
Revises: 23b646faad6d
Create Date: 2026-08-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '430a01f1509f'
down_revision: Union[str, Sequence[str], None] = '23b646faad6d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Catálogo de permisos: una pantalla o botón por fila. `clave` es el identificador estable
# que usan backend (require_permission) y frontend. No es exhaustivo a nivel de cada
# elemento de UI (ej. no cubre flechas de paginación) — cubre las pantallas y los botones
# con peso real (crear/editar/eliminar, acciones destructivas, navegación entre pantallas).
PANTALLAS = [
    ("pantalla:dashboard", "Panel general", "dashboard"),
    ("pantalla:carga", "Carga y libro de ventas", "carga"),
    ("pantalla:correlatividad", "Control de correlatividad", "correlatividad"),
    ("pantalla:rg90", "Comparación contra RG90", "rg90"),
    ("pantalla:locales", "Administración de Locales", "locales"),
    ("pantalla:usuarios", "Administración de Usuarios", "usuarios"),
    ("pantalla:roles", "Administración de Roles y Permisos", "roles"),
]

BOTONES = [
    ("boton:carga.convertir", "Analizar y convertir", "carga"),
    ("boton:carga.eliminar_todos", "Eliminar todos los archivos adjuntados", "carga"),
    ("boton:carga.borrar_libro", "Borrar libro de ventas", "carga"),
    ("boton:carga.descargar_csv", "Descargar CSV del libro", "carga"),
    ("boton:rg90.comparar", "Analizar y comparar RG90", "rg90"),
    ("boton:rg90.quitar_archivo", "Quitar archivo RG90", "rg90"),
    ("boton:locales.crear", "Crear local", "locales"),
    ("boton:locales.editar", "Editar local", "locales"),
    ("boton:locales.eliminar", "Eliminar local", "locales"),
    ("boton:usuarios.crear", "Crear usuario", "usuarios"),
    ("boton:usuarios.editar", "Editar usuario", "usuarios"),
    ("boton:usuarios.eliminar", "Desactivar usuario", "usuarios"),
    ("boton:roles.crear", "Crear rol", "roles"),
    ("boton:roles.editar", "Editar permisos del rol", "roles"),
    ("boton:roles.eliminar", "Eliminar rol", "roles"),
]

# Lo que ya podía hacer cualquier usuario autenticado (operador incluido) antes de que
# existiera este sistema de permisos — se preserva tal cual para no romper el flujo diario.
PERMISOS_OPERADOR = {
    "pantalla:dashboard", "pantalla:carga", "pantalla:correlatividad", "pantalla:rg90",
    "boton:carga.convertir", "boton:carga.eliminar_todos", "boton:carga.borrar_libro",
    "boton:carga.descargar_csv", "boton:rg90.comparar", "boton:rg90.quitar_archivo",
}


def upgrade() -> None:
    op.create_table(
        'roles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('nombre', sa.String(length=50), nullable=False),
        sa.Column('descripcion', sa.String(length=255), nullable=True),
        sa.Column('es_sistema', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('nombre'),
    )
    op.create_table(
        'permisos',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('clave', sa.String(length=100), nullable=False),
        sa.Column('nombre', sa.String(length=150), nullable=False),
        sa.Column('tipo', sa.String(length=10), nullable=False),
        sa.Column('pantalla', sa.String(length=50), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('clave'),
    )
    op.create_table(
        'rol_permisos',
        sa.Column('rol_id', sa.Integer(), nullable=False),
        sa.Column('permiso_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['rol_id'], ['roles.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['permiso_id'], ['permisos.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('rol_id', 'permiso_id'),
    )
    op.create_table(
        'locales',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('nombre', sa.String(length=150), nullable=False),
        sa.Column('punto_expedicion', sa.String(length=10), nullable=False),
        sa.Column('codigo', sa.String(length=30), nullable=True),
        sa.Column('estado', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_locales_punto_expedicion', 'locales', ['punto_expedicion'], unique=False)

    op.add_column('usuarios', sa.Column('rol_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_usuarios_rol_id', 'usuarios', 'roles', ['rol_id'], ['id'])

    # --- datos semilla ---
    conn = op.get_bind()

    roles_tbl = sa.table(
        'roles', sa.column('id', sa.Integer), sa.column('nombre', sa.String),
        sa.column('descripcion', sa.String), sa.column('es_sistema', sa.Boolean),
        sa.column('created_at', sa.DateTime),
    )
    permisos_tbl = sa.table(
        'permisos', sa.column('id', sa.Integer), sa.column('clave', sa.String),
        sa.column('nombre', sa.String), sa.column('tipo', sa.String), sa.column('pantalla', sa.String),
    )
    rol_permisos_tbl = sa.table('rol_permisos', sa.column('rol_id', sa.Integer), sa.column('permiso_id', sa.Integer))
    usuarios_tbl = sa.table('usuarios', sa.column('id', sa.Integer), sa.column('rol', sa.String), sa.column('rol_id', sa.Integer))

    now = sa.func.now()
    admin_id = conn.execute(
        roles_tbl.insert().values(nombre='admin', descripcion='Acceso total al sistema', es_sistema=True, created_at=now).returning(roles_tbl.c.id)
    ).scalar()
    operador_id = conn.execute(
        roles_tbl.insert().values(nombre='operador', descripcion='Uso diario: carga, conversión, comparación RG90', es_sistema=True, created_at=now).returning(roles_tbl.c.id)
    ).scalar()

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

    # admin: todos los permisos
    conn.execute(rol_permisos_tbl.insert(), [{"rol_id": admin_id, "permiso_id": pid} for pid in clave_a_id.values()])
    # operador: lo que ya podía hacer antes de este sistema
    conn.execute(rol_permisos_tbl.insert(), [
        {"rol_id": operador_id, "permiso_id": clave_a_id[clave]} for clave in PERMISOS_OPERADOR
    ])

    # backfill: usuarios existentes con rol='admin'/'operador' (string) -> rol_id correspondiente
    conn.execute(usuarios_tbl.update().where(usuarios_tbl.c.rol == 'admin').values(rol_id=admin_id))
    conn.execute(usuarios_tbl.update().where(usuarios_tbl.c.rol == 'operador').values(rol_id=operador_id))


def downgrade() -> None:
    op.drop_constraint('fk_usuarios_rol_id', 'usuarios', type_='foreignkey')
    op.drop_column('usuarios', 'rol_id')
    op.drop_index('ix_locales_punto_expedicion', table_name='locales')
    op.drop_table('locales')
    op.drop_table('rol_permisos')
    op.drop_table('permisos')
    op.drop_table('roles')
