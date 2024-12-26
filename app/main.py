from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, FileResponse, Response
from fastapi.exceptions import RequestValidationError
from html2image import Html2Image
import os
import uuid
import requests
import base64
import re
import urllib3
import logging
import chardet
from config import settings
import hashlib
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from PIL import Image
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from redis import Redis
from redis.exceptions import RedisError
import time
import asyncio
from exceptions import UserRateLimitExceeded, SystemOverloadedException, ImageProcessingError, ImageConverterException

# Базовая настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not settings.verify_ssl:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(
    docs_url=None,  # Отключаем Swagger UI
    redoc_url=None, # Отключаем ReDoc
    title="HTML to Image Converter",
    description="Сервис для конвертации HTML в изображения",
    version="1.0.0"
)

# Создаем директорию если их нет
os.makedirs(settings.temp_dir, exist_ok=True)
os.makedirs(settings.static_dir, exist_ok=True)

# Проверяем и монтируем основную static директорию
if os.path.exists(settings.static_dir):
    app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
else:
    raise RuntimeError(f"Directory '{settings.static_dir}' does not exist")

# Монтируем директорию со шрифтами
fonts_dir = os.path.join(settings.static_dir, 'fonts')
if os.path.exists(fonts_dir):
    app.mount("/static/fonts", StaticFiles(directory=fonts_dir), name="fonts")
else:
    logger.warning(f"Directory '{fonts_dir}' does not exist")

# Настройка CORS
origins = settings.allowed_origins.split(',')
methods = settings.allowed_methods.split(',')
headers = settings.allowed_headers.split(',')

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=settings.allow_credentials,
    allow_methods=methods,
    allow_headers=headers,
)

# Middleware для ограничения размера загружаемых файлов
class LimitUploadSizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "POST" and request.url.path == "/convert":
            content_length = request.headers.get('content-length')
            if content_length and int(content_length) > settings.max_upload_size:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Размер файла превышает допустимый лимит."}
                )
        return await call_next(request)

app.add_middleware(LimitUploadSizeMiddleware)

# Инициализация Redis и лимитера
redis = Redis(host=settings.redis_host, port=settings.redis_port, db=0)

# Определяем GlobalRateLimiter до его использования
class GlobalRateLimiter:
    def __init__(self, redis_client):
        self.redis = redis_client
        self.queue_key = "request_queue"
        self.processing_key = "processing_requests"
        self.lock_key = "processing_lock"
        self.wait_timeout = settings.wait_timeout  # Берем значение из конфига

    async def check_limit(self):
        try:
            # Атомарно получаем текущее состояние
            current_processing = int(self.redis.get(self.processing_key) or 0)
            
            logger.info(f"Текущая загрузка: {current_processing}/{settings.global_rate_limit}")
            
            if current_processing >= settings.global_rate_limit:
                # Ждем освобождения слота
                timeout = time.time() + self.wait_timeout
                while time.time() < timeout:
                    current_processing = int(self.redis.get(self.processing_key) or 0)
                    if current_processing < settings.global_rate_limit:
                        break
                    await asyncio.sleep(0.1)
                else:
                    # Если превышен таймаут
                    raise SystemOverloadedException(
                        queue_length=current_processing,
                        max_queue=settings.global_rate_limit
                    )

            # Увеличиваем счетчик обрабатываемых запросов
            self.redis.incr(self.processing_key)
            self.redis.expire(self.processing_key, 60)  # TTL 60 секунд

        except (SystemOverloadedException, RedisError) as e:
            logger.error(f"Ошибка при проверке лимитов: {str(e)}")
            raise

    async def release(self):
        try:
            current = int(self.redis.get(self.processing_key) or 0)
            if current > 0:  # Проверяем, чтобы не уйти в отрицательные значения
                self.redis.decr(self.processing_key)
        except Exception as e:
            logger.error(f"Ошибка при освобождении ресурса: {str(e)}")

# Теперь определяем middleware, который использует GlobalRateLimiter
class GlobalRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.limiter = GlobalRateLimiter(redis)

    async def dispatch(self, request: Request, call_next):
        try:
            await self.limiter.check_limit()
            response = await call_next(request)
            return response
        except SystemOverloadedException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content=exc.detail,
                headers={"Retry-After": "60"}  # Увеличиваем до 60 секунд
            )
        finally:
            await self.limiter.release()

