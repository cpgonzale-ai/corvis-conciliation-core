# Documento de Análisis Funcional — Plataforma Web de Conciliación de Libros de Venta y Compras vs RG90

**Entregable de Fase 1** (Anexo A, Sección 4). Consolida las reglas de negocio relevadas en las minutas de reunión 1 a 4 (29/07 – 18/08/2026) y las contrasta con el prototipo actual (`corvis-conciliation-core`, `siscom-rg90`), para que sirva de base verificable al desarrollo de Fase 2. Complementa `modelo_datos.md` y `diagrama_arquitectura.md`.

## 1. Introducción

El grupo cliente de la Consultora San Miguel opera varios proyectos gastronómicos (Juan Valdez, La Cabrera, Almacén de Pizza, Fabri Sushi, 100M, entre otros) bajo un único RUC, facturando desde dos sistemas: **Aloha** (Juan Valdez, ~31 locales) y **Hiopos** (el resto). Ninguno de los dos emite el libro de ventas en el formato requerido por la Consultora, por lo que hoy se arma manualmente y se compara contra la RG90 (SET) en Excel, con bloqueos frecuentes por volumen. Este documento define las reglas que la plataforma debe automatizar.

## 2. Alcance y trazabilidad

Este documento cubre el **Módulo de Procesamiento Base** (libro de ventas: CU-01 a CU-07) en su totalidad, ya que su relevamiento está cerrado (Minuta 4: *"se da por cerrado el relevamiento del libro de ventas"*). El **Módulo Ampliado** (PA-02/PA-03, libro de compras) se documenta parcialmente: solo lo que ya fue acordado en las minutas 1 y 2, porque la reunión de relevamiento detallado de compras aún no se realizó (ver §12).

## 3. Actores y roles

| Actor | Descripción |
|---|---|
| Operador | Usuario de la Consultora que carga reportes, ejecuta la consolidación y la comparación RG90 en su uso diario. |
| Administrador | Además de lo anterior, gestiona altas/bajas de usuarios y accede a la auditoría completa (ver `modelo_datos.md` §4.1). |

## 4. Glosario

- **Punto de expedición**: código que identifica el local/sucursal emisor de un comprobante (ej. `025`, `030`).
- **Correlatividad**: continuidad de la numeración de comprobantes dentro de un mismo punto de expedición; un "salto" es un número faltante en la secuencia.
- **RG90**: reporte del SET (Marangatu) con los comprobantes que el organismo recaudador tiene efectivamente registrados.
- **"Herraje"**: control de facturación electrónica que la Consultora usaba antes; ya no aplica porque la operación es 100% electrónica (Minuta 3).
- **Inutilización**: baja de una numeración no utilizada (plazo 30 días); distinto de la **anulación** de un comprobante ya emitido (plazo 48 h).

## 5. Reglas de negocio transversales (Libro de Ventas)

### 5.1 Cálculo de IVA y clasificación de tasa (Minutas 3 y 4)

Ni Aloha ni Hiopos identifican explícitamente si un comprobante está gravado al 10%, al 5% o exento — solo entregan un importe "gravada" y un importe "IVA" en bruto. La regla acordada es:

1. Probar las tres tasas posibles: **10%, 5% y 0%**.
2. Para cada tasa candidata, calcular `gravada × tasa − IVA`.
3. La tasa cuyo resultado da **0 (o próximo a 0, tolerancia ≈ 0,5 por redondeo)** es la tasa real del comprobante.
4. Si el reporte trae **IVA = 0** con importe gravado presente, la operación se considera **exenta**.
5. **Control de total**: el total del comprobante no debe variar entre el documento en bruto y el resultado del cálculo, aun cuando cambie el desglose interno del IVA.

Esta regla es **común a Aloha y a Hiopos** (Minuta 4: *"se confirmó que se aplica la misma fórmula que en Aloha"*).

### 5.2 Control de correlatividad

- Se agrupa por punto de expedición (ej. `025-001`) dentro de cada local.
- Se ordenan los números y se detectan huecos en la secuencia.
- Debe exponerse **antes** de la comparación contra RG90 (acuerdo de Minuta 1), indicando el último número procesado y la fila del salto.
- Aplica únicamente al **Libro de Ventas**; el Libro de Compras no lleva control de correlatividad (Minuta 2, PA-03).

