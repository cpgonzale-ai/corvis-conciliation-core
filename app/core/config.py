"""Application settings loaded from environment variables (.env)."""

from pydantic_settings import BaseSettings, SettingsConfigDict

# Valor de ejemplo para jwt_secret_key — NUNCA debe usarse en un arranque real. Se valida
# más abajo, después de instanciar Settings, para que un .env faltante o mal cargado haga
# fallar el arranque con un mensaje claro, en vez de dejar la app corriendo silenciosamente
# con una clave de firma de JWT conocida públicamente (cualquiera podría forjar tokens
# válidos contra ese valor).
_JWT_SECRET_PLACEHOLDER = "change-me-in-.env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://corvis:corvis@localhost:5432/corvis_rg90"
    jwt_secret_key: str = _JWT_SECRET_PLACEHOLDER
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480  # 8 horas
    cors_origins: str = "*"
    # Límite de concurrencia para los 4 endpoints pesados (ingest/reconcile de Ventas y
    # Compras) -- Plan de Acción de la auditoría del 02/10, Pilar 5: sin esto, nada impide
    # que N usuarios disparando una comparación grande a la vez acumulen cientos de MB cada
    # uno en el mismo worker, sobre un host medido con poco margen real de RAM libre. Es
    # POR PROCESO (cada worker de --workers tiene su propio semáforo, no se comparte entre
    # procesos) -- con --workers 2 y el default de acá, el techo real de la app completa es
    # 2x este valor. Configurable por .env para ajustar sin tocar código si cambia el
    # margen real de memoria del host.
    max_operaciones_pesadas_concurrentes: int = 2

    @property
    def cors_origins_list(self) -> list[str]:
        if self.cors_origins == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()

if settings.jwt_secret_key == _JWT_SECRET_PLACEHOLDER:
    raise RuntimeError(
        "JWT_SECRET_KEY no está configurada (falta en el .env, o el .env no se cargó desde "
        "el directorio de trabajo del proceso). La app no arranca con la clave de ejemplo — "
        "configurá una clave real en el archivo .env antes de iniciar el servidor."
    )
