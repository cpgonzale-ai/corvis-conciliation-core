"""estado (activo/inactivo) en roles

Revision ID: f2ec91bbff65
Revises: 2364c9a7bc9e
Create Date: 2026-08-22 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f2ec91bbff65'
down_revision: Union[str, Sequence[str], None] = '2364c9a7bc9e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('roles', sa.Column('estado', sa.String(length=10), nullable=False, server_default='activo'))
    op.alter_column('roles', 'estado', server_default=None)


def downgrade() -> None:
    op.drop_column('roles', 'estado')
