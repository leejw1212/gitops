"""worker — 큐에서 작업을 꺼내 카드를 만든다.

앞 편까지는 fetcher 가 HTTP 요청을 직접 받았다. 이제는 RabbitMQ 에서
꺼내 온다. 사용자는 이미 응답을 받고 떠난 뒤라, 여기서 8초가 걸리든
말든 아무도 기다리지 않는다.
"""
import base64
import json
import os
import time
import urllib.request

import pika

from card import build_card          # 카드 만드는 부분은 그대로 재사용

RABBIT_URL = os.environ.get(
    "RABBIT_URL", "amqp://linkcard:linkcard-dev@rabbitmq:5672/%2F")
QUEUE = os.environ.get("QUEUE", "card.jobs")
RESULT_URL = os.environ.get("RESULT_URL", "http://linkcard-web:8000/internal/result")
PREFETCH = int(os.environ.get("PREFETCH", "1"))


def report(job_id, payload):
    """결과를 web 에 돌려준다."""
    body = json.dumps({"job_id": job_id, **payload}).encode()
    req = urllib.request.Request(
        RESULT_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[worker] 결과 전달 실패 job={job_id}: {e}", flush=True)


def handle(ch, method, props, body):
    started = time.time()
    try:
        job = json.loads(body)
    except ValueError:
        # 읽을 수 없는 메시지는 되돌려 봐야 소용없다. 버린다.
        ch.basic_ack(method.delivery_tag)
        return

    job_id, url = job.get("job_id"), job.get("url", "")
    print(f"[worker] 시작 job={job_id} url={url}", flush=True)

    result = build_card(url)
    result["worker_elapsed"] = round(time.time() - started, 2)
    report(job_id, result)

    # 처리가 끝났다고 알린다. 이걸 보내야 큐에서 지워진다.
    ch.basic_ack(method.delivery_tag)
    print(f"[worker] 완료 job={job_id} ok={result.get('ok')} "
          f"elapsed={result['worker_elapsed']}s", flush=True)


def main():
    params = pika.URLParameters(RABBIT_URL)
    params.heartbeat = 30
    while True:
        try:
            conn = pika.BlockingConnection(params)
            ch = conn.channel()
            ch.queue_declare(queue=QUEUE, durable=True)
            # 한 번에 하나씩만 가져온다. 이게 없으면 워커 하나가 큐를
            # 통째로 쓸어 가서 다른 워커가 놀게 된다.
            ch.basic_qos(prefetch_count=PREFETCH)
            ch.basic_consume(queue=QUEUE, on_message_callback=handle)
            print(f"[worker] 대기 중 queue={QUEUE} prefetch={PREFETCH}", flush=True)
            ch.start_consuming()
        except Exception as e:
            print(f"[worker] 연결 끊김, 5초 뒤 재시도: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
