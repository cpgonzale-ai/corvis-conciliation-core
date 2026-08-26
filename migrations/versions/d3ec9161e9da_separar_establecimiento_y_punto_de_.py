"""separar establecimiento y punto de expedicion

Revision ID: d3ec9161e9da
Revises: f966ba61ef40
Create Date: 2026-08-26 12:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3ec9161e9da'
down_revision: Union[str, Sequence[str], None] = 'f966ba61ef40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Hasta ahora locales.punto_expedicion guardaba "EEE-PPP" (establecimiento y punto de
    # expedición) pegados en un solo campo — el usuario pidió que sean dos columnas
    # separadas, como corresponde a la numeración real del SET (Establecimiento-Punto de
    # Expedición-Número).
    op.add_column('locales', sa.Column('establecimiento', sa.String(length=10), nullable=True))
    # Hay que aflojar el NOT NULL y sacar la UNIQUE de una sola columna ANTES de correr los
    # UPDATE de mas abajo: van a repetir valores de punto_expedicion entre si (ej. "001" va
    # a quedar en varias filas), lo que la vieja constraint de una sola columna no permite.
    op.alter_column('locales', 'punto_expedicion', existing_type=sa.String(length=10), nullable=True)
    op.drop_constraint('uq_locales_punto_expedicion', 'locales', type_='unique')
    op.drop_index('ix_locales_punto_expedicion', table_name='locales')

    conn = op.get_bind()
    # Los 5 locales de marca usados en Compras (LA CABRERA, JUAN VALDEZ, etc.) tenían un
    # placeholder ('COMPRAS-N') en punto_expedicion que no representa ningún establecimiento
    # real de ventas — se limpia a NULL en los dos campos; esas filas se identifican
    # únicamente por su `codigo`.
    conn.execute(sa.text("""
        UPDATE locales SET establecimiento = NULL, punto_expedicion = NULL
        WHERE punto_expedicion LIKE 'COMPRAS-%'
    """))
    # El resto son locales reales de ventas con "EEE-PPP" pegado — se separa en las dos
    # columnas nuevas.
    conn.execute(sa.text("""
        UPDATE locales
        SET establecimiento = split_part(punto_expedicion, '-', 1),
            punto_expedicion = split_part(punto_expedicion, '-', 2)
        WHERE punto_expedicion IS NOT NULL AND punto_expedicion LIKE '%-%'
    """))

    # La unicidad ahora es sobre el PAR (establecimiento, punto_expedicion) — punto_expedicion
    # solo (ej. "001") es normal que se repita entre establecimientos distintos (023-001,
    # 024-001, ...). Nulo en ambos (las filas de marca) no choca entre sí: Postgres no
    # considera iguales dos NULL en una constraint UNIQUE.
    op.create_index('ix_locales_establecimiento', 'locales', ['establecimiento'])
    op.create_index('ix_locales_punto_expedicion', 'locales', ['punto_expedicion'])
    op.create_unique_constraint(
        'uq_locales_establecimiento_punto_expedicion', 'locales', ['establecimiento', 'punto_expedicion']
    )


def downgrade() -> None:
    conn = op.get_bind()
    op.drop_constraint('uq_locales_establecimiento_punto_expedicion', 'locales', type_='unique')
    op.drop_index('ix_locales_establecimiento', table_name='locales')
    op.drop_index('ix_locales_punto_expedicion', table_name='locales')

    conn.execute(sa.text("""
        UPDATE locales SET punto_expedicion = establecimiento || '-' || punto_expedicion
        WHERE establecimiento IS NOT NULL AND punto_expedicion IS NOT NULL
    """))

    op.drop_column('locales', 'establecimiento')
    op.alter_column('locales', 'punto_expedicion', existing_type=sa.String(length=10), nullable=False)
    op.create_index('ix_locales_punto_expedicion', 'locales', ['punto_expedicion'])
    op.create_unique_constraint('uq_locales_punto_expedicion', 'locales', ['punto_expedicion'])
