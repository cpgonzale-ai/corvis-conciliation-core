"""Límite de intentos fallidos de login, por número de documento y por IP.

Es en memoria y por proceso worker: con --workers 2 el límite efectivo es hasta el doble
de MAX_FALLOS. Es suficiente para frenar fuerza bruta desde un único origen; un límite
global exigiría un almacén compartido (Redis o la base).
"""

import time
from collections import defaultdict, deque

MAX_FALLOS = 5
VENTANA_SEGUNDOS = 15 * 60

_fallos: dict[str, deque] = defaultdict(deque)


def _purgar(clave: str, ahora: float) -> deque:
    intentos = _fallos[clave]
    while intentos and ahora - intentos[0] > VENTANA_SEGUNDOS:
        intentos.popleft()
    return intentos


def bloqueado(clave: str) -> bool:
    return len(_purgar(clave, time.monotonic())) >= MAX_FALLOS


def registrar_fallo(clave: str) -> None:
    _fallos[clave].append(time.monotonic())


def limpiar(clave: str) -> None:
    _fallos.pop(clave, None)
