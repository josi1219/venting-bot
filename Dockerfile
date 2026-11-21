FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

RUN useradd --create-home bot && mkdir -p /data && chown bot:bot /data
ENV COUNTER_DB=/data/vent_counter.sqlite3
USER bot
CMD ["python", "bot.py"]