# Добавляем middleware
app.add_middleware(GlobalRateLimitMiddleware)

# Перемещаем все обработчики исключений в одно место
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={"message": "Ошибка в данных запросе. Проверьте правильность введенных данных."}
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"message": exc.detail}
    )

@app.exception_handler(ImageConverterException)
async def image_converter_exception_handler(request: Request, exc: ImageConverterException):
    return JSONResponse(
        status_code=exc.status_code,
        headers=getattr(exc, 'headers', None),
        content=exc.detail
    )

@app.exception_handler(SystemOverloadedException)
async def system_overloaded_exception_handler(request: Request, exc: SystemOverloadedException):
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.detail,
        headers={"Retry-After": "30"}  # Рекомендуем подождать 30 секунд
    )

@app.exception_handler(RateLimitExceeded)
async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    return await image_converter_exception_handler(
        request, 
        UserRateLimitExceeded(retry_after=60)
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error(f"Неожиданная ошибка: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={"message": "Произошла внутренняя ошибка сервера. Попробуйте позже."}
    )

def download_and_encode_image(url):
    try:
        logger.info(f"Загрузка изображения: {url}")
        response = requests.get(url, timeout=settings.http_timeout, verify=settings.verify_ssl)
        response.raise_for_status()
        image_content = response.content
        encoded = base64.b64encode(image_content).decode('utf-8')
        content_type = response.headers.get('content-type', 'image/png')
        result = f"data:{content_type};base64,{encoded}"
        logger.info(f"Изображение успешно загружено и закод��ровано: {url}")
        return result
    except Exception as e:
        logger.error(f"Ошибка загрузки изображения {url}: {str(e)}")
        raise ImageProcessingError(f"Не удалось загрузить изображение {url}: {str(e)}")

def process_html_with_images(html_content):
    try:
        img_pattern = r'src=[\'"]?(https?://[^\'" >]+)'
        bg_pattern = r'background-image:\s*url\((https?://[^)]+)\)'
        
        def replace_with_base64(match):
            url = match.group(1)
            base64_data = download_and_encode_image(url)
            return f'src="{base64_data}"'
            
        def replace_bg_with_base64(match):
            url = match.group(1)
            base64_data = download_and_encode_image(url)
            return f'background-image: url({base64_data})'
        
        processed_html = re.sub(img_pattern, replace_with_base64, html_content)
        processed_html = re.sub(bg_pattern, replace_bg_with_base64, processed_html)
        
        return processed_html
    except Exception as e:
        logger.error(f"Ошибка обработки HTML: {str(e)}")
        raise

def get_cached_image(url: str, cache_dir: str) -> str:
    url_hash = hashlib.md5(url.encode()).hexdigest()
    file_ext = url.split('.')[-1].lower()
    cached_path = Path(cache_dir) / f"{url_hash}.{file_ext}"
    
    if not cached_path.exists():
        response = requests.get(url, verify=settings.verify_ssl)
        response.raise_for_status()
        cached_path.write_bytes(response.content)
    
    return str(cached_path)

def create_screenshot_with_selenium(html_content, output_path):
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--window-size=1920,1080')
    
    driver = webdriver.Chrome(options=chrome_options)
    try:
        # Сохраняем HTML во временный файл
        temp_html = os.path.join(settings.temp_dir, 'temp.html')
        with open(temp_html, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        driver.get(f'file://{temp_html}')
        
        # Ждем загрузки всех элементов
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script('return document.readyState') == 'complete'
        )
        
        # Делаем скриншот
        driver.save_screenshot(output_path)
    finally:
        driver.quit()
        if os.path.exists(temp_html):
            os.remove(temp_html)

@app.post("/convert", 
    response_class=FileResponse,
    summary="Конвертировать HTML файл в изображение",
    response_description="PNG изображение"
)
async def convert_html_to_image(
    request: Request,
    html_file: UploadFile = File(...),
    width: int = Form(...),
    height: int = Form(...)
):
    full_path = None
    try:
        content = await html_file.read()
        detected = chardet.detect(content)
        encoding = detected['encoding']
        
        try:
            html_content = content.decode(encoding)
        except Exception:
            try:
                html_content = content.decode('cp1251')
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="Не удалось правильно прочитать файл. Убедитесь, что файл в кодировке UTF-8 или Windows-1251"
                )

        hti = Html2Image(
            output_path=settings.temp_dir,
            custom_flags=[
                '--no-sandbox',
                '--disable-gpu',
                '--disable-dev-shm-usage',
                '--headless',
                '--hide-scrollbars',
                '--force-device-scale-factor=2',
                '--window-size=1920,1080',
                '--font-render-hinting=medium',
                '--disable-setuid-sandbox',
                '--no-first-run',
                '--no-default-browser-check',
                '--disable-extensions',
                '--log-level=3',  # FATAL
                '--silent',
                '--disable-logging',
                '--disable-ipc-flooding-protection',
                '--disable-notifications'
            ]
        )
        
        # Извлекаем стили из HTML
        style_pattern = r'<style[^>]*>(.*?)</style>'
        html_styles = ' '.join(re.findall(style_pattern, html_content, re.DOTALL))
        
        # Базовые стили
        base_styles = """
        @font-face {
            font-family: 'PTSans';
            src: url('file:///app/static/fonts/PT_Sans-Web-Bold.ttf') format('truetype');
            font-weight: bold;
            font-style: normal;
        }
        @font-face {
            font-family: 'Inter';
            src: url('file:///app/static/fonts/Inter_18pt-Regular.ttf') format('truetype');
            font-weight: normal;
            font-style: normal;
        }
        * {
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
            text-rendering: optimizeLegibility;
        }
        html, body {
            margin: 0;
            padding: 0;
            height: 100%;
            overflow: hidden;
        }
        """
        
        # Удляем теги style из HTML, так как стили будут переданы отдельно
        processed_html = re.sub(style_pattern, '', process_html_with_images(html_content))
        
        filename = f"image_{uuid.uuid4()}.png"
        full_path = os.path.join(settings.temp_dir, filename)
        
        # Удаляем проверку и чтение внешнего CSS файла
        css_content = base_styles + html_styles
        
        hti.screenshot(
            html_str=processed_html,
            css_str=css_content,
            save_as=filename,
            size=(width, height)
        )
        
        if not os.path.exists(full_path):
            raise ImageProcessingError("Не удалось создать изображение")
            
        # Добавляем обработку и обрезку изображения
        try:
            with Image.open(full_path) as img:
                # Обрезаем нижние 420 пикселей
                cropped_img = img.crop((0, 0, 1920, 1080))  # 1500 - 420 = 1080
                # Сохраняем обрезанное изображение
                cropped_img.save(full_path, 'PNG')
        except Exception as e:
            raise ImageProcessingError(f"Ошибка при обработке изображения: {str(e)}")
        
        # Читаем файл в память перед отправкой
        with open(full_path, 'rb') as f:
            image_data = f.read()
            
        # Удаляем файл сразу после чтения
        if os.path.exists(full_path):
            os.remove(full_path)
            
        # Возвращаем Response с данными из памяти
        return Response(
            content=image_data,
            media_type="image/png"
        )
        
    except ImageConverterException:
        raise
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {str(e)}")
        raise ImageProcessingError("Произошла внутренняя ошибка сервера")

