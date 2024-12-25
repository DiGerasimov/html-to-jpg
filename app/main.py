from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, FileResponse
from html2image import Html2Image
import os
import uuid
import requests
import base64
import re
import urllib3
import logging
import traceback
import chardet
from config import settings
import hashlib
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Настраиваем логирование
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

if not settings.verify_ssl:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(
    docs_url=None,  # Отключаем Swagger UI
    redoc_url=None, # Отключаем ReDoc
    title="HTML to Image Converter",
    description="Сервис для к��нвертации HTML в изображения",
    version="1.0.0"
)

# Создаем директории если их нет
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
    app.mount("/fonts", StaticFiles(directory=fonts_dir), name="fonts")
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

def download_and_encode_image(url):
    try:
        response = requests.get(url, timeout=settings.http_timeout, verify=settings.verify_ssl)
        response.raise_for_status()
        image_content = response.content
        encoded = base64.b64encode(image_content).decode('utf-8')
        content_type = response.headers.get('content-type', 'image/png')
        return f"data:{content_type};base64,{encoded}"
    except Exception as e:
        logger.error(f"Ошибка загрузки изображения {url}: {str(e)}")
        logger.error(traceback.format_exc())
        return url

def process_html_with_images(html_content):
    try:
        # Паттерн для поиска URL в src трибутах
        img_pattern = r'src=[\'"]?(https?://[^\'" >]+)'
        # Паттерн для поиска URL в background-image стилях
        bg_pattern = r'background-image:\s*url\((https?://[^)]+)\)'
        
        def replace_with_base64(match):
            url = match.group(1)
            base64_data = download_and_encode_image(url)
            return f'src="{base64_data}"'
            
        def replace_bg_with_base64(match):
            url = match.group(1)
            base64_data = download_and_encode_image(url)
            return f'background-image: url({base64_data})'
        
        # Змеяе все src изображения
        processed_html = re.sub(img_pattern, replace_with_base64, html_content)
        # Заменяем все background-image
        processed_html = re.sub(bg_pattern, replace_bg_with_base64, processed_html)
        
        return processed_html
    except Exception as e:
        logger.error(f"Ошибка обработки HTML: {str(e)}")
        logger.error(traceback.format_exc())
        raise

