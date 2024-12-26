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
    
    # Настройки ограничения запросов
    rate_limit: int = 10000  # Количество запросов
    rate_limit_window: int = 60  # Временное окно в секундах
    
    # Redis настройки
    redis_host: str = "redis"
    redis_port: int = 6379
    
    # Глобальные лимиты
    global_rate_limit: int = 100  # Максимальное количество одновременных запросов
    max_queue_size: int = 1000    # Максимальный размер очереди
    rate_limit_window: int = 60   # Окно в секундах
    
    # Таймаут ожидания в очереди (в секундах)
    wait_timeout: int = 60
    
    class Config:
        env_file = ".env"

settings = Settings() 