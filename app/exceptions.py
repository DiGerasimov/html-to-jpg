from fastapi import HTTPException

class ImageConverterException(HTTPException):
    """Базовый класс для исключений сервиса конвертации изображений"""
    pass

class UserRateLimitExceeded(ImageConverterException):
    """Превышен лимит запросов для конкретного пользователя"""
    def __init__(self, retry_after: int = 60):
        super().__init__(
            status_code=429,
            detail={
                "message": "Превышен лимит запросов. Пожалуйста, подождите перед следующим запросом.",
                "retry_after": retry_after
            },
            headers={"Retry-After": str(retry_after)}
        )

class SystemOverloadedException(ImageConverterException):
    """Превышен глобальный лимит системы"""
    def __init__(self, queue_length: int, max_queue: int):
        super().__init__(
            status_code=429,
            detail={
                "message": f"Система перегружен�� ({queue_length}/{max_queue}). Пожалуйста, повторите запрос позже.",
                "queue_length": queue_length,
                "max_queue": max_queue
            }
        )

class ImageProcessingError(ImageConverterException):
    """Ошибка при обработке изображения"""
    def __init__(self, detail: str = "Произошла ошибка при обработке изображения"):
        super().__init__(
            status_code=500,
            detail={
                "message": detail,
                "error_type": "processing_error"
            }
        ) 