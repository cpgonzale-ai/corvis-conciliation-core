from fastapi.testclient import TestClient

from app.api.main import app
from app.core import login_limiter
from app.db.database import get_db


class FakeDB:
    def __init__(self, usuario=None):
        self.usuario = usuario

    def query(self, *_):
        return self

    def filter(self, *_):
        return self

    def first(self):
        return self.usuario


def _cliente(usuario=None):
    app.dependency_overrides[get_db] = lambda: FakeDB(usuario)
    return TestClient(app)


def setup_function():
    login_limiter._fallos.clear()


def teardown_function():
    app.dependency_overrides.clear()


def test_bloquea_tras_5_fallos_y_responde_429():
    cliente = _cliente(None)
    for _ in range(5):
        r = cliente.post("/api/auth/login", data={"username": "123", "password": "x"})
        assert r.status_code == 401
    r = cliente.post("/api/auth/login", data={"username": "123", "password": "x"})
    assert r.status_code == 429


def test_el_bloqueo_no_depende_solo_del_documento_sino_de_la_ip():
    cliente = _cliente(None)
    for i in range(5):
        cliente.post("/api/auth/login", data={"username": f"doc{i}", "password": "x"})
    r = cliente.post("/api/auth/login", data={"username": "otro", "password": "x"})
    assert r.status_code == 429
