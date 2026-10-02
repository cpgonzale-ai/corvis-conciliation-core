"""agregar indices en rol_id y lote_id (FKs sin indexar)

Revision ID: 4493b3d176cb
Revises: d3ec9161e9da
Create Date: 2026-10-02 05:44:02.372161

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4493b3d176cb'
down_revision: Union[str, Sequence[str], None] = 'd3ec9161e9da'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Solo los 3 índices de FK que faltaban (Pilar 4 de la auditoría del 02/10: lote_id en
    # archivos_procesados/resultados_rg90, rol_id en usuarios -- deuda latente, sin consultas
    # activas que los necesiten hoy, pero de costo cero agregarlos). --autogenerate también
    # detectó un drift preexistente y no relacionado en usuarios.nro_documento (constraint
    # UNIQUE vs. índice UNIQUE -- mismo efecto práctico, declarado distinto en el modelo vs.
    # la base real) -- se deja afuera de este migration a propósito, no es parte de este
    # hallazgo y tocarlo implica un drop+create sobre una columna con datos reales.
    op.create_index(op.f('ix_archivos_procesados_lote_id'), 'archivos_procesados', ['lote_id'], unique=False)
    op.create_index(op.f('ix_resultados_rg90_lote_id'), 'resultados_rg90', ['lote_id'], unique=False)
    op.create_index(op.f('ix_usuarios_rol_id'), 'usuarios', ['rol_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_usuarios_rol_id'), table_name='usuarios')
    op.drop_index(op.f('ix_resultados_rg90_lote_id'), table_name='resultados_rg90')
    op.drop_index(op.f('ix_archivos_procesados_lote_id'), table_name='archivos_procesados')
