"""Middleware de infraestructura para la API (no lógica de negocio).

GzipRequestDecompressionMiddleware: descomprime el BODY de un pedido cuando llega con
Content-Encoding: gzip. FastAPI/Starlette ya saben comprimir RESPUESTAS (no se usa acá
GZipMiddleware de Starlette porque ese es justamente para eso, respuestas, no pedidos), pero
no traen nada para el sentido contrario -- un pedido entrante comprimido no se descomprime
solo.

Por qué hace falta: el Detalle de Discrepancias (POST a /api/export/diff-ventas-excel) manda
el JSON completo de la grilla filtrada -- con archivos reales grandes, 41MB para 97.851
filas (cada fila lleva los datos del libro Y de la RG90 anidados, con los mismos nombres de
campo repetidos miles de veces). Esa subida corre por la conexión del propio usuario, no la
del servidor -- medido: comprime a 3,8MB con gzip (~10x), y esa subida es, en la práctica, el
cuello de botella más grande de esta descarga con archivos grandes, más que el tiempo que el
servidor tarda en armar el Excel en sí. El frontend (services/api.ts, postParaDescarga)
comprime con CompressionStream antes de mandar; esto de acá es el otro lado.

Se implementa como middleware ASGI puro (no BaseHTTPMiddleware) envolviendo `receive` -- así
ningún endpoint necesita saber que esto existe, ni FastAPI ni Pydantic ven la diferencia
entre un pedido comprimido y uno que no lo estaba.
"""

import gzip

from starlette.types import ASGIApp, Receive, Scope, Send


class GzipRequestDecompressionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_encoding = next(
            (v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"content-encoding"),
            "",
        )
        if "gzip" not in content_encoding.lower():
            await self.app(scope, receive, send)
            return

        # Los endpoints que reciben esto (export/*, reconcile) ya leen el body COMPLETO
        # antes de procesar nada (Pydantic/request.json()) -- bufferizar acá entero no
        # pierde ningún beneficio de streaming real que ya existiera.
        body = b""
        more_body = True
        while more_body:
            message = await receive()
            body += message.get("body", b"")
            more_body = message.get("more_body", False)

        try:
            body = gzip.decompress(body)
        except OSError:
            # Content-Encoding decía gzip pero el body no lo es de verdad (o está corrupto)
            # -- se deja pasar tal cual para que el parseo normal de más abajo (Pydantic/
            # json) falle con un error claro sobre el pedido en sí, en vez de esconder el
            # problema acá con un 500 genérico de gzip.
            pass

        ya_entregado = False

        async def receive_descomprimido():
            # Solo la PRIMERA llamada entrega el body ya descomprimido. Starlette sigue
            # llamando receive() después de eso -- StreamingResponse lo usa para detectar
            # si el cliente cortó la conexión a mitad del streaming de la respuesta (Excel
            # grande, puede tardar). Devolver acá un "http.disconnect" falso en esa segunda
            # llamada (bug real, encontrado probando esto mismo) le hacía creer a Starlette
            # que el cliente se había ido, y cortaba la respuesta a mitad de camino --
            # "ASGI callable returned without completing response", 0 bytes descargados.
            # Delegar al receive() real de acá en adelante deja que la detección de
            # desconexión genuina siga funcionando igual que si este middleware no existiera.
            nonlocal ya_entregado
            if not ya_entregado:
                ya_entregado = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, receive_descomprimido, send)
