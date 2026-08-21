# Diagrama de Arquitectura — Plataforma Web de Conciliación de Libros de Venta y Compras vs RG90

**Entregable de Fase 1** (Análisis funcional detallado / Diseño de arquitectura / Diseño de base de datos), conforme a la Sección 4 del Anexo A del contrato del 19/08/2026. Complementa a `modelo_datos.md`.

## 1. Restricciones que definen la arquitectura

Estas restricciones vienen del contrato y de las decisiones ya tomadas, y condicionan todo el diseño:

- **Precio cerrado, VPS único de bajo costo** (Cláusula Décima Tercera: ~USD 10/mes, "Entorno Linux con recursos optimizados para procesamiento de archivos Excel en memoria"). No hay presupuesto para infraestructura distribuida (balanceador, DB gestionada, colas, etc.).
- **Sin almacenamiento histórico de comprobantes** (cláusula 4.3 / numeral 5.2): el procesamiento de archivos es transitorio, en memoria, por sesión. Solo se persisten metadatos/agregados y auditoría (ver `modelo_datos.md`).
- **Acceso web público con usuario y contraseña** (CU-01), incluyendo dispositivos móviles (responsive).
- **Dependencia de terceros** (Cláusula Décima Octava): la estructura de columnas de Aloha, Hiopos y RG90 puede cambiar; el mapeo debe estar aislado en configuración (perfiles JSON), no hardcodeado, para que un cambio de formato no obligue a tocar el motor.

## 2. Diagrama de componentes (nivel lógico)

```
┌──────────────────────────────────────────────────────────────────┐
│                        Navegador (desktop / móvil)                 │
│                     React + TypeScript + Vite (SPA)                │
│  Sidebar · Header · Dashboard · CargaView · CorrelatividadView ·   │
│  RG90View · AuthProvider (JWT en memoria/localStorage)             │
└───────────────────────────────┬──────────────────────────────────┘
                                 │ HTTPS (fetch, Bearer token)
┌───────────────────────────────▼──────────────────────────────────┐
│                       Nginx (reverse proxy + TLS)                  │
│         sirve el build estático de React · enruta /api/* → API     │
└───────────────────────────────┬──────────────────────────────────┘
                                 │
┌───────────────────────────────▼──────────────────────────────────┐
│                    FastAPI (Uvicorn, app.api.main)                 │
│  ┌────────────────┐  ┌───────────────────┐  ┌───────────────────┐ │
│  │  Auth Router    │  │  Ingest Router     │  │  Reconcile Router │ │
│  │  /api/auth/*    │  │  /api/ingest       │  │  /api/reconcile   │ │
│  │  login, JWT     │  │  (venta/compra)    │  │  vs RG90          │ │
│  └────────┬────────┘  └─────────┬─────────┘  └─────────┬─────────┘ │
│           │                     │                        │         │
│  ┌────────▼─────────────────────▼────────────────────────▼───────┐ │
│  │            IngestionEngine (app.core.engine) — pandas          │ │
│  │   perfiles declarativos (app/profiles/*.json): aloha,          │ │
│  │   hiopos_ventas, hiopos_compras*, universal, rg90_set          │ │
│  │   → procesamiento 100% en memoria, nada se persiste acá        │ │
│  └──────────────────────────────────────────────────────────────┘ │
│           │                                            │           │
│  ┌────────▼────────┐                          ┌────────▼────────┐ │
│  │  Auth Service     │                          │  Audit Service  │ │
│  │  (passlib/bcrypt, │                          │  registra lotes_│ │
│  │   JWT)            │                          │  procesamiento, │ │
│  │                    │                          │  eventos_audit. │ │
│  └────────┬──────────┘                          └────────┬────────┘ │
└───────────┼─────────────────────────────────────────────┼─────────┘
            │                                              │
┌───────────▼──────────────────────────────────────────────▼────────┐
│                          PostgreSQL                                 │
│   usuarios · lotes_procesamiento · archivos_procesados ·           │
│   resultados_rg90 · eventos_auditoria   (ver modelo_datos.md)      │
└──────────────────────────────────────────────────────────────────┘

* hiopos_compras / perfil de compras: pendiente de definir tras el
  relevamiento faltante (ver punto 6 de modelo_datos.md).
```

## 3. Diagrama de despliegue (infraestructura)

Todo corre en un único VPS Linux, tal como prevé el presupuesto (Cláusula Décima Tercera del contrato):