def get_cached_image(url: str, cache_dir: str) -> str:
    """Скачивает изображение и кэширует его, возвращает путь к локальному файлу"""
    # Создаем хеш от URL для имени файла
    url_hash = hashlib.md5(url.encode()).hexdigest()
    file_ext = url.split('.')[-1].lower()
    cached_path = Path(cache_dir) / f"{url_hash}.{file_ext}"
    
    if not cached_path.exists():
        logger.info(f"Скачивание изобраения {url}")
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
    summary="Конвртировать HTML файл  изображение",
    response_description="PNG изображение"
)
async def convert_html_to_image(
    html_file: UploadFile = File(...),
    width: int = Form(...),
    height: int = Form(...)
):
    try:
        logger.info("Начало конвертации HTML в изображение")
        
        # Читаем содержимое файла как байты
        content = await html_file.read()
        
        # Определяем кодировку автоматически
        detected = chardet.detect(content)
        encoding = detected['encoding']
        
        logger.debug(f"Определена кодировка файла: {encoding}")
        
        try:
            html_content = content.decode(encoding)
        except Exception as e:
            logger.error(f"Ошибка декодирования с {encoding}: {str(e)}")
            # Пробуем запасной вариант с cp1251
            try:
                html_content = content.decode('cp1251')
                logger.debug("Успешно декодировано с cp1251")
            except Exception as e2:
                logger.error(f"Ошибка декодирования с cp1251: {str(e2)}")
                raise HTTPException(
                    status_code=400,
                    detail="Не уалос правильно прочиать файл. Убедитесь, что файл в кодировке UTF-8 или Windows-1251"
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
                '--disable-extensions'
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
        
        logger.debug("Обработка HTML и встраивание изображений")
        # Удляем теги style из HTML, так как стили будут переданы отдельно
        processed_html = re.sub(style_pattern, '', process_html_with_images(html_content))
        
        filename = f"image_{uuid.uuid4()}.png"
        full_path = os.path.join(settings.temp_dir, filename)
        
        # Удаляем проверку и чтение внешнего CSS файла
        css_content = base_styles + html_styles
        logger.debug(f"Содержимое CSS: {css_content[:100]}...")
        
        logger.debug("Создание скриншота")
        hti.screenshot(
            html_str=processed_html,
            css_str=css_content,
            save_as=filename,
            size=(width, height)
        )
        
        if not os.path.exists(full_path):
            logger.error(f"Не удалось создать файл изображения: {full_path}")
            raise HTTPException(status_code=500, detail="Failed to create image")
        
        logger.info("Изображение успешно создано")
        
        async def background_cleanup():
            try:
                os.remove(full_path)
                logger.debug(f"Временный файл удален: {full_path}")
            except Exception as e:
                logger.error(f"Ошибка при удалении временного файла: {str(e)}")
        
        return FileResponse(
            full_path, 
            media_type="image/png", 
            background=background_cleanup
        )
        
    except Exception as e:
        logger.error(f"Ошибка конвертации: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/render-card", response_class=FileResponse)
async def render_card(
    vjuh: str,
    bg: str,
    name: str,
    text: str
):
    try:
        logger.info("Начало рендеринга карточки")
        
        # Создаем директорию для кэша изображений
        cache_dir = os.path.join(settings.static_dir, 'cache')
        os.makedirs(cache_dir, exist_ok=True)
        
        # Скачиваем и кэшируем изображения
        vjuh_path = get_cached_image(vjuh, cache_dir)
        bg_path = get_cached_image(bg, cache_dir)
        
        # Читем шаблон HTML
        template_path = os.path.join(settings.static_dir, 'index.html')
        with open(template_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        # Заменяем placeholder'ы в HTML
        html_content = html_content.replace('https://cdek25.ru/cards/1.png', f'file://{bg_path}')
        html_content = html_content.replace('https://cdek25.ru/cards/v1.png', f'file://{vjuh_path}')
        html_content = html_content.replace('Константин Викторович Фамильцев', name)
        html_content = html_content.replace('Пусть у тебя в жизни будет ...', text)
        
        # Обновляем настройки для Html2Image
        hti = Html2Image(
            output_path=settings.static_dir,
            custom_flags=[
                '--no-sandbox',
                '--disable-gpu',
                '--headless',
                '--hide-scrollbars',
                '--force-device-scale-factor=1',
                '--window-size=1920,1080',
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
                '--screenshot-clip=0,0,1920,1080',  # Явно указываем область скриншота
                '--hide-scrollbars',
                '--force-device-scale-factor=1'
            ]
        )
        
        # Добавляем дополнительные стили для фиксации размеров
        html_content = html_content.replace('</head>',
            '''
            <style>
                html, body {
                    width: 1920px !important;
                    height: 1080px !important;
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
                    height: 1080px !important;
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
        
        logger.debug("Создание скриншота карточки")
        
        hti.screenshot(
            html_str=html_content,
            save_as=filename,
            size=(1920, 1080)
        )
        
        if not os.path.exists(full_path):
            logger.error(f"Не удалось создать файл изображения: {full_path}")
            raise HTTPException(status_code=500, detail="Failed to create image")
        
        logger.info("Карточка успешно создана")
        
        async def background_cleanup():
            try:
                os.remove(full_path)
                logger.debug(f"Временный файл карточки удален: {full_path}")
            except Exception as e:
                logger.error(f"Ошибка при удалении временного файла карточки: {str(e)}")
        
        return FileResponse(
            full_path,
            media_type="image/png",
            background=background_cleanup
        )
        
    except Exception as e:
        logger.error(f"Ошибка рендеринга карточки: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
