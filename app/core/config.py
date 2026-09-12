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
