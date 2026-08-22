"""unicidad punto_expedicion/codigo en locales

Revision ID: 2364c9a7bc9e
Revises: 430a01f1509f
Create Date: 2026-08-22 10:30:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '2364c9a7bc9e'
down_revision: Union[str, Sequence[str], None] = '430a01f1509f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # UNIQUE en Postgres no choca entre múltiples NULLs — código puede seguir quedando
    # vacío en varios locales, solo se exige unicidad cuando tiene valor.
    op.create_unique_constraint('uq_locales_punto_expedicion', 'locales', ['punto_expedicion'])
    op.create_unique_constraint('uq_locales_codigo', 'locales', ['codigo'])


def downgrade() -> None:
    op.drop_constraint('uq_locales_codigo', 'locales', type_='unique')
    op.drop_constraint('uq_locales_punto_expedicion', 'locales', type_='unique')
