from pydantic import BaseSettings

class Settings(BaseSettings):
    temp_dir: str
    static_dir: str
    allowed_origins: str
    max_upload_size: int
    http_timeout: int
    verify_ssl: bool
    allowed_methods: str
    allowed_headers: str
    allow_credentials: bool

    class Config:
        env_file = ".env"

settings = Settings() 