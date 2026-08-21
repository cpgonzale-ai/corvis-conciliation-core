# Modelo de Datos — Plataforma Web de Conciliación de Libros de Venta y Compras vs RG90

**Entregable de Fase 1** (Análisis funcional detallado / Diseño de arquitectura / Diseño de base de datos), conforme a la Sección 4 del Anexo A del contrato del 19/08/2026.

## 1. Objetivo

Definir el modelo de datos en PostgreSQL que soporta:
- Autenticación de usuarios con dos roles (`admin`, `operador`).
- Registro auditable de cada procesamiento (carga, consolidación, comparación RG90) sin persistir el detalle de los comprobantes.

## 2. Principio de diseño: qué se persiste y qué no

El contrato excluye explícitamente del alcance el **"Almacenamiento histórico permanente de los datos procesados"** (numeral 5.2, Anexo A) y establece que el sistema procesa datos **"de forma temporal durante la sesión activa, sin almacenamiento histórico permanente"** (cláusula 4.3). En consecuencia:

- **Se persiste:** usuarios, y metadatos/agregados de cada corrida de procesamiento (cuántos comprobantes, cuántos saltos, cuántas diferencias, quién lo hizo y cuándo) — esto habilita auditoría sin guardar información fiscal sensible de terceros a largo plazo.
- **No se persiste:** el contenido fila por fila de los comprobantes (documento, RUC, cliente, montos). Ese detalle vive únicamente en memoria durante la sesión activa del usuario, tal como en el prototipo actual (`engine.py`), y se descarta al finalizar la sesión o al cerrar el proceso.

Si en el futuro se necesita guardar el detalle completo, es un cambio de alcance (Cláusula Décima del contrato), no parte de este diseño.

## 3. Diagrama entidad-relación (lógico)

```
usuarios (1) ────< lotes_procesamiento (1) ────< archivos_procesados
                          │
                          └────< resultados_rg90

usuarios (1) ────< eventos_auditoria >──── lotes_procesamiento (0..1)
```

## 4. Tablas

### 4.1 `usuarios`

| Columna | Tipo | Notas |
|---|---|---|
| id | SERIAL PK | |
| nombre | VARCHAR(150) NOT NULL | |
| email | VARCHAR(255) UNIQUE NOT NULL | login |
| password_hash | VARCHAR(255) NOT NULL | hash (bcrypt/argon2), nunca texto plano |
| rol | VARCHAR(20) NOT NULL DEFAULT 'operador' | `admin` \| `operador` |
| activo | BOOLEAN NOT NULL DEFAULT TRUE | baja lógica |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| updated_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |

- **`admin`**: gestiona usuarios (alta/baja) y accede a la auditoría completa de todos los usuarios.
- **`operador`**: usa el flujo normal (carga, consolidación, comparación RG90) y ve únicamente su propia actividad.

### 4.2 `lotes_procesamiento`

Un registro por cada corrida de carga + consolidación (no las filas, el resumen).

| Columna | Tipo | Notas |
|---|---|---|
| id | SERIAL PK | |
| usuario_id | INTEGER NOT NULL REFERENCES usuarios(id) | |
| tipo_libro | VARCHAR(10) NOT NULL | `venta` \| `compra` |
| sistema_origen | VARCHAR(20) NOT NULL | `aloha` \| `hiopos` \| `universal` \| `mixto` |
| cantidad_comprobantes | INTEGER NOT NULL DEFAULT 0 | |
| cantidad_saltos | INTEGER NOT NULL DEFAULT 0 | saltos de correlatividad detectados (solo aplica a ventas) |
| estado | VARCHAR(20) NOT NULL DEFAULT 'cargado' | `cargado` \| `comparado_rg90` |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |

### 4.3 `archivos_procesados`

Metadata de cada archivo subido dentro de un lote (no el contenido del archivo).

| Columna | Tipo | Notas |
|---|---|---|
| id | SERIAL PK | |
| lote_id | INTEGER NOT NULL REFERENCES lotes_procesamiento(id) | |
| nombre_archivo | VARCHAR(255) NOT NULL | |
| perfil | VARCHAR(30) NOT NULL | `aloha` \| `hiopos_ventas` \| `universal` \| `rg90_set` |
| fecha_carga | TIMESTAMPTZ NOT NULL DEFAULT now() | |

### 4.4 `resultados_rg90`

Resumen agregado de cada comparación contra la RG90 (no el detalle de las diferencias).

| Columna | Tipo | Notas |
|---|---|---|
| id | SERIAL PK | |
| lote_id | INTEGER NOT NULL REFERENCES lotes_procesamiento(id) | |
| archivo_rg90_nombre | VARCHAR(255) NOT NULL | |
| total_coinciden | INTEGER NOT NULL DEFAULT 0 | |
| total_no_en_rg90 | INTEGER NOT NULL DEFAULT 0 | |
| total_no_en_libro | INTEGER NOT NULL DEFAULT 0 | |
| total_saltos | INTEGER NOT NULL DEFAULT 0 | |
| fecha_comparacion | TIMESTAMPTZ NOT NULL DEFAULT now() | |

### 4.5 `eventos_auditoria`

Log genérico de acciones del sistema.

| Columna | Tipo | Notas |
|---|---|---|
| id | SERIAL PK | |
| usuario_id | INTEGER NOT NULL REFERENCES usuarios(id) | |
| accion | VARCHAR(30) NOT NULL | `login` \| `carga_archivo` \| `conversion` \| `comparacion_rg90` \| `descarga_csv` \| `descarga_excel` \| `alta_usuario` \| `baja_usuario` |
| lote_id | INTEGER NULL REFERENCES lotes_procesamiento(id) | nulo para acciones sin lote (ej. login, gestión de usuarios) |
| detalle | JSONB NULL | datos libres de contexto (ej. nombre de archivo, cantidad de registros) |
| ip_origen | VARCHAR(45) NULL | |
| timestamp | TIMESTAMPTZ NOT NULL DEFAULT now() | |

## 5. Índices sugeridos

- `usuarios(email)` — ya único, usado en login.
- `lotes_procesamiento(usuario_id, created_at)` — listar actividad por usuario.
- `eventos_auditoria(usuario_id, timestamp)` y `eventos_auditoria(accion, timestamp)` — consultas de auditoría.

## 6. Puntos abiertos / dependencias

- **Libro de Compras (PA-02/PA-03):** el `tipo_libro = 'compra'` ya está contemplado en `lotes_procesamiento`, pero las reglas de negocio de compras todavía no fueron relevadas (pendiente la reunión de relevamiento posterior al 18/08/2026). El modelo no debería requerir cambios para soportarlo una vez relevado, salvo un nuevo perfil de mapeo declarativo (JSON), igual que `aloha.json` / `hiopos_ventas.json`.
- **Formato universal:** la estructura mínima de columnas todavía no está definida con el cliente; no afecta este modelo (vive en `perfil = 'universal'` como dato de `archivos_procesados`), pero sí al perfil declarativo JSON correspondiente.
- **Multi-cliente/multi-empresa:** la minuta 2 menciona la posibilidad de que la Consultora use el formato universal para *otros* clientes de auditoría a futuro. Ese escenario no está en el alcance contratado actualmente y este modelo no lo contempla (no hay tabla de "empresas/clientes"); si se pide más adelante, sería una Orden de Cambio.
