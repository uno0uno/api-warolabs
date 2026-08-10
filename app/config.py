from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional

class Settings(BaseSettings):
    # Database - mapeando desde las variables de warolabs.com
    database_url: str
    db_user: str = Field(alias='NUXT_PRIVATE_DB_USER')
    db_host: str = Field(alias='NUXT_PRIVATE_DB_HOST')
    db_password: str = Field(alias='NUXT_PRIVATE_DB_PASSWORD')
    db_port: int = Field(default=5432, alias='NUXT_PRIVATE_DB_PORT')
    db_name: str = Field(alias='NUXT_PRIVATE_DB_NAME')
    
    # JWT Security - nombres limpios
    jwt_secret: str = Field(alias='NUXT_PRIVATE_JWT_SECRET')
    auth_secret: str = Field(alias='BETTER_AUTH_SECRET_KEY')
    token_backend: str = Field(alias='NUXT_PRIVATE_TOKEN_BACKEND')
    
    # AWS - nombres limpios (para SES)
    aws_access_key_id: Optional[str] = Field(default=None, alias='NUXT_PRIVATE_AWS_ACCES_KEY_ID')
    aws_secret_access_key: Optional[str] = Field(default=None, alias='NUXT_PRIVATE_AWS_SECRET_ACCESS_KEY')
    aws_region: Optional[str] = Field(default=None, alias='NUXT_PRIVATE_AWS_REGION')
    email_from: Optional[str] = Field(default=None, alias='NUXT_PRIVATE_EMAIL_FROM')
    frontend_url: str = Field(default="https://warocol.com", alias='FRONTEND_URL')

    # Cloudflare R2 - S3-compatible storage
    r2_access_key_id: Optional[str] = Field(default=None, alias='NUXT_PRIVATE_R2_ACCESS_KEY_ID')
    r2_secret_access_key: Optional[str] = Field(default=None, alias='NUXT_PRIVATE_R2_SECRET_ACCESS_KEY')
    r2_endpoint: Optional[str] = Field(default=None, alias='NUXT_PRIVATE_R2_ENDPOINT')
    r2_bucket: str = Field(default='warocol-purchase-attachments', alias='NUXT_PRIVATE_R2_BUCKET')
    # Public R2 bucket for logos, banners, and other public-facing images
    r2_public_bucket: str = Field(default='warocol-public-assets', alias='NUXT_PRIVATE_R2_PUBLIC_BUCKET')
    r2_public_url: Optional[str] = Field(default=None, alias='NUXT_PRIVATE_R2_PUBLIC_URL')
    
    # Encryption - nombres limpios
    private_key_encrypter: Optional[str] = Field(default=None, alias='NUXT_PRIVATE_PRIVATE_KEY_ENCRYPTER')
    public_key_encrypter: Optional[str] = Field(default=None, alias='NUXT_PUBLIC_PUBLIC_KEY_ENCRYPTER')
    
    # App settings
    environment: str = Field(default="development", alias='NODE_ENV')
    base_url: str = Field(default="http://localhost:5000", alias='NUXT_PUBLIC_BASE_URL')
    
    # FastAPI specific
    port: int = Field(default=5000, alias='FASTAPI_PORT')
    host: str = Field(default="0.0.0.0", alias='FASTAPI_HOST')
    debug: bool = Field(default=True, alias='DEBUG')
    
    # CORS configuration
    cors_origins: str = Field(alias='CORS_ORIGINS')
    
    # Localhost to tenant mapping for development
    localhost_mapping: str = Field(default="", alias='LOCALHOST_MAPPING')

    # Ollama Extract API
    ollama_api_url: str = Field(default="https://chat.warocol.com", alias='OLLAMA_API_URL')

    # Internal WARO agent-api proxy
    agent_api_url: Optional[str] = Field(default=None, alias='AGENT_API_URL')
    agent_internal_signature_secret: Optional[str] = Field(default=None, alias='INTERNAL_SIGNATURE_SECRET')
    agent_api_connect_timeout_seconds: float = Field(default=5.0, alias='AGENT_API_CONNECT_TIMEOUT_SECONDS')
    agent_api_read_timeout_seconds: Optional[float] = Field(default=300.0, alias='AGENT_API_READ_TIMEOUT_SECONDS')
    
    # Google Gemini API
    google_api_key: Optional[str] = Field(default=None, alias='GOOGLE_API_KEY')

    # Discord webhook for notifications
    discord_webhook_url: Optional[str] = Field(default=None, alias='DISCORD_WEBHOOK_URL')
    discord_session_webhook_url: Optional[str] = Field(
        default="https://discord.com/api/webhooks/1444177595808878704/jgLKBIFmv8VKIVlniJiQXl1TSCE6hKfEwj40wOSbWJb33NL28N5hbkcZUkH8-S90lojM",
        alias='DISCORD_SESSION_WEBHOOK_URL'
    )
    discord_purchase_webhook_url: Optional[str] = Field(
        default="https://discord.com/api/webhooks/1444179228563079250/0orSlHydW1ptwsgsTBA4sFf70rqMgZn_WgitKM6bV7qcSAEsb8jZXZKJNGgqLNW2S1ef",
        alias='DISCORD_PURCHASE_WEBHOOK_URL'
    )
    discord_error_webhook_url: Optional[str] = Field(
        default="https://discord.com/api/webhooks/1445269262515044464/lNHnWUHhUeObE11SOJztvwc8LqrGgLrh4uQtnqxn6lrn4KgdKARPeV7F1Nd-sNlybyaF",
        alias='DISCORD_ERROR_WEBHOOK_URL'
    )
    discord_supplier_webhook_url: Optional[str] = Field(default=None, alias='DISCORD_SUPPLIER_WEBHOOK_URL')
    discord_purchase_actions_webhook_url: Optional[str] = Field(default=None, alias='DISCORD_PURCHASE_ACTIONS_WEBHOOK_URL')
    discord_leads_webhook_url: Optional[str] = Field(default=None, alias='DISCORD_LEADS_WEBHOOK_URL')

    # Wompi — pasarela de pagos Colombia (issue #60); legacy until #798
    wompi_public_key: Optional[str] = Field(default=None, alias='WOMPI_PUBLIC_KEY')
    wompi_private_key: Optional[str] = Field(default=None, alias='WOMPI_PRIVATE_KEY')
    wompi_events_secret: Optional[str] = Field(default=None, alias='WOMPI_EVENTS_SECRET')
    wompi_sandbox_events_secret: Optional[str] = Field(
        default=None, alias='WOMPI_SANDBOX_EVENTS_SECRET'
    )
    wompi_integrity_secret: Optional[str] = Field(default=None, alias='WOMPI_INTEGRITY_SECRET')
    wompi_environment: str = Field(default='sandbox', alias='WOMPI_ENVIRONMENT')

    # Central Wompi router → Tickets forward (#353 / api_warotickets#46)
    warotickets_api_url: Optional[str] = Field(
        default=None, alias='WAROTICKETS_API_URL'
    )
    wompi_webhook_forward_secret: Optional[str] = Field(
        default=None, alias='WOMPI_WEBHOOK_FORWARD_SECRET'
    )

    # Paddle Billing — regional SaaS checkout (epic #793 / batch #795)
    paddle_api_key_live: Optional[str] = Field(default=None, alias='PADDLE_API_KEY_LIVE')
    paddle_api_key_sandbox: Optional[str] = Field(default=None, alias='PADDLE_API_KEY_SANDBOX')
    paddle_webhook_secret_live: Optional[str] = Field(
        default=None, alias='PADDLE_WEBHOOK_SECRET_LIVE'
    )
    paddle_webhook_secret_sandbox: Optional[str] = Field(
        default=None, alias='PADDLE_WEBHOOK_SECRET_SANDBOX'
    )
    paddle_price_usd_9_annual_live: Optional[str] = Field(
        default=None, alias='PADDLE_PRICE_USD_9_ANNUAL_LIVE'
    )
    paddle_price_usd_9_annual_test: Optional[str] = Field(
        default=None, alias='PADDLE_PRICE_USD_9_ANNUAL_TEST'
    )
    paddle_price_usd_30_annual_live: Optional[str] = Field(
        default=None, alias='PADDLE_PRICE_USD_30_ANNUAL_LIVE'
    )
    paddle_price_usd_30_annual_test: Optional[str] = Field(
        default=None, alias='PADDLE_PRICE_USD_30_ANNUAL_TEST'
    )
    paddle_price_eur_30_annual_live: Optional[str] = Field(
        default=None, alias='PADDLE_PRICE_EUR_30_ANNUAL_LIVE'
    )
    paddle_price_eur_30_annual_test: Optional[str] = Field(
        default=None, alias='PADDLE_PRICE_EUR_30_ANNUAL_TEST'
    )

    # Cron secret — grace period reminders (issue #62)
    cron_secret: Optional[str] = Field(default=None, alias='CRON_SECRET')

    # Outgoing webhook fired on subscription payment approval (issue #156).
    # Empty / None → no-op. Set to any URL (Discord, n8n, Slack, custom) to
    # receive a JSON payload describing each successful renewal.
    billing_webhook_url: Optional[str] = Field(default=None, alias='BILLING_WEBHOOK_URL')

    # api-facturacion microservice — DIAN electronic invoicing (issue #128)
    facturacion_api_url: str = Field(default='http://api-facturacion:8001', alias='FACTURACION_API_URL')
    # Matias environment label for /facturacion-status (mirrors api_facturacion)
    matias_environment_id: int = Field(default=2, alias='MATIAS_ENVIRONMENT_ID')
    matias_habilitacion_tenant_ids: str = Field(default='', alias='MATIAS_HABILITACION_TENANT_IDS')
    matias_sandbox_tenant_ids: str = Field(default='', alias='MATIAS_SANDBOX_TENANT_IDS')

    # warocol.com#596 — grace window for the "ya validado" short-circuit in
    # emit_invoice. A repeated emit on a rejected-as-validado order is 409'd
    # only if the previous rejection is younger than this many minutes.
    # Older rejections fall through to api-facturacion so the retry loop
    # (api-facturacion#21) can recover the order.
    dian_short_circuit_grace_minutes: int = Field(default=5, alias='DIAN_SHORT_CIRCUIT_GRACE_MINUTES')

    # ── Platform legal identity for receipt/print footers (source of truth) ──
    # WARO = POS software / technology platform (not the tenant FE issuer).
    # Empty env → empty print fields (no PII hardcode in repo for production).
    waro_legal_commercial_name: str = Field(default='', alias='WARO_LEGAL_COMMERCIAL_NAME')
    waro_legal_legal_name: str = Field(default='', alias='WARO_LEGAL_LEGAL_NAME')
    waro_legal_nit: str = Field(default='', alias='WARO_LEGAL_NIT')
    waro_legal_document_type: str = Field(default='', alias='WARO_LEGAL_DOCUMENT_TYPE')
    waro_legal_document_number: str = Field(default='', alias='WARO_LEGAL_DOCUMENT_NUMBER')
    waro_legal_address: str = Field(default='', alias='WARO_LEGAL_ADDRESS')
    waro_legal_city: str = Field(default='', alias='WARO_LEGAL_CITY')
    waro_legal_email: str = Field(default='', alias='WARO_LEGAL_EMAIL')
    waro_legal_phone_1: str = Field(default='', alias='WARO_LEGAL_PHONE_1')
    waro_legal_phone_2: str = Field(default='', alias='WARO_LEGAL_PHONE_2')
    waro_legal_website: str = Field(default='warocol.com', alias='WARO_LEGAL_WEBSITE')
    waro_legal_iva_label: str = Field(default='', alias='WARO_LEGAL_IVA_LABEL')
    waro_legal_role_label: str = Field(
        default='Software de gestión',
        alias='WARO_LEGAL_ROLE_LABEL',
    )
    waro_legal_not_issuer_disclaimer: str = Field(
        default='No es el emisor de esta venta',
        alias='WARO_LEGAL_NOT_ISSUER_DISCLAIMER',
    )

    # Facturador técnico (Matias API / LOPEZSOFT S.A.S.) — print label only.
    # Public company data from matias-api.com/terminos (not Matias PAT secrets).
    facturador_legal_brand_name: str = Field(default='', alias='FACTURADOR_LEGAL_BRAND_NAME')
    facturador_legal_legal_name: str = Field(default='', alias='FACTURADOR_LEGAL_LEGAL_NAME')
    facturador_legal_nit: str = Field(default='', alias='FACTURADOR_LEGAL_NIT')
    facturador_legal_role_label: str = Field(
        default='Facturador técnico DIAN',
        alias='FACTURADOR_LEGAL_ROLE_LABEL',
    )
    facturador_legal_not_issuer_disclaimer: str = Field(
        default='No es el emisor de esta venta',
        alias='FACTURADOR_LEGAL_NOT_ISSUER_DISCLAIMER',
    )
    facturador_legal_city: str = Field(default='', alias='FACTURADOR_LEGAL_CITY')
    facturador_legal_support_email: str = Field(default='', alias='FACTURADOR_LEGAL_SUPPORT_EMAIL')

    # Platform operators — effective superuser on all tenants without tenant_members
    platform_superuser_emails: str = Field(default='', alias='PLATFORM_SUPERUSER_EMAILS')

    class Config:
        env_file = ".env"
        extra = "ignore"  # Ignore extra environment variables
    
    # Properties calculadas
    @property
    def db_connection_params(self) -> dict:
        return {
            "host": self.db_host,
            "port": self.db_port,
            "user": self.db_user,
            "password": self.db_password,
            "database": self.db_name,
        }
    
    @property
    def is_development(self) -> bool:
        return self.environment == "development"

settings = Settings()
