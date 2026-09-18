"""Guardado seguro de archivos subidos por el cliente — ver hallazgo CRÍTICO #1 de
/auditoria/02-seguridad-datos.md: usar file.filename directo en os.path.join permitía
escritura fuera del directorio temporal (si el nombre era una ruta absoluta, os.path.join
descartaba tmp_dir y devolvía esa ruta tal cual).

El nombre de archivo del cliente nunca se usa para construir una ruta en disco: se genera
un nombre propio del lado servidor (uuid4 + extensión validada), y se verifica que la ruta
final resuelta quede realmente dentro de tmp_dir antes de escribir."""

import os
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

EXTENSIONES_PERMITIDAS = {".xls", ".xlsx"}
TAMANO_MAXIMO_BYTES = 60 * 1024 * 1024  # 60 MB por archivo


async def guardar_archivo_seguro(upload_file: UploadFile, tmp_dir: str) -> str:
    """Valida extensión y tamaño, y guarda upload_file dentro de tmp_dir con un nombre
    generado del lado servidor. Devuelve la ruta final (string) para pasarle a
    engine.ingest_file. Lanza HTTPException (422/413) si la validación falla."""
    ext = os.path.splitext(upload_file.filename or "")[1].lower()
    if ext not in EXTENSIONES_PERMITIDAS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Extensión de archivo no permitida: '{ext or '(sin extensión)'}' "
                f"({upload_file.filename!r}). Se aceptan: {', '.join(sorted(EXTENSIONES_PERMITIDAS))}"
            ),
        )

    contenido = await upload_file.read()
    if len(contenido) > TAMANO_MAXIMO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"El archivo '{upload_file.filename}' supera el tamaño máximo permitido "
                f"({TAMANO_MAXIMO_BYTES // (1024 * 1024)} MB)."
            ),
        )

    tmp_dir_resuelto = Path(tmp_dir).resolve()
    destino = (tmp_dir_resuelto / f"{uuid.uuid4().hex}{ext}").resolve()
    # El nombre es un uuid generado acá, así que esto no debería poder fallar nunca — se
    # verifica igual, de forma explícita, en vez de asumir que alcanza con construirlo bien.
    if destino.parent != tmp_dir_resuelto:
        raise HTTPException(status_code=500, detail="Ruta de archivo temporal inválida.")

    destino.write_bytes(contenido)
    return str(destino)
