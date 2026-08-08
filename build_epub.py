#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
wenku8 插图 EPUB 生成器（纯 Python 单脚本）

功能：
  1. 读取本地已下载的 wenku8 txt（支持单文件或目录批量，aid 从文件名 \d+.txt 解析）
  2. 通过 app 中继 API 拉取目录/章节，把 txt 按「卷名 + 空格 + 章节名」切分对齐
  3. 识别「插图」章节（标题启发式或 --scan-all 全量扫描），拉正文提取 <!--image--> 图片 URL
  4. 模拟浏览器请求头（curl_cffi 指纹 -> requests -> cloudscraper 逐级降级）并发下载插图，磁盘缓存 + 断点续跑
  5. 生成 Sigil 合规的 EPUB3（nav + ncx + cover-image + 转义文本 + ASCII 安全路径），写盘前自校验

依赖：pip install -r requirements.txt
"""
import argparse
import base64
import html as html_mod
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
RELAY_URL = "https://wenku8-relay.mewx.org"        # 中继根路径（实测可用）
DIRECT_URL = "http://app.wenku8.com/android.php"   # 直连（可能已失效，仅兜底）
DALVIK_UA = "Dalvik/2.1.0 (Linux; U; Android 7.1.2; unknown Build/NZH54D)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

IMAGE_MARKER_RE = re.compile(r"<!--image-->(.*?)<!--image-->")
ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
IMAGE_CHAPTER_RE = re.compile(r"插图|插畫|img|写真", re.I)
BANNER_RE = re.compile(r"^[★☆◆◇]+")
FOOTER_RE = re.compile(r"Www\.WenKu8\.Com", re.I)
TITLE_LINE_RE = re.compile(r"^<(.+)>$")

ENGINES = ("curl_cffi", "requests", "cloudscraper")
IMG_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    "bmp": "image/bmp",
}
IMG_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
)

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
_lock_out = threading.Lock()


def log(msg, tag="INFO"):
    with _lock_out:
        try:
            sys.stdout.write(f"[{tag}] {msg}\n")
            sys.stdout.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 请求头 / 会话
# ---------------------------------------------------------------------------
def _browser_headers(referer):
    return {
        "User-Agent": BROWSER_UA,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": referer,
        "Connection": "keep-alive",
    }


class RateLimiter:
    """令牌桶限速，rate_per_min 为每分钟允许的请求数，burst 为突发容量。"""

    def __init__(self, rate_per_min, burst):
        self._rate = rate_per_min / 60.0
        self._cap = max(burst, 1)
        self._tokens = float(self._cap)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._cap, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            deficit = (1.0 - self._tokens) / self._rate
            self._tokens = 0.0
        time.sleep(deficit)
        with self._lock:
            self._last = time.monotonic()


def _make_session(engine):
    """按引擎创建独立的会话（每线程一个）。"""
    if engine == "curl_cffi":
        from curl_cffi import requests as cr
        return cr.Session(impersonate="chrome")
    if engine == "requests":
        import requests
        return requests.Session()
    if engine == "cloudscraper":
        import cloudscraper
        try:
            return cloudscraper.create_scraper()
        except Exception:
            import requests
            return requests.Session()
    raise ValueError(engine)


def _session_request(sess, method, url, **kw):
    return sess.request(method, url, **kw)


def _session_cookies(sess):
    try:
        return getattr(sess, "cookies", None)
    except Exception:
        return None


class HttpClient:
    """
    统一的 HTTP 客户端：引擎降级链、限速、退避重试、CF 挑战识别、Cookie 持久化。
    get/get_bytes/post 均为线程安全（每线程独立会话 + 全局锁保护共享状态）。
    """

    def __init__(self, engine="auto", workers=3, delay=0.0, req_per_min=60,
                 cookie_jar=None, referer="https://www.wenku8.cc/", retries=3):
        if engine == "auto":
            engine = "curl_cffi"
        self.engine_choice = engine
        self._engine_idx = ENGINES.index(engine) if engine in ENGINES else 0
        self.referer = referer
        self.delay = delay
        self.retries = retries
        self.workers = max(1, workers)
        self.limiter = RateLimiter(req_per_min, self.workers * 4)
        self.cookie_jar = cookie_jar
        self._cookies = {}
        self._cookies_lock = threading.Lock()
        self._local = threading.local()
        self._load_cookies()

    # ---- 会话管理 ----
    def _session(self):
        sess = getattr(self._local, "sess", None)
        if sess is None:
            sess = self._new_session(self._engine_idx)
            self._local.sess = sess
        return sess

    def _new_session(self, idx):
        engine = ENGINES[idx]
        try:
            sess = _make_session(engine)
        except Exception as e:
            log(f"创建 {engine} 会话失败({e})，降级到 requests", "WARN")
            sess = _make_session("requests")
        self._seed_cookies(sess)
        return sess

    # ---- Cookie 持久化 ----
    def _load_cookies(self):
        if not self.cookie_jar or not os.path.isfile(self.cookie_jar):
            return
        try:
            with open(self.cookie_jar, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._cookies = {c["name"]: c for c in data.get("cookies", [])}
        except Exception:
            self._cookies = {}

    def _seed_cookies(self, sess):
        cookies = _session_cookies(sess)
        if cookies is None:
            return
        for c in self._cookies.values():
            try:
                cookies.set(c["name"], c["value"], domain=c.get("domain", ""),
                            path=c.get("path", "/"))
            except Exception:
                pass

    def _harvest_cookies(self, sess, resp):
        cookies = _session_cookies(sess)
        if cookies is None:
            return
        try:
            jar = cookies if hasattr(cookies, "get_dict") else None
            if jar is None:
                return
            d = jar.get_dict() if hasattr(jar, "get_dict") else {}
            with self._cookies_lock:
                for name, value in d.items():
                    self._cookies[name] = {"name": name, "value": value,
                                           "domain": self.referer_host(), "path": "/"}
        except Exception:
            pass

    def referer_host(self):
        return urllib.parse.urlparse(self.referer).netloc or ""

    def save_cookies(self):
        if not self.cookie_jar:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.cookie_jar)), exist_ok=True)
            with open(self.cookie_jar, "w", encoding="utf-8") as f:
                json.dump({"cookies": list(self._cookies.values())}, f, ensure_ascii=False)
        except Exception as e:
            log(f"保存 cookie 失败: {e}", "WARN")

    # ---- 挑战识别 ----
    @staticmethod
    def _is_cf_challenge(status, content):
        if status in (403, 503, 429):
            head = content[:4096].decode("utf-8", "ignore")
            if re.search(r"cf-chl|challenge-platform|cf-mitigated|Just a moment|__cf_chl", head):
                return True
        return False

    # ---- 核心请求 ----
    def request(self, method, url, *, headers=None, data=None, timeout=35):
        last_err = None
        attempts = self.retries + 1
        for attempt in range(attempts):
            self.limiter.acquire()
            idx = self._engine_idx
            try:
                sess = self._new_session(idx) if attempt == 0 else self._new_session(idx)
                resp = _session_request(sess, method, url, headers=headers or {}, data=data, timeout=timeout)
                self._harvest_cookies(sess, resp)
                content = getattr(resp, "content", None) or b""
                status = int(getattr(resp, "status_code", 0))

                if self._is_cf_challenge(status, content) and idx < len(ENGINES) - 1:
                    log(f"检测到 Cloudflare 挑战({status})，切换到 {ENGINES[idx+1]} 重试", "WARN")
                    self._engine_idx = idx + 1
                    self._local.sess = None
                    time.sleep(1.0 + attempt)
                    continue

                if status < 400:
                    if self.delay > 0:
                        time.sleep(self.delay * (0.5 + random.random()))
                    return resp

                last_err = f"HTTP {status}"
                if status in (429, 503):
                    ra = None
                    try:
                        ra = resp.headers.get("Retry-After")
                    except Exception:
                        ra = None
                    wait = (int(ra) if ra and str(ra).isdigit() else 2 ** attempt)
                    log(f"{url} 触发限速(HTTP {status})，等待 {wait}s 后重试", "WARN")
                    time.sleep(min(wait, 60))
                    continue
                # 其余 4xx/5xx
                time.sleep(2 ** attempt)
            except Exception as e:
                last_err = str(e)
                log(f"{method} {url} 失败({e})，第{attempt+1}/{attempts}次重试", "WARN")
                time.sleep(min(2 ** attempt, 30))

        raise RuntimeError(f"请求失败 {method} {url}: {last_err}")

    def get_bytes(self, url, referer=None):
        hdrs = _browser_headers(referer or self.referer)
        resp = self.request("GET", url, headers=hdrs)
        return resp.content

    def post(self, url, data, headers):
        return self.request("POST", url, headers=headers, data=data)

    # ---- wenku8 app API ----
    def api_post(self, action, use_relay=True):
        body = ("&appver=1.13&request=" + base64.b64encode(action.encode()).decode()
                + "&timetoken=" + str(int(time.time() * 1000)))
        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "User-Agent": DALVIK_UA,
        }
        endpoints = [RELAY_URL] if use_relay else []
        if use_relay:
            endpoints.append(DIRECT_URL)
        last = None
        for url in endpoints:
            try:
                resp = self.post(url, body, headers)
                if resp.status_code == 200:
                    self.save_cookies()
                    return resp.content
                last = resp.status_code
            except Exception as e:
                last = e
                log(f"API 端点 {url} 失败: {e}", "WARN")
        raise RuntimeError(f"API 请求失败: {action} -> {last}")


# ---------------------------------------------------------------------------
# 文本 / txt 解析
# ---------------------------------------------------------------------------
def detect_encoding(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5hkscs", "big5"):
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"


def read_txt(path):
    enc = detect_encoding(path)
    with open(path, "r", encoding=enc, errors="replace") as f:
        text = f.read()
    return text, enc


def split_txt(text, volumes):
    """
    用 API 卷/章名把 txt 切分为 卷->章 文本树。
    返回 (chapters_text, preamble)，chapters_text 的键为 (v_idx, c_idx)。
    """
    header_map = {}
    volume_names = set()
    for vi, v in enumerate(volumes):
        volume_names.add(v["name"].strip())
        for ci, c in enumerate(v["chapters"]):
            hdr = f"{v['name'].strip()} {c['name'].strip()}".strip()
            header_map[hdr] = (vi, ci)

    chapters_text = {}
    current = None
    preamble = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line in volume_names:  # 裸卷名分隔行，跳过
            continue
        if BANNER_RE.match(line) or FOOTER_RE.search(line):
            continue
        if line in header_map:
            current = header_map[line]
            chapters_text.setdefault(current, [])
            continue
        if current is None:
            preamble.append(line)
        else:
            chapters_text[current].append(line)
    return chapters_text, preamble


def _to_text(data):
    if isinstance(data, bytes):
        return data.decode("utf-8", "ignore")
    return data


def parse_toc(xml_text):
    xml_text = ILLEGAL_XML_RE.sub("", _to_text(xml_text))
    root = ET.fromstring(xml_text)
    volumes = []
    for v in root.iter("volume"):
        vid = v.get("vid") or ""
        name = (v.text or "").strip()
        chapters = []
        for c in v.iter("chapter"):
            chapters.append({"cid": c.get("cid") or "", "name": (c.text or "").strip()})
        if vid or name:
            volumes.append({"vid": vid, "name": name, "chapters": chapters})
    return volumes


def extract_image_urls(content):
    return [u.strip() for u in IMAGE_MARKER_RE.findall(content)]


def fetch_book_info(client, aid):
    """尝试从 API 获取书名/作者；失败返回 (None, None)。"""
    try:
        raw = client.api_post(f"action=book&do=bookinfo&aid={aid}&t=0")
        root = ET.fromstring(ILLEGAL_XML_RE.sub("", _to_text(raw)))

        title, author = None, None
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag != "data":
                continue
            name = el.get("name") or ""
            if name == "Title":
                title = (el.text or "").strip() or None
            elif name == "Author":
                author = (el.get("value") or "").strip() or None
        return title, author
    except Exception as e:
        log(f"bookinfo 获取失败: {e}", "WARN")
        return None, None


# 页面元数据抓取的常量（书籍页面 https://www.wenku8.cc/book/<aid>.htm）
PAGE_BOOK_URL = "https://www.wenku8.cc/book/{aid}.htm"
_PAGE_AUTHOR_RE = re.compile(r"\u5c0f\u8bf4\u4f5c\u8005\uff1a(.*?)</td>", re.S)          # 小说作者：
_PAGE_DESC_SPLIT = "\u5185\u5bb9\u7b80\u4ecb\uff1a</span>"                              # 内容简介：</span>
_PAGE_COLLECTION_RE = re.compile(r"\u6587\u5e93\u5206\u7c7b\uff1a(.*?)</td>", re.S)      # 文库分类：
_BR_RE = re.compile(r"<br\s*/?>", re.I)


def _strip_html(s):
    return re.sub(r"<[^>]+>", "", s).strip()


def _extract_page_desc(text):
    """提取内容简介：保留 <br> 换行为段落换行；无法提取返回 None。"""
    seg = text.split(_PAGE_DESC_SPLIT, 1)
    if len(seg) != 2:
        return None
    m = re.search(r"<span[^>]*>(.*?)</span>", seg[1], re.S)
    html = m.group(1) if m else seg[1]
    html = _BR_RE.sub("\n", html)
    txt = _strip_html(html)
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in txt.split("\n")]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines) or None


def fetch_page_meta(client, aid):
    """
    抓取书籍网页上的 作者 / 内容简介 / 文库分类。
    返回 (author, description, collection)，各自失败时为 None（不中断流程）。
    """
    try:
        raw = client.get_bytes(PAGE_BOOK_URL.format(aid=aid))
    except Exception as e:
        log(f"书籍页面获取失败(book/{aid}.htm): {e}", "WARN")
        return None, None, None
    text = None
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = raw.decode("utf-8", "replace")

    author = collection = description = None
    m = _PAGE_AUTHOR_RE.search(text)
    if m:
        author = _strip_html(m.group(1)) or None
    m = _PAGE_COLLECTION_RE.search(text)
    if m:
        collection = _strip_html(m.group(1)) or None
    description = _extract_page_desc(text)
    return author, description, collection


# ---------------------------------------------------------------------------
# 图片下载
# ---------------------------------------------------------------------------
def safe_name(s, max_len=60):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len] if s else "unnamed"


def ext_of(url):
    path = urllib.parse.urlparse(url).path
    base = os.path.basename(path)
    if "." in base:
        ext = base.rsplit(".", 1)[1].lower()
        if ext in IMG_MIME:
            return ext
    return "jpg"


def validate_image(data, url):
    for magic, mime in IMG_MAGIC:
        if data[: len(magic)] == magic:
            return mime
    log(f"图片校验失败(非 JPEG/PNG/GIF/WEBP): {url[:80]}", "WARN")
    return None


def download_images(client, aid, targets, cache_dir, resume=True, workers=3):
    """
    targets: {cid: [url, ...]} -> {cid: [{"url","file","data"}]}
    data 在 resume 且缓存命中时为 None（后续从缓存文件读取）。
    """
    base = os.path.join(cache_dir, f"{aid}")
    os.makedirs(base, exist_ok=True)
    result = {}
    jobs = []
    for cid, urls in targets.items():
        cdir = os.path.join(base, str(cid))
        os.makedirs(cdir, exist_ok=True)
        result.setdefault(cid, [])
        for i, url in enumerate(urls, 1):
            ext = ext_of(url)
            fname = f"{i:03d}.{ext}"
            fpath = os.path.join(cdir, fname)
            if resume and os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
                result[cid].append({"url": url, "file": os.path.relpath(fpath, base), "data": None})
                continue
            jobs.append((cid, url, fname, fpath))

    total = len(jobs)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {ex.submit(client.get_bytes, url): (cid, url, fname, fpath) for cid, url, fname, fpath in jobs}
        for fut in as_completed(futures):
            cid, url, fname, fpath = futures[fut]
            done += 1
            try:
                data = fut.result()
                mime = validate_image(data, url)
                if not mime:
                    continue
                with open(fpath, "wb") as f:
                    f.write(data)
                result[cid].append({"url": url, "file": os.path.relpath(fpath, base), "data": data})
            except Exception as e:
                log(f"图片下载失败 {url[:80]}: {e}", "ERROR")
            log(f"图片进度 {done}/{total}", "INFO")
    return result


# ---------------------------------------------------------------------------
# EPUB 生成
# ---------------------------------------------------------------------------
def esc(text):
    return html_mod.escape(str(text), quote=False)


def build_epub(aid, title, author, volumes, chapters_text, image_data, cover_data, out_path,
               description=None, collection=None):
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier(str(aid))
    book.set_title(title or f"novel_{aid}")
    book.set_language("zh-Hans")
    if author:
        try:
            book.add_author(author)
        except Exception:
            pass
    if description:
        try:
            book.add_metadata("DC", "description", description)
        except Exception:
            pass
    if collection:
        try:
            book.add_metadata(None, "meta", collection, {"property": "belongs-to-collection"})
        except Exception:
            pass

    if cover_data:
        book.set_cover("Images/cover.jpg", cover_data, create_page=True)
        # 封面页改为线性项：这样无需要 landmarks 链接也能通过 epubcheck OPF-096，
        # 从而可以在目录页移除 "Guide"(landmarks) 区
        cover_page = book.get_item_with_id("cover")
        if cover_page is not None:
            cover_page.is_linear = True

    # 样式表：卷/章标题居中、正文首行缩进 2 字符、插图段落取消缩进
    css_text = (
        "h2, h3 { text-align: center; }\n"
        "p { text-indent: 2em; }\n"
        "p.image { text-indent: 0; text-align: center; }\n"
    )
    css = epub.EpubItem(uid="style", file_name="style.css", media_type="text/css",
                        content=css_text.encode("utf-8"))
    book.add_item(css)

    toc_entries = []
    spine = ["nav"] + (["cover"] if cover_data else [])
    ids = []
    for vi, v in enumerate(volumes):
        file_name = f"vol_{vi}.xhtml"
        uid = f"vol-{vi}"
        parts = [f"<h2>{esc(v['name'])}</h2>"]
        links = []
        for ci, c in enumerate(v["chapters"]):
            anchor = f"ch-{vi}-{ci}"
            parts.append(f'<h3 id="{anchor}">{esc(c["name"])}</h3>')
            for line in chapters_text.get((vi, ci), []):
                parts.append(f"<p>{esc(line)}</p>")
            for item in image_data.get(c["cid"], []):
                if not item.get("data"):
                    continue
                fname = item["file"].replace("\\", "/")
                parts.append(f'<p class="image"><img src="Images/{fname}" alt=""/></p>')
            links.append(epub.Link(f"{file_name}#{anchor}", c["name"], f"l-{vi}-{ci}"))
        if not links:
            links.append(epub.Link(file_name, v["name"], f"lv-{vi}"))
        chapter = epub.EpubHtml(title=v["name"], file_name=file_name, lang="zh-Hans", uid=uid)
        chapter.content = "".join(parts)
        chapter.add_link(href="style.css", rel="stylesheet", type="text/css")
        book.add_item(chapter)
        # ebooklib 的 toc 需用嵌套 tuple：(父节点, 子节点们)
        toc_entries.append((epub.Section(v["name"], file_name), tuple(links)))
        ids.append(uid)

    # 插图资源
    for cid, items in image_data.items():
        for i, item in enumerate(items):
            if not item.get("data"):
                continue
            fname = item["file"].replace("\\", "/")
            data = item["data"]
            img = epub.EpubImage(uid=f"img-{cid}-{i}", file_name=f"Images/{fname}",
                                 media_type=validate_image_mime(fname), content=data)
            book.add_item(img)

    book.toc = tuple(toc_entries)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine + ids

    epub.write_epub(out_path, book, {})
    return out_path


def validate_image_mime(fname):
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    return IMG_MIME.get(ext, "image/jpeg")


# ---------------------------------------------------------------------------
# 自校验（写盘后）
# ---------------------------------------------------------------------------
def validate_epub(path):
    """解压并检查 EPUB 结构，模拟 Sigil 常见报错点。返回问题列表（空=通过）。"""
    problems = []
    try:
        zf = zipfile.ZipFile(path)
    except Exception as e:
        return [f"无法打开 EPUB: {e}"]

    names = zf.namelist()
    if not names or names[0] != "mimetype":
        problems.append("mimetype 不是 zip 首项")
    try:
        if zf.read("mimetype") != b"application/epub+zip":
            problems.append("mimetype 内容错误")
    except KeyError:
        problems.append("缺少 mimetype")

    # 容器
    opf_rel = None
    try:
        container = zf.read("META-INF/container.xml")
        croot = ET.fromstring(container)
        for rf in croot.iter():
            if rf.tag.endswith("rootfile") and rf.get("media-type") == "application/oebps-package+xml":
                opf_rel = rf.get("full-path")
                break
        if not opf_rel:
            problems.append("container.xml 未找到 content.opf")
    except Exception as e:
        problems.append(f"container.xml 解析失败: {e}")

    if not opf_rel:
        return problems
    opf_dir = os.path.dirname(opf_rel).replace("\\", "/")
    try:
        opf = ET.fromstring(zf.read(opf_rel))
    except Exception as e:
        return problems + [f"content.opf 解析失败: {e}"]

    # manifest
    manifest_hrefs = []
    for it in opf.iter():
        if it.tag.endswith("item"):
            href = it.get("href")
            mid = it.get("id")
            if href:
                manifest_hrefs.append(href)
            if not mid:
                problems.append("manifest 存在缺少 id 的 item")
    for href in manifest_hrefs:
        resolved = urllib.parse.urljoin(opf_dir + "/", href)
        if resolved not in names:
            problems.append(f"manifest href 不存在: {href}")

    # spine
    spine_ids = set()
    for it in opf.iter():
        if it.tag.endswith("itemref"):
            spine_ids.add(it.get("idref"))
    for sid in spine_ids:
        found = any(it.get("id") == sid for it in opf.iter() if it.tag.endswith("item"))
        if not found:
            problems.append(f"spine idref 无对应 manifest: {sid}")

    # 逐文件 well-formed + 引用检查
    for name in names:
        if name.endswith((".opf", ".xml", ".xhtml", ".ncx")):
            try:
                ET.fromstring(zf.read(name))
            except Exception as e:
                problems.append(f"XML 不合法 {name}: {e}")
        if name.endswith(".xhtml"):
            try:
                content = zf.read(name).decode("utf-8")
                for m in re.finditer(r'<img[^>]+src="([^"]+)"', content):
                    src = m.group(1).replace("../", "")
                    base = os.path.dirname(name).replace("\\", "/")
                    resolved = urllib.parse.urljoin(base + "/", src)
                    if resolved not in names:
                        problems.append(f"{name} 引用不存在的图片: {src}")
            except Exception as e:
                problems.append(f"xhtml 读取失败 {name}: {e}")

    # 图片魔数
    media = {}
    for it in opf.iter():
        if it.tag.endswith("item") and it.get("media-type", "").startswith("image/"):
            media[it.get("id")] = (it.get("href"), it.get("media-type"))
    for mid, (href, mtype) in media.items():
        resolved = urllib.parse.urljoin(opf_dir + "/", href)
        if resolved not in names:
            continue
        data = zf.read(resolved)
        ok = any(data.startswith(m) for m, _ in IMG_MAGIC)
        if not ok:
            problems.append(f"图片内容非有效图像: {href}")

    # nav / ncx 内部锚点存在性（Sigil 会对断裂的内部链接报警）
    def _check_links(link_file, attr, names, zf):
        try:
            content = zf.read(link_file).decode("utf-8")
        except Exception:
            return
        ids = set()
        for m in re.finditer(r'<[^>]+\bid="([^"]+)"', content):
            ids.add(m.group(1))
        for m in re.finditer(attr + r'="([^"]+)"', content):
            href = m.group(1)
            if not href or href.startswith(("http://", "https://", "mailto:")):
                continue
            path_part, _, frag = href.partition("#")
            if not path_part:
                continue
            base = os.path.dirname(link_file).replace("\\", "/")
            resolved = urllib.parse.urljoin(base + "/", path_part)
            if resolved not in names:
                problems.append(f"{link_file} 链接目标不存在: {href}")
                continue
            if frag and resolved.endswith(".xhtml"):
                try:
                    target = zf.read(resolved).decode("utf-8")
                    if f'id="{frag}"' not in target and f'name="{frag}"' not in target:
                        problems.append(f"{link_file} 锚点不存在: {href}")
                except Exception:
                    pass

    for nav_file in (opf_dir + "/nav.xhtml", opf_dir + "/toc.ncx"):
        if nav_file in names:
            _check_links(nav_file, "href" if nav_file.endswith(".xhtml") else "src", names, zf)
    zf.close()
    return problems


# ---------------------------------------------------------------------------
# 单本书主流程
# ---------------------------------------------------------------------------
def process_book(txt_path, args, client):
    aid = args.aid
    if not aid:
        m = re.search(r"(\d+)\.txt$", os.path.basename(txt_path), re.I)
        aid = m.group(1) if m else None
    if not aid:
        log(f"无法从 {txt_path} 文件名识别 aid，跳过（可用 --aid 指定）", "ERROR")
        return

    log(f"==== 处理 {txt_path} (aid={aid}) ====", "INFO")
    text, enc = read_txt(txt_path)
    log(f"编码: {enc}，字符数: {len(text)}", "INFO")

    volumes = parse_toc(client.api_post(f"action=book&do=list&aid={aid}&t=0"))
    if not volumes:
        log("目录解析为空，跳过", "ERROR")
        return
    log(f"目录: {len(volumes)} 卷", "INFO")

    chapters_text, preamble = split_txt(text, volumes)

    api_title, api_author = fetch_book_info(client, aid)
    page_author, description, collection = fetch_page_meta(client, aid)
    author = page_author or api_author
    title = api_title
    if not title and preamble:
        for line in preamble:
            m = TITLE_LINE_RE.match(line)
            if m:
                title = m.group(1).strip()
                break
    if not title:
        title = os.path.splitext(os.path.basename(txt_path))[0]
    if author:
        log(f"作者: {author}", "INFO")
    if collection:
        log(f"文库分类: {collection}", "INFO")
    if description:
        log(f"内容简介: {description.splitlines()[0][:40]}…（{len(description)} 字符）", "INFO")

    # 目标章节
    target_cids = {}
    scan_all = args.scan_all
    for vi, v in enumerate(volumes):
        for c in v["chapters"]:
            if scan_all or IMAGE_CHAPTER_RE.search(c["name"]):
                target_cids[c["cid"]] = (vi, c["name"])
    log(f"插图章节: {[(cid, name) for cid, (vi, name) in target_cids.items()]}", "INFO")

    # 拉插图章正文，提取 <!--image--> 图片 URL
    targets = {}
    for cid in target_cids:
        try:
            content = client.api_post(f"action=book&do=text&aid={aid}&cid={cid}&t=0")
            urls = extract_image_urls(content.decode("utf-8", "ignore"))
            if urls:
                targets[cid] = urls
        except Exception as e:
            log(f"章节 {cid} 内容获取失败: {e}", "ERROR")
    total_imgs = sum(len(v) for v in targets.values())
    log(f"共发现 {total_imgs} 张插图", "INFO")

    # 下载
    cache_dir = args.cache or os.path.join(os.path.dirname(os.path.abspath(txt_path)), f"{aid}_images")
    image_data = {}
    if total_imgs:
        downloaded = download_images(client, aid, targets, cache_dir, resume=not args.no_resume,
                                     workers=args.workers)
        # 并发下载的完成顺序不等于 URL 顺序，按文件名（URL 序号）排序
        for cid in downloaded:
            downloaded[cid].sort(key=lambda it: it["file"])
        # 读取缓存补齐 data（下载时 data 已带，续跑时从文件读）
        for cid, items in downloaded.items():
            filled = []
            for it in items:
                if it["data"] is None:
                    full = os.path.join(cache_dir, str(aid), it["file"])
                    try:
                        with open(full, "rb") as f:
                            it["data"] = f.read()
                    except Exception:
                        continue
                filled.append(it)
            image_data[cid] = filled

    # 封面
    cover_data = None
    if not args.no_cover:
        if args.cover == "api":
            pass  # 走 API 缩略图
        else:
            for cid, items in image_data.items():
                if items and items[0]["data"]:
                    cover_data = items[0]["data"]
                    break
        if cover_data is None:
            try:
                cover_data = client.api_post(f"action=book&do=cover&aid={aid}")
            except Exception as e:
                log(f"封面(API)获取失败: {e}", "WARN")
            if cover_data is not None:
                if not validate_image(cover_data, "cover"):
                    cover_data = None
    if cover_data:
        log(f"封面: {'第一卷插图章首图' if (args.cover != 'api') else 'API 缩略图'} ({len(cover_data)} 字节)", "INFO")
    else:
        log("未设置封面", "WARN")

    out_dir = args.out or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)
    safe_title = safe_name(title or f"novel_{aid}")
    out_path = os.path.join(out_dir, f"{safe_title} - {aid}.epub")
    build_epub(aid, title, author, volumes, chapters_text, image_data, cover_data, out_path,
               description=description, collection=collection)
    log(f"已生成: {out_path}", "INFO")

    problems = validate_epub(out_path)
    if problems:
        log("自校验发现问题：", "ERROR")
        for p in problems:
            log(f"  - {p}", "ERROR")
        log(f"EPUB 存在 {len(problems)} 个问题，请检查（建议在 Sigil 中打开确认）", "ERROR")
    else:
        log("自校验通过：结构完整、XML 合法、引用与图片全部有效", "INFO")

    if args.epubcheck:
        _run_epubcheck(args.epubcheck, out_path)
    return out_path, problems


def _run_epubcheck(jar_path, epub_path):
    """可选：用官方 epubcheck 校验（需要 Java 与 epubcheck.jar）。"""
    import subprocess
    if not os.path.isfile(jar_path):
        log(f"epubcheck jar 不存在: {jar_path}", "ERROR")
        return
    log("调用 epubcheck 校验...", "INFO")
    try:
        r = subprocess.run(["java", "-jar", jar_path, epub_path],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.splitlines():
            line = line.strip()
            if line:
                log(line, "EPUBCHECK")
    except FileNotFoundError:
        log("未找到 java，无法运行 epubcheck", "ERROR")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="wenku8 本地 txt -> 插图 EPUB 生成器")
    ap.add_argument("--txt", required=True, help="txt 文件或目录（目录则批量处理其中 *.txt）")
    ap.add_argument("--out", help="EPUB 输出目录（默认当前目录）")
    ap.add_argument("--aid", help="强制指定 aid（默认从文件名 \\d+.txt 解析）")
    ap.add_argument("--engine", choices=["auto", "curl_cffi", "requests", "cloudscraper"], default="auto")
    ap.add_argument("--workers", type=int, default=3, help="图片下载并发数")
    ap.add_argument("--delay", type=float, default=0.0, help="请求间附加延时(秒)")
    ap.add_argument("--req-per-min", type=int, default=60, help="限速：每分钟请求上限")
    ap.add_argument("--cache", help="图片缓存目录（默认 txt 旁 <aid>_images）")
    ap.add_argument("--cookie-jar", help="Cookie 持久化文件")
    ap.add_argument("--no-resume", dest="no_resume", action="store_true", help="关闭断点续跑（默认开启）")
    ap.add_argument("--scan-all", action="store_true", help="扫描所有章节内容提取图片（不只插图章）")
    ap.add_argument("--cover", choices=["auto", "api"], default="auto",
                    help="auto=第一卷插图章首图(无则 API 缩略图)；api=始终 API 缩略图")
    ap.add_argument("--no-cover", action="store_true", help="不设置封面")
    ap.add_argument("--epubcheck", help="epubcheck.jar 路径，若提供则生成后用官方 epubcheck 校验")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    client = HttpClient(engine=args.engine, workers=args.workers, delay=args.delay,
                        req_per_min=args.req_per_min, cookie_jar=args.cookie_jar)
    try:
        if os.path.isdir(args.txt):
            files = sorted(f for f in os.listdir(args.txt)
                           if re.search(r"\d+\.txt$", f, re.I))
            if not files:
                log(f"目录 {args.txt} 下未找到 <aid>.txt 文件", "ERROR")
                return
            for f in files:
                process_book(os.path.join(args.txt, f), args, client)
        else:
            process_book(args.txt, args, client)
    finally:
        client.save_cookies()


if __name__ == "__main__":
    main()
