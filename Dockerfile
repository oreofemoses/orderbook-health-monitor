FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py debug.py defaults.py favicon.ico dashboard.html start.sh ./
RUN chmod +x start.sh

RUN mkdir -p /app/data

EXPOSE 8080

CMD ["./start.sh"]