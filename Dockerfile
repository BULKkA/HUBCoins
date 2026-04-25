FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY astana_hub_farmer.py .
COPY cookie_server.py .
COPY cookie_updater.html .

VOLUME ["/app/data"]

ENV HISTORY_FILE=/app/data/history.json
ENV COOKIES_FILE=/app/data/cookies.json
ENV COOKIE_SERVER_PORT=8765

EXPOSE 8765

CMD ["python", "astana_hub_farmer.py"]