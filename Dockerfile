# ZenFi Tech: funciona em qualquer hospedagem que rode Docker (Northflank, Railway, Koyeb, Fly.io, Cloud Run...).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m zenfi && mkdir -p /app/dados && chown -R zenfi /app
USER zenfi
EXPOSE 8000
CMD ["python", "servidor_zenfi_whatsapp.py"]
