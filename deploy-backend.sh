#!/bin/bash
# Despliega el backend a /opt/siscom/backend (fuera del home, ver
# auditoria/altos-pendientes-sudo.md sección 3) y reinicia el servicio.
# Requiere sudo: /opt/siscom es root:root, y el servicio corre como usuario `siscom`.

set -e

PROJECT_DIR="/home/cpgonzalez/Documents/CSM_PWCLC/corvis-conciliation-core"
TARGET_DIR="/opt/siscom/backend"
VENV_DIR="/opt/siscom/venv"

echo "=========================================="
echo "   Desplegando backend SISCOM (desarrollo) "
echo "=========================================="

echo "1. Sincronizando código ($PROJECT_DIR -> $TARGET_DIR)..."
# --delete para que los archivos borrados en git no queden colgados en destino.
# Se excluye todo lo que es solo de este checkout de desarrollo o del entorno ya
# instalado en destino: nunca se toca el .env real de /opt/siscom/backend.
rsync -a --delete \
  --exclude ".venv" --exclude ".git" --exclude "__pycache__" \
  --exclude ".env" --exclude ".env.*" \
  "$PROJECT_DIR"/ "$TARGET_DIR"/

echo "2. Ajustando dueño (siscom:siscom)..."
chown -R siscom:siscom "$TARGET_DIR"

echo "3. Instalando dependencias (requirements.txt)..."
"$VENV_DIR"/bin/pip install -q -r "$TARGET_DIR"/requirements.txt

echo "4. Reiniciando el servicio..."
systemctl restart siscom-backend

sleep 2
echo "5. Verificando..."
curl -s -o /dev/null -w "health: %{http_code}\n" http://127.0.0.1:8090/api/health

echo "=========================================="
echo "   ¡Despliegue de backend completado!      "
echo "=========================================="
