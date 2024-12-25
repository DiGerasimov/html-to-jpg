#!/bin/bash
# Запускаем dbus
mkdir -p /var/run/dbus
dbus-daemon --system --fork

# Запускаем приложение
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 