### 5.3 Formato limpio del libro de ventas (estructura destino común)

Campos del libro de ventas unificado (Minuta 3):

| Campo | Notas |
|---|---|
| Número de factura | punto de expedición + número, formato `EEE-PPP-NNNNNNN` |
| Fecha | normalizada a `YYYY-MM-DD` |
| Proyecto (sucursal/cliente) | ej. "Juan Valdez - Shopping del Sol" |
| Punto de expedición | diferencia cada sucursal |
| Tipo | factura / nota de crédito (conviven en el mismo formato) |
| RUC / Razón social | `X` / `SIN NOMBRE` si faltan |
| Gravada 10%, IVA 10%, Gravada 5%, IVA 5%, Exenta, Total | según clasificación de tasa (§5.1) |
| Estado | Válida / Anulada |
| Condición de venta | se mantiene en blanco por ahora (dato no disponible aún) |

Solo se genera la hoja **"Libro Ventas Global"**; el control de "herraje" queda descartado (ya no aplica).

## 6. Reglas específicas — Sistema Aloha

Fuente: Minuta 3 (17/08/2026).

- El reporte en bruto trae **facturas y notas de crédito en el mismo archivo**, con las notas de crédito debajo de las facturas.
- Cuando hay más de una serie/punto de expedición, el reporte trae **subtotales por serie** intercalados → deben eliminarse antes de procesar.
- Existe una columna de **"impuesto"** mal ubicada/rotulada que debe descartarse.
- Los títulos de columna vienen **desalineados** respecto de su contenido real → deben reordenarse. Columnas reales: cliente (rotulada "servicio"), fecha, número de documento, gravada, IVA, venta total, estado (aprobado/anulado).
- **La columna "estado" (aprobado/anulado) no se utiliza** para el libro de ventas — el estado se deriva del cálculo (§5.1), no de esa columna.
- Como el número de comprobante siempre está presente en el reporte, la **correlatividad se controla directamente**, sin necesidad de concatenar campos.

## 7. Reglas específicas — Sistema Hiopos

Fuente: Minuta 4 (18/08/2026).

- Se descartan los reportes de **"medio de pago"** (forma de cobro, no se usa).
- El reporte trae facturas y notas de crédito diferenciadas por **tipo de documento** y por **signo del importe** (notas de crédito en negativo).
- Tipos de documento: **"factura de venta"** y **"factura de venta simplificada"** (ambas se tratan como factura), y **"abono"** (nota de crédito emitida por el sistema).
- Existe una serie **"anulación"**: documento interno que Hiopos genera cuando la factura electrónica ya no puede anularse. **No tiene correlatividad válida y no se traslada al libro de ventas — debe eliminarse.** (Ya implementado como `filter_rule` en `hiopos_ventas.json:18`.)
- Columnas del reporte: tipo de documento; punto de expedición y sucursal; número de factura/NC; código interno (no se usa); fecha de emisión (se descarta la hora); proyecto/sucursal; contacto (nombre cliente); empleado/cajero (no se traslada); total base (gravada, incluye exenta); IVA; total. Las columnas "estado" y "procesado" vienen vacías y no se usan.
- Si falta nombre de cliente: RUC = `X`, contacto = `SIN NOMBRE`.
- **Conformación de la numeración (concatenación)**: sucursal + punto de expedición (ej. `030-001`) + número de comprobante, con el segmento numérico completado a **7 dígitos** con ceros a la izquierda. Aplica a facturas y notas de crédito por igual.
- El **libro global** consolida todas las sucursales e incluye, al final, un resumen por tipo de documento (total facturas / total notas de crédito) que determina la **venta neta**.

## 8. Reglas de comparación contra RG90

- **Deben adjuntarse dos reportes de RG90** — uno de venta y otro de nota de crédito — que se **consolidan en un único archivo antes de comparar** (Minuta 3). Para Hiopos específicamente, las notas de crédito se descargan desde la RG **de compras** (no de ventas), porque no están disponibles en la RG de ventas (Minuta 4).
- La columna **"gravada" del reporte de la RG90 viene mal calculada** (incluye el IVA) — **no debe usarse** para el control. El control correcto se hace por **IVA (10% y 5%) y monto gravado**, no por la columna "gravada" de la RG.
- Categorías de diferencia a exponer: coincide / no llegó a la interfaz / no está en el libro propio / rechazada / anulada / salto de numeración (ya reflejado en `engine.py:200-259`, `reconcile_with_rg90`).

