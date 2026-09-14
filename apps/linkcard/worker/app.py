"""worker — 큐에서 작업을 꺼내 카드를 만든다. 실패하면 재시도하고, 끝내 안 되면 DLQ 로.

실패는 두 종류다.
  - 다시 하면 될 수도 있는 것 (타임아웃, 5xx, 429)  → 잠시 묵혔다가 재시도
  - 다시 해도 똑같은 것       (404, 잘못된 주소)     → 바로 실패로 끝낸다

재시도는 RabbitMQ 의 메시지 TTL 과 데드레터를 엮어서 한다. 플러그인이 필요 없다.

  card.jobs ──실패──▶ card.retry.5s  ──5초 뒤──▶ card.jobs
            ──또 실패──▶ card.retry.15s ──15초 뒤──▶ card.jobs
            ──끝내 실패──▶ card.dlq      (사람이 볼 때까지 보관)

재시도 큐에는 소비자가 없다. 메시지가 TTL 만큼 묵으면 RabbitMQ 가 알아서
데드레터 목적지(card.jobs)로 되돌려 보낸다. 그게 "잠시 뒤 재시도" 가 된다.
"""
import json
import os
import signal
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


def report(job_id, payload):
    """결과(또는 중간 상태)를 web 에 돌려준다."""
    body = json.dumps({"job_id": job_id, **payload}).encode()
    req = urllib.request.Request(
        RESULT_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[worker] 결과 전달 실패 job={job_id}: {e}", flush=True)


def publish(ch, queue, body, headers):
    ch.basic_publish(
        exchange="", routing_key=queue, body=body,
        properties=pika.BasicProperties(
            delivery_mode=2, content_type="application/json", headers=headers))


def handle(ch, method, props, body):
    headers = dict(props.headers or {})
    attempt = int(headers.get("x-attempt", 1))
    started = time.time()

    try:
        job = json.loads(body)
    except ValueError:
        # 읽을 수조차 없는 메시지. 다시 넣어 봐야 계속 실패하며 큐를 막는다.
        # 버리지 말고 DLQ 에 넣어 사람이 볼 수 있게 한다.
        publish(ch, DLQ, body, {**headers, "x-reason": "unparseable"})
        ch.basic_ack(method.delivery_tag)
        print("[worker] 읽을 수 없는 메시지 → DLQ", flush=True)
        return

    job_id, url = job.get("job_id"), job.get("url", "")
    print(f"[worker] 시작 job={job_id} attempt={attempt}/{MAX_ATTEMPTS} "
          f"redelivered={method.redelivered} url={url}", flush=True)

    result = build_card(url)
    result["worker_elapsed"] = round(time.time() - started, 2)
    result["attempt"] = attempt
    result["max_attempts"] = MAX_ATTEMPTS

    # ── 성공 ────────────────────────────────────────────────
    if result.get("ok"):
        report(job_id, result)
        ch.basic_ack(method.delivery_tag)
        print(f"[worker] 완료 job={job_id} attempt={attempt} "
              f"elapsed={result['worker_elapsed']}s", flush=True)
        return

    # ── 다시 해 볼 만한 실패 ─────────────────────────────────
    if result.get("retryable") and attempt < MAX_ATTEMPTS:
        delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
        # 순서가 중요하다. 재시도 큐에 먼저 넣고, 그다음 원본을 ack 한다.
        # 반대로 하면 ack 와 publish 사이에 죽었을 때 작업이 통째로 사라진다.
        # 이 순서면 최악의 경우 같은 작업이 한 번 더 도는 것으로 끝난다.
        publish(ch, retry_queue(delay), body, {
            **headers, "x-attempt": attempt + 1,
            "x-last-error": str(result.get("error", ""))[:200]})
        report(job_id, {**result, "status": "retrying", "next_retry_in": delay})
        ch.basic_ack(method.delivery_tag)
        print(f"[worker] 재시도 예약 job={job_id} attempt={attempt} → {delay}초 뒤 "
              f"error={str(result.get('error', ''))[:60]}", flush=True)
        return

    # ── 여기서 끝낸다 ────────────────────────────────────────
    if result.get("retryable"):
        # 재시도를 다 썼다. 처리하지 못한 작업이니 DLQ 에 보관한다.
        publish(ch, DLQ, body, {
            **headers, "x-attempt": attempt, "x-reason": "max_attempts",
            "x-last-error": str(result.get("error", ""))[:200]})
        report(job_id, {**result, "status": "failed", "dead_lettered": True})
        print(f"[worker] 포기 job={job_id} attempt={attempt} → DLQ", flush=True)
    else:
        # 404 같은 것. 처리는 제대로 했고 답이 "없는 페이지" 일 뿐이다.
        # DLQ 는 '처리하지 못한 것' 을 두는 곳이지 '결과가 실패인 것' 을 두는 곳이 아니다.
        report(job_id, {**result, "status": "failed", "dead_lettered": False})
        print(f"[worker] 실패(재시도 안 함) job={job_id} code={result.get('code')}", flush=True)
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
    배포할 때마다 워커 하나당 30초가 버려지고, 그보다 긴 작업은 중간에 잘린다.

    신호를 받으면 '멈춰라' 를 pika 루프에 맡긴다. 지금 처리 중인 작업은 끝내고
    ack 까지 한 뒤에 멈춘다. 신호 처리기 안에서 pika 를 직접 만지면 루프 한가운데를
    건드리게 되므로, add_callback_threadsafe 로 루프가 안전한 시점에 부르게 한다.
    """
    global _stopping
    if _stopping:
        return
    _stopping = True
    print("[worker] SIGTERM 받음 — 하던 작업만 끝내고 종료", flush=True)
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
            print(f"[worker] 대기 중 queue={QUEUE} max_attempts={MAX_ATTEMPTS} "
                  f"delays={RETRY_DELAYS}", flush=True)
            _channel.start_consuming()
            # stop_consuming 으로 빠져나왔으면 정상 종료다.
            _connection.close()
        except Exception as e:
            if _stopping:
                break
            print(f"[worker] 연결 끊김, 5초 뒤 재시도: {e}", flush=True)
            time.sleep(5)
    print("[worker] 종료", flush=True)


if __name__ == "__main__":
    main()
