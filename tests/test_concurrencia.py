import asyncio

import pytest
from fastapi import HTTPException

from app.core import concurrencia


@pytest.fixture(autouse=True)
def limite_fijo(monkeypatch):
    monkeypatch.setattr(concurrencia.settings, "max_operaciones_pesadas_concurrentes", 2)
    monkeypatch.setattr(concurrencia, "_en_vuelo", 0)


def test_rechaza_con_429_al_superar_el_limite():
    async def caso():
        await concurrencia.adquirir_operacion_pesada()
        await concurrencia.adquirir_operacion_pesada()
        with pytest.raises(HTTPException) as e:
            await concurrencia.adquirir_operacion_pesada()
        assert e.value.status_code == 429
    asyncio.run(caso())


def test_liberar_un_slot_permite_volver_a_entrar():
    async def caso():
        await concurrencia.adquirir_operacion_pesada()
        await concurrencia.adquirir_operacion_pesada()
        concurrencia.liberar_operacion_pesada()
        await concurrencia.adquirir_operacion_pesada()
    asyncio.run(caso())