## 9. Casos de uso (Módulo de Procesamiento Base)

| CU | Descripción | Regla de negocio asociada |
|---|---|---|
| CU-01 Autenticación | Login usuario/contraseña, JWT | §Auth en `diagrama_arquitectura.md` §4 |
| CU-02 Dashboard | Locales activos, comprobantes del período, saltos, diferencias RG90 | Agregados de `lotes_procesamiento` / `resultados_rg90` |
| CU-03 Carga de reportes | Selección de sistema (Aloha/Hiopos/Universal), subida de archivos | §6, §7 |
| CU-04 Consolidación | Unificación de locales, concatenación de numeración, separación de exentas | §5.1, §5.3, §7 |
| CU-05 Control de correlatividad | Detección de saltos por punto de expedición, antes de comparar | §5.2 |
| CU-06 Comparación RG90 | Cruce libro propio vs RG90, consolidando venta+NC | §8 |
| CU-07 Exportación | Descarga de libro limpio y de diferencias, completo o filtrado | — |

## 10. Brechas identificadas y corregidas (validado contra los archivos reales de Mayo 2026)

Los puntos siguientes se detectaron comparando `engine.py` contra los 39 archivos reales de muestra (Aloha, Hiopos y RG90 de mayo 2026) y ya fueron corregidos en el motor:

1. **Algoritmo de prueba de tasas (10/5/0%) implementado.** `classify_tax_rate()` en `engine.py` prueba `gravada × tasa − IVA ≈ 0` para 10 %, 5 % y exento, tal como describen las Minutas 3 y 4. Los perfiles `aloha.json` y `hiopos_ventas.json` ahora extraen `gravada_bruta`/`iva_bruta` (sin asumir tasa) y el motor clasifica; `rg90_set.json` y `universal.json` no lo necesitan porque ya traen los montos separados por tasa.
2. **La comparación RG90 acepta dos archivos** (venta + nota de crédito) que se consolidan antes de comparar (`/api/reconcile` ahora recibe `rg90_files: List[UploadFile]`), según §8.
3. **La comparación RG90 controla 4 campos por separado** (Total, IVA 10 %, IVA 5 %, Exenta) en vez de solo el total, replicando las columnas "Check RG Total/IVA10%/IVA5%/Exentas" de la planilla real del cliente (`Ejemplo Controles sobre Libros y RG.xls`, hoja "Libro Ventas"). No se usa la columna "gravada" de la RG90 porque viene mal calculada (confirmado en Minuta 4).
4. **Un comprobante anulado que no aparece en la RG90 ya no se marca como error** ("No llegó a la interfaz"), sino como "Anulada" — es el comportamiento esperado (una anulación nunca se transmite a la RG90). Confirmado con datos reales: de 81 anuladas en el archivo de Sheraton, ninguna está en la RG90.
5. **Corrección sobre la Minuta 3 ("estado no se utiliza"):** los datos reales muestran que la columna de estado de Aloha (`E`/`A`) sí se usa y coincide exactamente con los totales del pie del reporte (657 facturas, 81 anuladas) — se mantiene el mapeo existente. Este es un caso donde la evidencia de los archivos reales corrigió lo relevado en la minuta.
6. **Validación estricta del formato de documento.** Antes, `normalize_invoice_number` devolvía el string crudo como *fallback* cuando no reconocía el formato, lo que dejaba pasar filas de subtotal/pie de página (ej. `"Serie: 001 Totales..."`, `"Total General..."`) como si fueran comprobantes válidos. Ahora `ingest_file` valida el resultado final contra `^(NC)?\d{3}-\d{3}-\d{7}$` y descarta lo que no calce — con esto se eliminan automáticamente subtotales, sin necesidad de un `data_start_row` exacto por archivo.
7. **Series internas no fiscales excluidas.** Hiopos mezcla en el mismo reporte facturas, notas de crédito, y también "Merma", "Invitación", "Factura compra", "Pedido de compra", "Albarán compra" (confirmado en `Libro Ventas 100 M Mayo.xls`, hoja 2). Todas estas usan códigos de serie que no calzan con el formato `EEE-PPP` de un punto de expedición real (`SHR0002`, `T000101`, `C000101`, `P000101`), así que se filtran con `SERIE_PATTERN`.
8. **Notas de crédito de Hiopos con prefijo "NC" confirmadas.** La serie de una NC viene como `NC030-001` (prefijo + establecimiento + punto de expedición); el documento final queda `NC030-001-0000043`, fuera del control de correlatividad (que solo aplica a facturas). Confirmado con la hoja "Detalles Ventas" que el propio cliente dejó documentada en `Ventas 100 M Mayo 2026.xls`.
9. **Mojibake reparado.** Hiopos exporta como UTF-8 pero el archivo se re-lee como Windows-1252 (`"Belén"` → `"BelÃ©n"`, `"Patiño"` → `"PatiÃ‘o"`). Se agregó `fix_mojibake()` (con `cp1252`, no `latin-1` — el primero no cubre todo el rango de acentos observado) aplicado a nombres de cliente.
10. **Selección de hoja tolerante a inconsistencias.** El cliente indicó que la hoja 1 de los reportes de muestra fue editada a mano y que solo debe usarse la hoja 2 ("tal como se descarga del sistema"). Sin embargo, **la convención de hojas no es uniforme entre archivos**: algunos Aloha tienen una sola hoja; `Libro Ventas Fabric.xls` (Hiopos) tiene 3 hojas (`SOLO VENTAS`, `Borrador`, `Original`) donde la hoja índice 1 es justamente el borrador mal estructurado. El motor ahora prueba primero la hoja preferida del perfil y, si no existe o no produce ningún comprobante válido, recorre las demás hojas del archivo hasta encontrar una que sí produzca filas — evita que un archivo con nombres de hoja atípicos rompa la carga.

