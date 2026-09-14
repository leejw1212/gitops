"""fetcher — URL 을 받아 미리보기 카드 '이미지'까지 만들어 돌려준다.

하는 일이 셋이다.
  1) 페이지를 받아 og 태그를 읽는다          (빠름, 0.1초 안팎)
  2) og:image 를 내려받는다                  (느림, 이미지가 크면 몇 초)
  3) 카드 이미지를 합성한다                  (느림, 리사이즈 + 글자 렌더링)

2번과 3번 때문에 한 건 처리에 몇 초가 걸린다. 이 '무거움'이 뒤에서
큐를 도입하는 이유가 된다.
"""
import base64
import io
import os
import time
import urllib.request
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse

from PIL import Image, ImageDraw, ImageFont


TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "8"))
UA = "Mozilla/5.0 (compatible; linkcard/1.0; +https://autops.run)"
CARD_W, CARD_H = 1200, 630          # 오픈그래프 권장 비율 (1.91:1)

# 한글이 깨지지 않게 나눔고딕을 쓴다. 없으면 기본 폰트로 떨어진다.
FONT_PATHS = [
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font(size):
    for p in FONT_PATHS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


class OpenGraphParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.title, self._in_title = {}, "", False

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


def _get(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read(limit)


def _wrap(draw, text, font, max_w, max_lines):
    """글자를 폭에 맞춰 줄바꿈한다. Pillow 에는 이런 기능이 없어 직접 한다.

    띄어쓰기가 있으면 단어 단위로 끊는다. 글자 단위로만 끊으면 영어 제목이
    'Orchestrati / on' 처럼 흉하게 갈라진다. 한국어처럼 띄어쓰기가 드물거나
    한 단어가 너무 길면 그때만 글자 단위로 떨어뜨린다.
    """
    def fits(t):
        return draw.textlength(t, font=font) <= max_w

    lines, cur = [], ""
    tokens = text.split(" ")
    for i, word in enumerate(tokens):
        cand = word if not cur else cur + " " + word
        if fits(cand):
            cur = cand
            continue
        if cur:
            lines.append(cur)
            cur = ""
            if len(lines) >= max_lines:
                break
        # 한 단어가 통째로 안 들어가면 글자 단위로 자른다
        piece = ""
        for ch in word:
            if fits(piece + ch):
                piece += ch
            else:
                lines.append(piece)
                piece = ch
                if len(lines) >= max_lines:
                    break
        if len(lines) >= max_lines:
            break
        cur = piece
    if cur and len(lines) < max_lines:
        lines.append(cur)

    lines = lines[:max_lines]
    # 다 못 담았으면 마지막 줄 끝에 말줄임표를 붙인다
    shown = " ".join(lines).replace(" ", "")
    if lines and len(shown) < len(text.replace(" ", "")):
        last = lines[-1]
        while last and not fits(last + "…"):
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def _render_card(title, site, img_bytes):
    """카드 이미지를 합성한다. 여기가 CPU 를 쓰는 대목이다."""
    card = Image.new("RGB", (CARD_W, CARD_H), (22, 24, 32))

    if img_bytes:
        try:
            src = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            # 카드를 꽉 채우도록 잘라 넣는다 (cover)
            ratio = max(CARD_W / src.width, CARD_H / src.height)
            new = (max(1, int(src.width * ratio)), max(1, int(src.height * ratio)))
            src = src.resize(new, Image.LANCZOS)
            left, top = (new[0] - CARD_W) // 2, (new[1] - CARD_H) // 2
            card.paste(src.crop((left, top, left + CARD_W, top + CARD_H)), (0, 0))
            # 글자가 읽히도록 아래쪽을 어둡게 덮는다
            shade = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
            sd = ImageDraw.Draw(shade)
            for i in range(CARD_H // 2, CARD_H):
                a = int(235 * (i - CARD_H // 2) / (CARD_H / 2))
                sd.line([(0, i), (CARD_W, i)], fill=(8, 10, 16, a))
            card = Image.alpha_composite(card.convert("RGBA"), shade).convert("RGB")
        except Exception:
            pass  # 이미지가 깨졌으면 배경색만 쓴다

    d = ImageDraw.Draw(card)
    tf, sf = _font(58), _font(30)
    for i, line in enumerate(_wrap(d, title or "", tf, CARD_W - 120, 3)):
        d.text((60, CARD_H - 250 + i * 72), line, font=tf, fill=(255, 255, 255))
    if site:
        d.text((60, CARD_H - 70), site, font=sf, fill=(150, 160, 180))

    out = io.BytesIO()
    card.save(out, format="JPEG", quality=82, optimize=True)
    return out.getvalue()


def build_card(url):
    """URL 하나를 받아 카드까지 만든다. 실패하면 ok=False 로 돌려준다."""
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "http(s) 로 시작하는 주소만 받습니다."}

    t0 = time.time(); steps = {}
    try:
        html = _get(url, 200_000).decode("utf-8", errors="replace")
    except HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}", "elapsed": round(time.time()-t0, 2)}
    except (URLError, TimeoutError) as e:
        return {"ok": False, "error": f"가져오지 못했습니다: {e}", "elapsed": round(time.time()-t0, 2)}
    steps["page"] = round(time.time() - t0, 2)

    p = OpenGraphParser(); p.feed(html)
    title = p.meta.get("og:title") or p.title or url
    site = p.meta.get("og:site_name") or urlparse(url).hostname or ""
    img_url = p.meta.get("og:image") or p.meta.get("twitter:image") or ""
    if img_url:
        img_url = urljoin(url, img_url)

    t1 = time.time(); img_bytes = b""
    if img_url:
        try:
            img_bytes = _get(img_url, 6_000_000)
        except Exception:
            img_bytes = b""
    steps["image_download"] = round(time.time() - t1, 2)

    t2 = time.time()
    card_jpg = _render_card(title, site, img_bytes)
    steps["render"] = round(time.time() - t2, 2)

    return {
        "ok": True, "url": url, "title": title, "site": site,
        "description": p.meta.get("og:description") or p.meta.get("description") or "",
        "source_image": img_url,
        "card": base64.b64encode(card_jpg).decode(),
        "card_bytes": len(card_jpg),
        "elapsed": round(time.time() - t0, 2), "steps": steps,
    }