```
┌───────────────────────── VPS Linux (≈USD 10/mes) ─────────────────────────┐
│                                                                            │
│  systemd                                                                  │
│   ├─ nginx.service ──── puerto 443 (TLS, Let's Encrypt) / 80 → 443        │
│   │     ├─ / (estático)        → dist/ del build de siscom-rg90          │
│   │     └─ /api/*              → proxy_pass a 127.0.0.1:8000              │
│   │                                                                       │
│   ├─ corvis-api.service ─ uvicorn app.api.main:app en 127.0.0.1:8000     │
│   │     (no expuesto directamente a internet)                            │
│   │                                                                       │
│   └─ postgresql.service ─ 127.0.0.1:5432 (solo loopback, sin acceso      │
│         externo)                                                          │
│                                                                            │
│  Certbot (renovación automática de certificado TLS)                      │
└────────────────────────────────────────────────────────────────────────┘
```

Notas:
- Un único servidor evita el costo de una base de datos gestionada aparte; a la escala de este proyecto (comparaciones puntuales, no continuas) es suficiente.
- Postgres y la API no se exponen a internet directamente; solo Nginx recibe tráfico externo. Reduce superficie de ataque sin agregar costo.
- El dominio (`.com.py`, Cláusula Décima Tercera) apunta al VPS; Nginx sirve el frontend y proxya `/api`.

## 4. Flujo de autenticación

```
Usuario → POST /api/auth/login {email, password}
        ← 200 {access_token (JWT), rol}
Usuario → guarda el token (memoria / localStorage)
Usuario → cualquier request subsiguiente: Authorization: Bearer <token>
FastAPI → middleware valida JWT en cada request (excepto /api/health y /api/auth/login)
        → decodifica rol (admin | operador) para autorizar acciones de gestión de usuarios
```

Cambio respecto al prototipo actual: `CORSMiddleware` está configurado con `allow_origins=["*"]` (`app/api/main.py`), válido para la etapa de demo con túnel, pero en producción debe restringirse al dominio final del frontend.

## 5. Flujo de procesamiento (los 3 pasos de la UI), y qué toca la base de datos

```
Paso 1 — Carga           Paso 2 — Consolidación/Correlatividad      Paso 3 — RG90
┌───────────────┐        ┌───────────────────────────────┐        ┌──────────────────┐
│ Sube archivos  │───────▶│ IngestionEngine.ingest_file()  │───────▶│ reconcile_with_   │
│ Aloha/Hiopos/  │        │ + detect_sequence_gaps()       │        │ rg90()            │
│ Universal      │        │ (100% en memoria, pandas)      │        │ (100% en memoria) │
└───────────────┘        └───────────────┬─────────────────┘        └─────────┬─────────┘
                                          │                                    │
                                          ▼                                    ▼
                          INSERT lotes_procesamiento               INSERT resultados_rg90
                          INSERT archivos_procesados (metadata)    (agregados, no detalle)
                          INSERT eventos_auditoria                 INSERT eventos_auditoria
                          (cantidad_comprobantes, cantidad_saltos) (totales, no filas)
```

Las filas de comprobantes (`doc`, `ruc`, `nombre`, montos) nunca llegan a la base — solo viajan entre el navegador y la API dentro de la sesión, y los conteos/resúmenes de esa corrida quedan en la base para auditoría.

## 6. Stack tecnológico (confirmación / cambios respecto al prototipo)

| Capa | Prototipo actual | Propuesta para producción |
|---|---|---|
| Frontend | React 18 + TS + Vite, estilos inline | Igual, se agrega `AuthProvider`/rutas protegidas y manejo de token |
| Backend | FastAPI + pandas + xlrd | Igual + `SQLAlchemy` (ORM) + `Alembic` (migraciones) + `passlib[bcrypt]` + `python-jose` (JWT) |
| Base de datos | Ninguna | PostgreSQL (ver `modelo_datos.md`) |
| Reverse proxy / TLS | `localtunnel` (solo demo) | Nginx + Certbot |
| Hosting | Local (dev) | VPS Linux único, contratado por el cliente (Cláusula Décima Tercera) |

## 7. Puntos abiertos

- **Perfil de compras:** el router `/api/ingest` deberá aceptar `tipo_libro=compra` una vez definidas las reglas (pendiente reunión de relevamiento).
- **CORS de producción:** restringir `allow_origins` al dominio final una vez que el cliente contrate el dominio/VPS.
- **Rotación/expiración de JWT:** a definir tiempo de expiración del token y si hay refresh token (por defecto, propongo expiración corta de 8h con re-login, dado que no hay requerimiento de sesiones prolongadas en el contrato).
