"""web — 사용자가 URL 을 넣는 화면과 API.

지금은 fetcher 를 '동기로' 부른다. 즉 fetcher 가 바깥 사이트를 다 긁어올
때까지 이 요청이 붙잡혀 있다. 사용자는 그동안 빈 화면을 본다.
이 불편함을 직접 겪은 다음에 큐를 넣는다.
"""
import os
import threading
import time
import urllib.request
import uuid
import json as _json
from urllib.error import HTTPError, URLError

import pika
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# 클러스터 안에서는 서비스 이름으로 서로를 부른다.
# linkcard-fetcher 는 같은 네임스페이스의 Service 이름이다.
FETCHER_URL = os.environ.get("FETCHER_URL", "http://linkcard-fetcher:8000")
TIMEOUT = float(os.environ.get("FETCHER_TIMEOUT", "15"))

# 인그레스가 /linkcard 를 떼고 넘겨 주기 때문에, 앱은 자기가 하위 경로에
# 있다는 걸 모른다. 그대로 두면 화면의 JS 가 /api/cards 를 절대 경로로
# 불러서 엉뚱한 서비스로 간다. 그래서 접두어를 밖에서 알려 준다.
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")

RABBIT_URL = os.environ.get(
    "RABBIT_URL", "amqp://linkcard:linkcard-dev@rabbitmq:5672/%2F")
QUEUE = os.environ.get("QUEUE", "card.jobs")

# 결과를 잠깐 들고 있는 곳. 지금은 파드 메모리라 재시작하면 사라진다.
# 파드가 여럿이면 접수한 파드와 조회하는 파드가 달라 못 찾을 수도 있다.
# 뒤 편에서 제대로 된 저장소로 옮긴다 — 지금은 큐 자체에 집중한다.
_results = {}
_lock = threading.Lock()


def _publish(job_id, url):
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
                content_type="application/json"),
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
    let j = null;
    for (let i = 0; i < 60; i++) {
      await new Promise((s) => setTimeout(s, 500));
      const q = await fetch(BASE + '/api/cards/' + a.job_id);
      j = await q.json();
      if (j.status === 'done' || j.status === 'failed') break;
    }
    if (!j || j.status !== 'done') throw new Error(j?.error || '아직 끝나지 않았어요');

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
    url = (request.json or {}).get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify(ok=False, error="http(s) 로 시작하는 주소만 받습니다."), 400

    job_id = uuid.uuid4().hex[:12]
    started = time.time()
    try:
        _publish(job_id, url)
    except Exception as e:
        return jsonify(ok=False, error=f"큐에 넣지 못했습니다: {e}"), 503

    with _lock:
        _results[job_id] = {"status": "queued", "url": url, "queued_at": started}

    app.logger.info("job queued id=%s url=%s in=%.3fs",
                    job_id, url, time.time() - started)
    # 202 Accepted — "받았고, 아직 안 끝났다"
    return jsonify(ok=True, job_id=job_id, status="queued",
                   accept_elapsed=round(time.time() - started, 3)), 202


@app.get("/api/cards/<job_id>")
def get_card(job_id):
    with _lock:
        r = _results.get(job_id)
    if r is None:
        return jsonify(ok=False, status="unknown",
                       error="그런 작업이 없습니다. (파드가 여럿이라 다른 파드가 받았을 수 있어요)"), 404
    return jsonify(ok=True, **r)


@app.post("/internal/result")
def put_result():
    """워커가 끝난 결과를 돌려주는 자리."""
    d = request.json or {}
    job_id = d.pop("job_id", "")
    with _lock:
        prev = _results.get(job_id, {})
        waited = round(time.time() - prev.get("queued_at", time.time()), 2)
        _results[job_id] = {"status": "done" if d.get("ok") else "failed",
                            "total_wait": waited, **d}
    app.logger.info("job done id=%s ok=%s wait=%.2fs", job_id, d.get("ok"), waited)
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
