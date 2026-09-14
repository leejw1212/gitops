"""web — 사용자가 URL 을 넣는 화면과 API.

POST /api/cards 는 작업을 큐(RabbitMQ)에 넣고 곧바로 202 를 돌려준다.
실제 작업은 worker 가 뒤에서 하고, 결과를 /internal/result 로 알려 준다.
결과는 Redis 에 둬서 web 파드가 여럿이어도 어느 파드든 조회할 수 있다.

로그는 사건(event) 이름이 붙은 JSON 한 줄로 남긴다. 모든 줄에 request_id 를
붙여, 사용자가 누른 요청 하나를 ingress → web → worker 로 한 번호로 따라간다.
"""
import json as _json
import os
import sys
import time
import uuid

import pika
import redis
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# 인그레스가 /linkcard 를 떼고 넘겨 주기 때문에, 앱은 자기가 하위 경로에
# 있다는 걸 모른다. 그대로 두면 화면의 JS 가 /api/cards 를 절대 경로로
# 불러서 엉뚱한 서비스로 간다. 그래서 접두어를 밖에서 알려 준다.
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")

RABBIT_URL = os.environ.get(
    "RABBIT_URL", "amqp://linkcard:linkcard-dev@rabbitmq:5672/%2F")
QUEUE = os.environ.get("QUEUE", "card.jobs")

