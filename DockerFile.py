FROM python:3.11-slim

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код
COPY . .

# Создаем папку для данных (чтобы база не удалялась при перезапуске)
RUN mkdir -p /app/data

# Запускаем сервер ключей в фоне (&), а бота — как основной процесс
CMD ["sh", "-c", "python license_server.py & python main.py"]