FROM python:3.9-slim

# Установка необходимых пакетов
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    dbus \
    && rm -rf /var/lib/apt/lists/*

# Настройка DBus
RUN mkdir -p /var/run/dbus
RUN dbus-uuidgen > /var/lib/dbus/machine-id

# Создание директорий
WORKDIR /app
RUN mkdir -p /app/static
RUN mkdir -p /app/temp
RUN chmod 777 /app/temp


# Установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование приложения
COPY ./app .

# Создаем скрипт для запуска
RUN echo '#!/bin/bash\n\
mkdir -p /var/run/dbus\n\
dbus-daemon --system --fork\n\
uvicorn main:app --host 0.0.0.0 --port 8000' > /start.sh

RUN chmod +x /start.sh

CMD ["/start.sh"] 