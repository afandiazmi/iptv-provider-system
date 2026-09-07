FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY iptvprovider ./iptvprovider

ENV IPTV_DATA_DIR=/app/data
VOLUME ["/app/data"]
EXPOSE 8302

CMD ["python", "app.py"]
