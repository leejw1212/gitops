"""worker — 큐에서 작업을 꺼내 카드를 만든다. 실패하면 재시도하고, 끝내 안 되면 DLQ 로.

실패는 두 종류다.
  - 다시 하면 될 수도 있는 것 (타임아웃, 5xx, 429)  → 잠시 묵혔다가 재시도
  - 다시 해도 똑같은 것       (404, 잘못된 주소)     → 바로 실패로 끝낸다

재시도는 RabbitMQ 의 메시지 TTL 과 데드레터를 엮어서 한다. 플러그인이 필요 없다.

  card.jobs ──실패──▶ card.retry.5s  ──5초 뒤──▶ card.jobs
            ──또 실패──▶ card.retry.15s ──15초 뒤──▶ card.jobs
            ──끝내 실패──▶ card.dlq      (사람이 볼 때까지 보관)

로그는 사건(event) 이름이 붙은 JSON 한 줄로 남긴다. Fluentd 가 글자를 뒤지는 대신
필드로 읽을 수 있게 하려는 것이다. 모든 줄에 request_id 를 붙여, 사용자가 누른
요청 하나를 ingress 부터 여기까지 한 번호로 따라갈 수 있게 한다.
"""
import json
import os
import signal
import sys
import time
import urllib.request

import pika

from card import build_card

RABBIT_URL = os.environ.get(
    "RABBIT_URL", "amqp://linkcard:linkcard-dev@rabbitmq:5672/%2F")
QUEUE = os.environ.get("QUEUE", "card.jobs")
DLQ = os.environ.get("DLQ", "card.dlq")
RESULT_URL = os.environ.get("RESULT_URL", "http://linkcard-web:8000/internal/result")
PREFETCH = int(os.environ.get("PREFETCH", "1"))

# 첫 시도를 포함한 최대 횟수. 3 이면 처음 1번 + 재시도 2번.
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
# n 번째 실패 뒤 기다릴 초. 뒤로 갈수록 길게 — 상대 서버가 숨 돌릴 틈을 준다.
RETRY_DELAYS = [int(x) for x in os.environ.get("RETRY_DELAYS", "5,15").split(",") if x.strip()]


