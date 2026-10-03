"""Límite de concurrencia para los 4 endpoints pesados del sistema: /api/ingest,
/api/reconcile (Ventas) y /api/compras/ingest, /api/compras/reconcile (Compras).

Plan de Acción de la auditoría técnica del 02/10 (Pilar 5, hallazgo Crítico): medido en
vivo ese día, el host corre con muy poco margen real de RAM libre (~640MB libres, 2,7 de
4GB de swap ya en uso por procesos ajenos al sistema) y ninguno de estos 4 endpoints tenía
techo de concurrencia -- nada impedía que varios usuarios dispararan una comparación
grande a la vez y acumularan cientos de MB cada uno en el mismo worker, sin ningún control
intermedio (tampoco lo hay en nginx, ver ese hallazgo aparte).

Un semáforo de asyncio, no una cola: se rechaza con 429 inmediato en vez de encolar sin
límite de tiempo -- encolar solo movería el mismo problema de memoria a más requests
esperando en simultáneo (y el timeout de nginx a /api es de 300s, así que una cola larga
terminaría en el mismo síntoma de todos modos, solo que más tarde y con un error más
confuso para el usuario).

Es POR PROCESO: cada worker de uvicorn (--workers en el systemd unit) tiene su propia
instancia de este módulo, con su propio semáforo -- no hay coordinación entre procesos
(no hace falta: cada proceso es responsable de su propia memoria). Con --workers 2 y el
default de settings.max_operaciones_pesadas_concurrentes, el techo real de la app
completa es 2x ese valor.
"""

from fastapi import HTTPException

from app.core.config import settings

# Contador simple, no asyncio.Semaphore -- se probó con Semaphore + wait_for(timeout=0)
# para un "try-acquire" no bloqueante, y dio falsos rechazos: asyncio.wait_for(fut,
# timeout=0) puede disparar el timeout ANTES de que semaforo.acquire() llegue a resolver,
# incluso cuando sí hay un slot libre (acquire() siempre pasa por al menos un punto de
# espera interno, y con timeout=0 la cancelación puede ganarle la carrera). Un contador
# entero evita el problema de raíz: el chequeo y el incremento de abajo no tienen ningún
# "await" en el medio, así que en un event loop de un solo hilo son atómicos por
# construcción -- ninguna otra corrutina puede intercalarse entre el if y el += 1.
_en_vuelo = 0

MSG_SERVIDOR_OCUPADO = "El servidor está procesando otras solicitudes grandes en este momento. Esperá unos segundos e intentá de nuevo."


async def adquirir_operacion_pesada() -> None:
    """Intenta tomar un slot SIN esperar -- si no hay ninguno libre, corta con un 429
    explícito de inmediato, en vez de bloquear hasta que se libere uno (eso sería la cola
    que este módulo evita a propósito, ver el docstring del archivo). Es `async def` por
    consistencia con los call sites (todos usan `await`), aunque no suspenda nunca -- no
    hace falta que lo haga."""
    global _en_vuelo
    if _en_vuelo >= settings.max_operaciones_pesadas_concurrentes:
        raise HTTPException(status_code=429, detail=MSG_SERVIDOR_OCUPADO)
    _en_vuelo += 1


def liberar_operacion_pesada() -> None:
    """Para /api/ingest y /api/compras/ingest (no streaming): liberar al final del
    try/finally de la función, normal. Para /api/reconcile y /api/compras/reconcile
    (StreamingResponse): el trabajo pesado real sigue corriendo DESPUÉS de que la función
    del endpoint ya retornó el StreamingResponse -- liberar ahí sería liberar el slot antes
    de que el streaming ni empiece. Hay que llamar a esta función recién en el finally del
    generador (_generar_respuesta), no en la función del endpoint."""
    global _en_vuelo
    _en_vuelo -= 1