### Falso positivo descartado

Se había señalado como posible bug que el RG90 de muestra (`SOLO VENTAS MAYO 2026.xls`) devolviera 65.535 filas al compararlo contra un solo local. **No es un bug**: la RG90 se descarga a nivel de toda la empresa (46 puntos de expedición distintos en ese archivo), no por local — coincide con lo que indica la Minuta 1 ("obliga a comparar la totalidad de los registros del grupo"). El perfil `rg90_set.json` está correcto tal como estaba.

## 11. Riesgos y supuestos

- El correcto funcionamiento depende de que Aloha y Hiopos mantengan la estructura de columnas relevada (Cláusula Décima Octava del contrato); un cambio de formato de origen es una Orden de Cambio, no un defecto de garantía.
- Los "Insumos Iniciales" que el cliente debe entregar (Cláusula Séptima del contrato) incluyen los archivos RG90 de venta y de nota de crédito por separado — hay que confirmar que la entrega los distinga así.
- **La convención de hojas dentro de los archivos Excel de origen no es estable** (ver §10.10): se recomienda pedirle al cliente que estandarice el nombre/orden de la hoja "tal como se descarga" en los envíos futuros, o incorporar en una etapa posterior una selección manual de hoja al momento de cargar el archivo, en vez de depender de heurísticas automáticas.
- `Libro Ventas Fabric.xls` es un caso conocido de datos posiblemente incompletos: tiene una hoja adicional ("Original") con comprobantes que no se solapan con "SOLO VENTAS", y el motor hoy solo toma la primera hoja válida que encuentra, no combina varias. A confirmar con el cliente si ambas hojas deben consolidarse.

## 12. Puntos pendientes

- **Libro de Compras (PA-02/PA-03):** solo se sabe, por la Minuta 2, que la comparación se hace por RUC + número de comprobante, sin control de correlatividad. Las reglas de limpieza, cálculo y estructura de columnas de compras (equivalentes a §6/§7 para ventas) **no están relevadas** — falta agendar y realizar esa reunión antes de poder detallar los CU de compras o construir el perfil de mapeo correspondiente.
- **Formato universal:** aún sin definir la estructura mínima de columnas requerida (contrato, cláusula 7.1). El perfil `universal.json` actual es un placeholder, no una definición validada con el cliente.