def log(event, **fields):
    """한 줄에 JSON 하나. 글자가 아니라 필드로 남겨야 나중에 골라 볼 수 있다."""
    sys.stdout.write(json.dumps({"event": event, **fields}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def retry_queue(delay):
    return f"card.retry.{delay}s"


def declare_topology(ch):
    """큐를 선언한다. 이미 있으면 아무 일도 하지 않는다."""
    ch.queue_declare(queue=QUEUE, durable=True)
    ch.queue_declare(queue=DLQ, durable=True)
    for delay in RETRY_DELAYS:
        ch.queue_declare(
            queue=retry_queue(delay), durable=True,
            arguments={
                "x-message-ttl": delay * 1000,       # 이만큼 묵힌 뒤
                "x-dead-letter-exchange": "",         # 기본 교환기를 거쳐
                "x-dead-letter-routing-key": QUEUE,   # 원래 큐로 돌려보낸다
            })


def report(job_id, request_id, payload):
    """결과(또는 중간 상태)를 web 에 돌려준다."""
    body = json.dumps({"job_id": job_id, "request_id": request_id, **payload}).encode()
    # web 에 직접 보내는 요청이라 ingress 를 거치지 않는다. 번호를 직접 실어야
    # web 접근 로그의 이 줄에도 같은 request_id 가 찍힌다.
    headers = {"Content-Type": "application/json"}
    if request_id:
        headers["X-Request-ID"] = request_id
    req = urllib.request.Request(RESULT_URL, data=body, headers=headers)
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log("job.report_failed", request_id=request_id, job_id=job_id, error=str(e)[:200])


def publish(ch, queue, body, headers, request_id):
    """다시 넣을 때 correlation_id 를 반드시 같이 넘긴다.

    BasicProperties 를 새로 만들면 원래 메시지의 속성은 따라오지 않는다.
    headers 만 복사하고 correlation_id 를 빼먹으면, 재시도부터는 요청 번호가
    끊겨 "첫 시도만 추적되는" 로그가 된다.
    """
    ch.basic_publish(
        exchange="", routing_key=queue, body=body,
        properties=pika.BasicProperties(
            delivery_mode=2, content_type="application/json",
            headers=headers, correlation_id=request_id or None))


def handle(ch, method, props, body):
    headers = dict(props.headers or {})
    attempt = int(headers.get("x-attempt", 1))
    # web 이 넣어 둔 요청 번호. AMQP 에는 이런 용도로 correlation_id 라는 칸이 따로 있다.
    request_id = props.correlation_id or ""
    started = time.time()

    try:
        job = json.loads(body)
    except ValueError:
        # 읽을 수조차 없는 메시지. 다시 넣어 봐야 계속 실패하며 큐를 막는다.
        # 버리지 말고 DLQ 에 넣어 사람이 볼 수 있게 한다.
        publish(ch, DLQ, body, {**headers, "x-reason": "unparseable"}, request_id)
        ch.basic_ack(method.delivery_tag)
        log("job.unparseable", request_id=request_id, body=body[:80].decode("utf-8", "replace"))
        return

    job_id, url = job.get("job_id"), job.get("url", "")
    # 접수부터 지금(워커가 꺼낸 순간)까지. 첫 시도라면 순수하게 '큐에서 기다린 시간'이다.
    # 재시도는 같은 본문을 다시 넣으므로 재시도 대기(5초, 15초)까지 더해진다.
    queued_at = job.get("queued_at")
    wait_ms = round((started - queued_at) * 1000, 1) if queued_at else None
    log("job.start", request_id=request_id, job_id=job_id, attempt=attempt,
        max_attempts=MAX_ATTEMPTS, redelivered=method.redelivered, url=url, wait_ms=wait_ms)

    result = build_card(url)
    elapsed = round(time.time() - started, 2)
    result["worker_elapsed"] = elapsed
    result["attempt"] = attempt
    result["max_attempts"] = MAX_ATTEMPTS

    # ── 성공 ────────────────────────────────────────────────
    if result.get("ok"):
        report(job_id, request_id, result)
        ch.basic_ack(method.delivery_tag)
        log("job.done", request_id=request_id, job_id=job_id, attempt=attempt,
            wait_ms=wait_ms, elapsed=elapsed, steps=result.get("steps"))
        return

    # ── 다시 해 볼 만한 실패 ─────────────────────────────────
    if result.get("retryable") and attempt < MAX_ATTEMPTS:
        delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
        # 순서가 중요하다. 재시도 큐에 먼저 넣고, 그다음 원본을 ack 한다.
        # 반대로 하면 ack 와 publish 사이에 죽었을 때 작업이 통째로 사라진다.
        publish(ch, retry_queue(delay), body, {
            **headers, "x-attempt": attempt + 1,
            "x-last-error": str(result.get("error", ""))[:200]}, request_id)
        report(job_id, request_id, {**result, "status": "retrying", "next_retry_in": delay})
        ch.basic_ack(method.delivery_tag)
        log("job.retry_scheduled", request_id=request_id, job_id=job_id, attempt=attempt,
            delay=delay, code=result.get("code"), error=str(result.get("error", ""))[:120],
            elapsed=elapsed)
        return

    # ── 여기서 끝낸다 ────────────────────────────────────────
    if result.get("retryable"):
        # 재시도를 다 썼다. 처리하지 못한 작업이니 DLQ 에 보관한다.
        publish(ch, DLQ, body, {
            **headers, "x-attempt": attempt, "x-reason": "max_attempts",
            "x-last-error": str(result.get("error", ""))[:200]}, request_id)
        report(job_id, request_id, {**result, "status": "failed", "dead_lettered": True})
        log("job.dead_lettered", request_id=request_id, job_id=job_id, attempt=attempt,
            code=result.get("code"), error=str(result.get("error", ""))[:120], elapsed=elapsed)
    else:
        # 404 같은 것. 처리는 제대로 했고 답이 "없는 페이지" 일 뿐이다.
        # DLQ 는 '처리하지 못한 것' 을 두는 곳이지 '결과가 실패인 것' 을 두는 곳이 아니다.
        report(job_id, request_id, {**result, "status": "failed", "dead_lettered": False})
        log("job.failed", request_id=request_id, job_id=job_id, attempt=attempt,
            code=result.get("code"), error=str(result.get("error", ""))[:120], elapsed=elapsed)
    ch.basic_ack(method.delivery_tag)


_stopping = False
_connection = None
_channel = None


def _request_stop():
    """pika 루프 안에서 불린다. 여기서 소비를 멈추는 건 안전하다."""
    if _channel is not None:
        try:
            _channel.stop_consuming()
        except Exception:
            pass


def _on_sigterm(signum, frame):
    """쿠버네티스가 파드를 내릴 때 보내는 SIGTERM 을 받는다.

    핸들러가 없으면 이 컨테이너에서 python 이 PID 1 이라 SIGTERM 을 무시한다.
    그러면 쿠버네티스는 terminationGracePeriodSeconds(기본 30초)를 꼬박 기다렸다가
    SIGKILL 로 죽인다. 실제로 재 보니 파드가 사라지는 데 31.2초가 걸렸다.

    신호를 받으면 '멈춰라' 를 pika 루프에 맡긴다. 지금 처리 중인 작업은 끝내고
    ack 까지 한 뒤에 멈춘다.
    """
    global _stopping
    if _stopping:
        return
    _stopping = True
    log("worker.sigterm")
    if _connection is not None:
        try:
            _connection.add_callback_threadsafe(_request_stop)
        except Exception:
            pass


def main():
    global _connection, _channel
    signal.signal(signal.SIGTERM, _on_sigterm)
    params = pika.URLParameters(RABBIT_URL)
    params.heartbeat = 30
    while not _stopping:
        try:
            _connection = pika.BlockingConnection(params)
            _channel = _connection.channel()
            declare_topology(_channel)
            _channel.basic_qos(prefetch_count=PREFETCH)
            _channel.basic_consume(queue=QUEUE, on_message_callback=handle)
            log("worker.ready", queue=QUEUE, max_attempts=MAX_ATTEMPTS, delays=RETRY_DELAYS)
            _channel.start_consuming()
            # stop_consuming 으로 빠져나왔으면 정상 종료다.
            _connection.close()
        except Exception as e:
            if _stopping:
                break
            log("worker.reconnect", error=str(e)[:200], wait=5)
            time.sleep(5)
    log("worker.stopped")


if __name__ == "__main__":
    main()