@app.get("/render-card", response_class=FileResponse)
async def render_card(
    request: Request,
    name: str,
    text: str,
    vjuh: str = "https://cdek25.ru/cards/v1.png",
    bg: str = "https://cdek25.ru/cards/1.png"
):
    # Проверяем и устанавливаем дефолтные значения для изображений
    if not bg.startswith("https://cdek25.ru/cards/"):
        bg = "https://cdek25.ru/cards/1.png"
    
    if not vjuh.startswith("https://cdek25.ru/cards/"):
        vjuh = "https://cdek25.ru/cards/v1.png"

    # Добавляем проверку длины параметров
    if len(name) > 45:
        name = name[:45] + "..."
    
    if len(text) > 200:
        text = text[:200] + "..."

    full_path = None
    try:
        template_path = os.path.join(settings.static_dir, 'index.html')
        with open(template_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        try:
            # Загружаем изображения
            logger.info("Начало загрузки изображений")
            try:
                bg_data = download_and_encode_image(bg)
            except:
                logger.warning(f"Не удалось загрузить фоновое изображение {bg}, использую дефолтное")
                bg_data = download_and_encode_image("https://cdek25.ru/cards/1.png")
            
            try:
                vjuh_data = download_and_encode_image(vjuh)
            except:
                logger.warning(f"Не удалось загрузить вжух {vjuh}, использую дефолтный")
                vjuh_data = download_and_encode_image("https://cdek25.ru/cards/v1.png")
                
            logger.info("Изображения успешно загружены")
            
            # Заменяем placeholder'ы в HTML
            html_content = html_content.replace("url('placeholder_bg')", f"url('{bg_data}')")
            html_content = html_content.replace('placeholder_vjuh', vjuh_data)
            html_content = html_content.replace('Константин Викторович Фамильцев', name)
            html_content = html_content.replace('Пусть у тебя в жизни будет ...', text)
            
        except ImageProcessingError as e:
            logger.error(f"Ошибка при обработке изображений: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Неожиданная ошибка при обработке изображений: {str(e)}")
            raise ImageProcessingError("Ошибка при обработке изображений")

        # Обновляем настройки для Html2Image
        hti = Html2Image(
            output_path=settings.static_dir,
            custom_flags=[
                '--no-sandbox',
                '--disable-gpu',
                '--headless',
                '--hide-scrollbars',
                '--lang=ru',
                '--font-render-hinting=none',
                '--disable-font-subpixel-positioning',
                '--force-device-scale-factor=1',
                '--window-size=1920,1500',
                '--disable-setuid-sandbox',
                '--disable-software-rasterizer',
                '--disable-dev-shm-usage',
                '--ignore-certificate-errors',
                '--disable-web-security',
                '--allow-file-access-from-files',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
                '--run-all-compositor-stages-before-draw',
                '--screenshot-clip=0,0,1920,1500',
                '--hide-scrollbars',
                '--force-device-scale-factor=1',
                '--log-level=3',  # FATAL
                '--silent',
                '--disable-logging',
                '--disable-extensions',
                '--disable-ipc-flooding-protection',
                '--disable-notifications'
            ]
        )
        
        # Заменяем относительные пути на абсолютные для шрифтов
        html_content = html_content.replace(
            "url('/fonts/",
            f"url('file://{os.path.join(settings.static_dir, 'fonts', '')}"
        )
        
        # Добавляем дополнительные стили для фиксации размеров
        html_content = html_content.replace('</head>',
            '''
            <style>
                html, body {
                    width: 1920px !important;
                    height: 1500px !important;
                    margin: 0 !important;
                    padding: 0 !important;
                    overflow: hidden !important;
                    background: transparent !important;
                    position: fixed !important;
                    top: 0 !important;
                    left: 0 !important;
                }
                #card {
                    width: 1920px !important;
                    height: 1500px !important;
                    margin: 0 !important;
                    padding: 32px !important;
                    position: absolute !important;
                    box-sizing: border-box !important;
                    background-size: cover !important;
                    background-position: center !important;
                    display: block !important;
                    transform: translateZ(0) !important;
                    -webkit-transform: translateZ(0) !important;
                    top: 0 !important;
                    left: 0 !important;
                }
            </style>
            </head>
            '''
        )
        
        # Добавляем скрипт для гарантированной загрузки
        html_content = html_content.replace('</body>',
            '''
            <script>
                window.onload = function() {
                    document.documentElement.style.backgroundColor = 'transparent';
                    document.body.style.backgroundColor = 'transparent';
                };
            </script>
            </body>
            '''
        )
        
        filename = f"card_{uuid.uuid4()}.png"
        full_path = os.path.join(settings.static_dir, filename)
        
        hti.screenshot(
            html_str=html_content,
            save_as=filename,
            size=(1920, 1500)
        )
        
        if not os.path.exists(full_path):
            raise ImageProcessingError("Не удалось создать карточку")
            
        # Добавляем обработку и обрезку изображ��ния
        try:
            with Image.open(full_path) as img:
                # Обрезаем нижние 420 пикселей
                cropped_img = img.crop((0, 0, 1920, 1080))  # 1500 - 420 = 1080
                # Сохраняем обрезанное изображение
                cropped_img.save(full_path, 'PNG')
        except Exception as e:
            raise ImageProcessingError(f"Ошибка при обработке карточки: {str(e)}")
        
        # Читаем файл в память перед отправкой
        with open(full_path, 'rb') as f:
            image_data = f.read()
            
        # Удаляем файл сразу после чтения
        if os.path.exists(full_path):
            os.remove(full_path)
            
        # Возвращаем Response с данными из памяти
        return Response(
            content=image_data,
            media_type="image/png"
        )
        
    except ImageConverterException:
        raise
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {str(e)}")
        raise ImageProcessingError("Произошла внутренняя ошибка сервера")
