FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 8000
# fetcher 와 같은 이유로 gunicorn 을 쓴다. 워커 2개 고정.
CMD ["gunicorn", "-w", "2", "-k", "sync", "-t", "60", \
     "--access-logfile", "-", "-b", "0.0.0.0:8000", "app:app"]
