FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y git libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DC_DB_DIR=/app/data
ENV DB_PATH=/app/data/bouncer.db
ENV U2NET_HOME=/app/data/u2net
ENV REMBG_HOME=/app/data/u2net
VOLUME /app/data
CMD ["python", "-u", "bot.py", "serve"]
