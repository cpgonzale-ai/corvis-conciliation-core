"""abreviatura en locales

Revision ID: f966ba61ef40
Revises: d88c37f2ad02
Create Date: 2026-08-26 11:42:19.882718

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f966ba61ef40'
down_revision: Union[str, Sequence[str], None] = 'd88c37f2ad02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # La lista de códigos de sucursal de compras (Minuta 5) trae una abreviatura por marca
    # (ej. "JV" para Juan Valdez) que no tenía dónde guardarse — se agrega como columna
    # opcional, no se usa para nada funcional todavía (ni unicidad ni matching), es dato de
    # referencia.
    op.add_column('locales', sa.Column('abreviatura', sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column('locales', 'abreviatura')
