"""El total de saltos de numeración son los números FALTANTES, no la cantidad de tramos."""

from app.core.engine import detect_sequence_gaps, total_faltantes
from tests.fabricas import fila_ventas as v


def test_suma_faltantes_no_tramos():
    # 0000001 y 0000002 ok; faltan 3 y 4 (tramo de 2); 0000005 ok; faltan 6, 7 y 8 (tramo de 3).
    filas = [
        v(100, doc="052-001-0000001"),
        v(100, doc="052-001-0000002"),
        v(100, doc="052-001-0000005"),
        v(100, doc="052-001-0000009"),
    ]
    gaps = detect_sequence_gaps(filas)
    assert len(gaps) == 2                 # dos tramos
    assert total_faltantes(gaps) == 5     # cinco números faltantes


def test_sin_saltos_da_cero():
    filas = [v(100, doc="052-001-0000001"), v(100, doc="052-001-0000002")]
    assert total_faltantes(detect_sequence_gaps(filas)) == 0
