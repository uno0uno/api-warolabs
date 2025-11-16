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
    
    # AWS - nombres limpios
    aws_access_key_id: Optional[str] = Field(default=None, alias='NUXT_PRIVATE_AWS_ACCES_KEY_ID')
    aws_secret_access_key: Optional[str] = Field(default=None, alias='NUXT_PRIVATE_AWS_SECRET_ACCESS_KEY')
    aws_region: Optional[str] = Field(default=None, alias='NUXT_PRIVATE_AWS_REGION')
    aws_s3_bucket: str = Field(default='warocol-purchase-attachments', alias='NUXT_PRIVATE_AWS_S3_BUCKET')
    email_from: Optional[str] = Field(default=None, alias='NUXT_PRIVATE_EMAIL_FROM')
    
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