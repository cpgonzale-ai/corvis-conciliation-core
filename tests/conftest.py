import os

# La app no arranca con la clave JWT de ejemplo (app/core/config.py). Para tests se fija una
# clave de prueba antes de importar nada de la app.
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-no-usar-en-produccion")
