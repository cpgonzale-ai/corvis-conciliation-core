"""nro_documento como usuario de login

Revision ID: 258c62d463fb
Revises: f2ec91bbff65
Create Date: 2026-08-22 12:00:00.000000

El login pasa de usar el email a usar el número de documento. Los usuarios que ya existen
no tienen un documento real cargado en el sistema, así que se les asigna un placeholder
('00000001', '00000002', ...) para no romper el NOT NULL/UNIQUE — hay que reemplazarlo por
el número real desde la pantalla de Usuarios apenas se pueda.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '258c62d463fb'
down_revision: Union[str, Sequence[str], None] = 'f2ec91bbff65'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('usuarios', sa.Column('nro_documento', sa.String(length=20), nullable=True))

    conn = op.get_bind()
    usuarios_tbl = sa.table('usuarios', sa.column('id', sa.Integer), sa.column('nro_documento', sa.String))
    filas = conn.execute(sa.select(usuarios_tbl.c.id).order_by(usuarios_tbl.c.id)).fetchall()
    for fila in filas:
        placeholder = f"{fila.id:08d}"
        conn.execute(usuarios_tbl.update().where(usuarios_tbl.c.id == fila.id).values(nro_documento=placeholder))

    op.alter_column('usuarios', 'nro_documento', nullable=False)
    op.create_unique_constraint('uq_usuarios_nro_documento', 'usuarios', ['nro_documento'])
    op.create_index('ix_usuarios_nro_documento', 'usuarios', ['nro_documento'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_usuarios_nro_documento', table_name='usuarios')
    op.drop_constraint('uq_usuarios_nro_documento', 'usuarios', type_='unique')
    op.drop_column('usuarios', 'nro_documento')
