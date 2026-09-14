FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py gunicorn.conf.py ./
EXPOSE 8000
# 워커 수·접근 로그 형식은 gunicorn.conf.py 에 있다.
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
