"""fetcher — URL 하나를 받아 미리보기에 쓸 정보를 긁어 온다.

일부러 느린 일이다. 바깥 사이트를 실제로 받아 와야 하므로 1~5초가 걸리고,
상대 서버가 느리면 더 걸린다. 이 '느림'이 뒤에서 큐를 도입하는 이유가 된다.
"""
import os
import time
import urllib.request
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError

from flask import Flask, jsonify, request

app = Flask(__name__)
TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "8"))
UA = "Mozilla/5.0 (compatible; linkcard/1.0; +https://autops.run)"


class OpenGraphParser(HTMLParser):
    """<meta property="og:..."> 와 <title> 만 골라 담는다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
            return
        if tag != "meta":
            return
        a = dict(attrs)
        key = (a.get("property") or a.get("name") or "").lower()
        if key.startswith("og:") or key in ("description", "twitter:image"):
            self.meta[key] = a.get("content", "")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()


@app.get("/healthz")
def healthz():
    return jsonify(ok=True, service="fetcher")


@app.post("/fetch")
def fetch():
    url = (request.json or {}).get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify(ok=False, error="http(s) 로 시작하는 주소만 받습니다."), 400

    started = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            # 페이지 전체를 읽을 필요가 없다. <head> 만 있으면 된다.
            raw = r.read(200_000)
        charset = "utf-8"
        html = raw.decode(charset, errors="replace")
    except HTTPError as e:
        return jsonify(ok=False, error=f"HTTP {e.code}", elapsed=round(time.time() - started, 2)), 502
    except (URLError, TimeoutError) as e:
        return jsonify(ok=False, error=f"가져오지 못했습니다: {e}", elapsed=round(time.time() - started, 2)), 504

    p = OpenGraphParser()
    p.feed(html)
    elapsed = round(time.time() - started, 2)

    return jsonify(
        ok=True,
        url=url,
        title=p.meta.get("og:title") or p.title or url,
        description=p.meta.get("og:description") or p.meta.get("description") or "",
        image=p.meta.get("og:image") or p.meta.get("twitter:image") or "",
        site=p.meta.get("og:site_name") or "",
        elapsed=elapsed,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
