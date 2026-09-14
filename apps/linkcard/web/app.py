"""web — 사용자가 URL 을 넣는 화면과 API.

지금은 fetcher 를 '동기로' 부른다. 즉 fetcher 가 바깥 사이트를 다 긁어올
때까지 이 요청이 붙잡혀 있다. 사용자는 그동안 빈 화면을 본다.
이 불편함을 직접 겪은 다음에 큐를 넣는다.
"""
import os
import time
import urllib.request
import json as _json
from urllib.error import HTTPError, URLError

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
    const r = await fetch(BASE + '/api/cards', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: $('u').value }),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '실패했어요');
    // fetcher 가 합성해 준 카드 이미지를 그대로 띄운다 (base64)
    $('ci').src = j.card ? ('data:image/jpeg;base64,' + j.card) : '';
    $('ci').style.display = j.card ? 'block' : 'none';
    $('ct').textContent = j.title || '';
    $('cd').textContent = j.description || '';
    const st = j.steps || {};
    $('cm').textContent =
      `${j.site || new URL(j.url).hostname} · 총 ${j.elapsed}초`
      + ` (페이지 ${st.page ?? '-'}s · 이미지 ${st.image_download ?? '-'}s · 합성 ${st.render ?? '-'}s)`
      + ` · 화면에서 기다린 시간 ${((performance.now()-t0)/1000).toFixed(1)}초`;
    $('card').style.display = 'block';
  } catch (e) {
    $('err').textContent = e.message; $('err').style.display = 'block';
  } finally {
    $('wait').style.display = 'none'; $('b').disabled = false;
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
    url = (request.json or {}).get("url", "").strip()
    started = time.time()

    # ── 동기 호출. fetcher 가 끝날 때까지 여기서 멈춰 있다. ──
    body = _json.dumps({"url": url}).encode()
    req = urllib.request.Request(
        f"{FETCHER_URL}/fetch", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = _json.loads(r.read().decode())
    except HTTPError as e:
        try:
            data = _json.loads(e.read().decode())
        except Exception:
            data = {"ok": False, "error": f"fetcher 오류 HTTP {e.code}"}
        return jsonify(data), e.code
    except (URLError, TimeoutError) as e:
        return jsonify(ok=False, error=f"fetcher 에 닿지 못했습니다: {e}"), 504

    data["total_elapsed"] = round(time.time() - started, 2)
    app.logger.info("card created url=%s elapsed=%s", url, data["total_elapsed"])
    return jsonify(data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
