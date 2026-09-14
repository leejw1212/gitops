FROM python:3.12-slim

# 한글 제목이 네모로 깨지지 않게 나눔고딕을 넣는다.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-nanum \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 8000

# ★ 개발 서버가 아니라 gunicorn 을 쓴다.
#   워커 수를 2 로 못 박아 둔 것이 중요하다. 개발 서버는 요청마다 스레드를
#   띄워 주므로 '느린 요청이 남을 막는' 문제가 가려진다. 실제 배포는
#   워커 수가 정해져 있고, 그게 막히면 서비스 전체가 막힌다.
CMD ["gunicorn", "-w", "2", "-k", "sync", "-t", "60", \
     "--access-logfile", "-", "-b", "0.0.0.0:8000", "app:app"]
