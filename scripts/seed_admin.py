"""Crea el primer usuario administrador si todavía no existe ninguno.

Uso:
    ADMIN_DOCUMENTO=1234567 ADMIN_EMAIL=admin@corvispy.com ADMIN_PASSWORD=xxxxx \
    ADMIN_NOMBRE="Cynthia" .venv/bin/python -m scripts.seed_admin
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.security import hash_password
from app.db.database import SessionLocal
from app.db.models import Rol, Usuario


def main() -> None:
    documento = os.environ.get("ADMIN_DOCUMENTO")
    email = os.environ.get("ADMIN_EMAIL")
    password = os.environ.get("ADMIN_PASSWORD")
    nombre = os.environ.get("ADMIN_NOMBRE", "Administrador")

    if not documento or not email or not password:
        print("Definí ADMIN_DOCUMENTO, ADMIN_EMAIL y ADMIN_PASSWORD como variables de entorno.")
        sys.exit(1)

    db = SessionLocal()
    try:
        if db.query(Usuario).filter(Usuario.nro_documento == documento).first():
            print(f"Ya existe un usuario con documento {documento}.")
            return
        if db.query(Usuario).filter(Usuario.email == email).first():
            print(f"Ya existe un usuario con email {email}.")
            return

        rol_admin = db.query(Rol).filter(Rol.nombre == "admin").first()

        admin = Usuario(
            nombre=nombre,
            nro_documento=documento,
            email=email,
            password_hash=hash_password(password),
            rol="admin",
            rol_id=rol_admin.id if rol_admin else None,
        )
        db.add(admin)
        db.commit()
        print(f"Usuario admin creado: documento {documento} ({email})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
