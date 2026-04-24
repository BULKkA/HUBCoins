FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY astana_hub_farmer.py .

# history.json будет храниться в volume
VOLUME ["/app/data"]

ENV HISTORY_FILE=/app/data/history.json

CMD ["python", "astana_hub_farmer.py"]