# 결과는 Redis 에 둔다. 파드 메모리에 두면 접수한 파드와 조회하는 파드가
# 달라 404 가 난다. 실제로 그랬다 — 파드를 여러 개 띄우려면 상태를
# 파드 밖에 둬야 한다.
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
RESULT_TTL = int(os.environ.get("RESULT_TTL", "3600"))
_r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def log(event, **fields):
    """한 줄에 JSON 하나.

    app.logger 로 남기면 gunicorn 이 앞에 '[시각] [pid] [INFO]' 를 붙여서
    한 줄이 JSON 이 아니게 된다. 그러면 Fluentd 가 필드로 펼치지 못한다.
    그래서 표준 출력에 JSON 만 곧바로 쓴다.
    """
    fields = {k: v for k, v in fields.items() if v is not None}
    sys.stdout.write(_json.dumps({"event": event, **fields}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _request_id():
    """ingress-nginx 가 붙여 준 요청 번호. 없으면(파드에 직접 들어온 요청) 새로 만든다."""
    return request.headers.get("X-Request-ID") or uuid.uuid4().hex


def _save(job_id, data):
    _r.setex(f"job:{job_id}", RESULT_TTL, _json.dumps(data))


def _load(job_id):
    raw = _r.get(f"job:{job_id}")
    return _json.loads(raw) if raw else None


def _publish(job_id, url, request_id):
    """큐에 작업을 넣는다. 넣기만 하고 결과는 기다리지 않는다."""
    params = pika.URLParameters(RABBIT_URL)
    params.heartbeat = 30
    conn = pika.BlockingConnection(params)
    try:
        ch = conn.channel()
        ch.queue_declare(queue=QUEUE, durable=True)   # 브로커가 죽어도 큐는 남는다
        ch.basic_publish(
            exchange="", routing_key=QUEUE,
            body=_json.dumps({"job_id": job_id, "url": url}),
            properties=pika.BasicProperties(
                delivery_mode=2,                       # 메시지도 디스크에 남긴다
                content_type="application/json",
                # AMQP 메시지에는 이런 용도로 correlation_id 라는 칸이 따로 있다.
                # 워커가 이 번호를 읽어 같은 번호로 로그를 남긴다.
                correlation_id=request_id),
        )
    finally:
        conn.close()

PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>링크 미리보기 카드</title>
<style>
 :root { color-scheme: light dark; }
 body { font-family: system-ui, -apple-system, "Apple SD Gothic Neo", sans-serif;
        max-width: 720px; margin: 0 auto; padding: 32px 20px; line-height: 1.6; }
 h1 { font-size: 20px; margin: 0 0 4px; }
 p.sub { color: #888; font-size: 13px; margin: 0 0 24px; }
 form { display: flex; gap: 8px; }
 input { flex: 1; min-width: 0; padding: 10px 12px; font-size: 14px;
         border: 1px solid #ccc; border-radius: 10px; }
 button { padding: 10px 18px; font-size: 14px; font-weight: 600; cursor: pointer;
          border: 0; border-radius: 10px; background: #4f46e5; color: #fff; }
 button:disabled { opacity: .5; cursor: progress; }
 .card { margin-top: 24px; border: 1px solid #ddd; border-radius: 14px;
         overflow: hidden; display: none; }
 .card img { width: 100%; display: block; background: #f3f4f6; }
 .card .body { padding: 14px 16px; }
 .card .t { font-weight: 700; margin-bottom: 6px; }
 .card .d { font-size: 13px; color: #666; }
 .card .m { font-size: 11px; color: #999; margin-top: 10px; }
 .err { margin-top: 20px; color: #b91c1c; font-size: 14px; display: none; }
 .wait { margin-top: 20px; color: #888; font-size: 14px; display: none; }
</style></head><body>
<h1>링크 미리보기 카드</h1>
<p class="sub">주소를 넣으면 제목·설명·썸네일을 긁어 카드로 만들어요.</p>
<form id="f">
  <input id="u" type="url" placeholder="https://example.com" required>
  <button id="b" type="submit">만들기</button>
</form>
<p class="wait" id="wait">가져오는 중… (바깥 사이트를 실제로 읽어 오느라 몇 초 걸립니다)</p>
<p class="err" id="err"></p>
<div class="card" id="card">
  <img id="ci" alt="">
  <div class="body">
    <div class="t" id="ct"></div>
    <div class="d" id="cd"></div>
    <div class="m" id="cm"></div>
  </div>
</div>
<script>
// 인그레스가 앞에 붙여 둔 경로. 이게 없으면 요청이 엉뚱한 곳으로 간다.
const BASE = {{ base_path|tojson }};
const $ = (id) => document.getElementById(id);
$('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('card').style.display = 'none'; $('err').style.display = 'none';
  $('wait').style.display = 'block'; $('b').disabled = true;
  const t0 = performance.now();
  try {
    // 1) 접수만 한다. 바로 돌아온다.
    const r = await fetch(BASE + '/api/cards', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: $('u').value }),
    });
    const a = await r.json();
    if (!a.ok) throw new Error(a.error || '접수에 실패했어요');
    const accepted = ((performance.now() - t0) / 1000).toFixed(2);
    $('wait').textContent = `접수됐어요 (${accepted}초). 결과를 기다리는 중…`;

    // 2) 끝났는지 물어본다 (폴링)
    //    재시도가 붙으면 8초 + 5초 + 8초 + 15초 + 8초 로 44초 가까이 걸릴 수 있다.
    //    그래서 90초까지 기다린다.
    let j = null;
    for (let i = 0; i < 180; i++) {
      await new Promise((s) => setTimeout(s, 500));
      const q = await fetch(BASE + '/api/cards/' + a.job_id);
      j = await q.json();
      if (j.status === 'retrying') {
        $('wait').textContent =
          `실패해서 다시 시도하는 중이에요 (${j.attempt}/${j.max_attempts} 실패, ${j.next_retry_in}초 뒤 재시도) — ${j.error || ''}`;
      }
      if (j.status === 'done' || j.status === 'failed') break;
    }
    if (!j || j.status !== 'done') {
      const tries = j && j.attempt ? ` (${j.attempt}번 시도)` : '';
      throw new Error((j?.error || '아직 끝나지 않았어요') + tries);
    }

    $('ci').src = j.card ? ('data:image/jpeg;base64,' + j.card) : '';
    $('ci').style.display = j.card ? 'block' : 'none';
    $('ct').textContent = j.title || '';
    $('cd').textContent = j.description || '';
    const st = j.steps || {};
    $('cm').textContent =
      `${j.site || ''} · 접수까지 ${accepted}초 · 처리 ${j.elapsed}초`
      + ` (페이지 ${st.page ?? '-'}s · 이미지 ${st.image_download ?? '-'}s · 합성 ${st.render ?? '-'}s)`
      + ` · 전체 ${j.total_wait}초`;
    $('card').style.display = 'block';
  } catch (e) {
    $('err').textContent = e.message; $('err').style.display = 'block';
  } finally {
    $('wait').style.display = 'none'; $('b').disabled = false;
    $('wait').textContent = '가져오는 중…';
  }
});
</script></body></html>"""


@app.get("/healthz")
def healthz():
    return jsonify(ok=True, service="web")


@app.get("/")
def index():
    return render_template_string(PAGE, base_path=BASE_PATH)


@app.post("/api/cards")
def create_card():
    """접수만 하고 바로 답한다. 실제 작업은 워커가 뒤에서 한다."""
    rid = _request_id()
    url = (request.json or {}).get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        log("job.rejected", request_id=rid, reason="bad_url", url=url[:200])
        return jsonify(ok=False, error="http(s) 로 시작하는 주소만 받습니다."), 400

    job_id = uuid.uuid4().hex[:12]
    started = time.time()
    try:
        _publish(job_id, url, rid)
    except Exception as e:
        log("job.publish_failed", request_id=rid, job_id=job_id, error=str(e)[:200])
        return jsonify(ok=False, error=f"큐에 넣지 못했습니다: {e}"), 503

    _save(job_id, {"status": "queued", "url": url, "queued_at": started, "request_id": rid})
    log("job.queued", request_id=rid, job_id=job_id, url=url,
        accept_ms=round((time.time() - started) * 1000, 1))
    # 202 Accepted — "받았고, 아직 안 끝났다"
    return jsonify(ok=True, job_id=job_id, request_id=rid, status="queued",
                   accept_elapsed=round(time.time() - started, 3)), 202


@app.get("/api/cards/<job_id>")
def get_card(job_id):
    r = _load(job_id)
    if r is None:
        return jsonify(ok=False, status="unknown", error="그런 작업이 없습니다."), 404
    # 저장된 결과에 이미 ok 가 들어 있다. ok=True 를 또 주면
    # "multiple values for keyword argument 'ok'" 로 터진다.
    return jsonify(r)


@app.post("/internal/result")
def put_result():
    """워커가 결과(또는 중간 상태)를 돌려주는 자리."""
    d = request.json or {}
    job_id = d.pop("job_id", "")
    rid_from_worker = d.pop("request_id", "")
    prev = _load(job_id) or {}
    rid = prev.get("request_id") or rid_from_worker or request.headers.get("X-Request-ID", "")
    waited = round(time.time() - prev.get("queued_at", time.time()), 2)
    # 워커가 status 를 직접 주면(retrying) 그걸 쓰고, 아니면 ok 로 판단한다.
    status = d.pop("status", None) or ("done" if d.get("ok") else "failed")
    # queued_at 을 그대로 들고 간다. 안 그러면 재시도 결과가 들어올 때마다
    # 접수 시각이 사라져 total_wait 이 0 으로 찍힌다.
    _save(job_id, {**d, "status": status, "total_wait": waited,
                   "queued_at": prev.get("queued_at"), "request_id": rid,
                   "url": prev.get("url", d.get("url"))})
    # 앞 편에서는 중간 보고에도 'job done' 이라 찍혀 헷갈렸다.
    # 상태를 사건 이름에 그대로 쓴다: result.retrying / result.done / result.failed
    log(f"result.{status}", request_id=rid, job_id=job_id, attempt=d.get("attempt"),
        total_wait=waited, error=(str(d["error"])[:120] if d.get("error") else None))
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
