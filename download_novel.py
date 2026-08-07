#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
wenku8 轻小说 txt 批量下载器（纯 Python 单脚本）

下载链接规则：
  第 1    本 -> https://dl2.wenku8.com/txtutf8/0/1.txt     (1     // 1000 = 0)
  第 2676 本 -> https://dl2.wenku8.com/txtutf8/2/2676.txt  (2676  // 1000 = 2)
  通用：  https://dl2.wenku8.com/txtutf8/{id // 1000}/{id}.txt

功能：
  1. 模拟浏览器请求头（Chrome UA + Referer）
  2. 输出到脚本同级目录下的 txt\ 文件夹（可用 config.ini 调整）
  3. 多线程下载，默认 8 线程 + 全局限速（默认 40 请求/分钟），均可在 config.ini 修改
  4. 失败自动重试（默认 3 次，可在 config.ini 修改）
  5. HTTP 404 视为该书不存在，直接跳过
  6. 被限速(HTTP 429)时尊重 Retry-After 并触发全局冷却，避免多线程惊群
  7. Cloudflare 兜底：curl_cffi -> requests -> cloudscraper 自动降级链
  8. 已存在的文件默认跳过（断点续下），--overwrite 强制重下

用法：
  python download_novel.py -d 1            # 下载单本
  python download_novel.py -d 1-1000       # 下载范围
  python download_novel.py -d 5,68         # 下载指定多本
  python download_novel.py -d 1-10,20,30-35  # 混合：范围 + 指定
  python download_novel.py -d 1-5000 -c my.ini --overwrite

依赖：requests（curl_cffi / cloudscraper 可选，用于 Cloudflare 兜底）
"""
import argparse
import configparser
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BASE_URL = "https://dl2.wenku8.com/txtutf8"
REFERER = "https://www.wenku8.cc/"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
ENGINES = ("curl_cffi", "requests", "cloudscraper")
CF_RE = re.compile(r"cf-chl|challenge-platform|cf-mitigated|Just a moment|__cf_chl", re.I)

DEFAULT_CONFIG = """\
[download]
threads = 8         ; 下载线程数
engine = auto       ; auto / curl_cffi / requests / cloudscraper
output_dir = txt    ; 相对脚本所在目录的输出文件夹
timeout = 120       ; 单请求超时(秒)
retries = 3         ; 下载失败重试次数
req_per_min = 40    ; 全局限速：每分钟请求数上限
request_delay = 0   ; 请求间附加延时(秒)，被限速时可调大
cookie =            ; 可选：被 Cloudflare 拦截时填浏览器解出的 cf_clearance 等 cookie
"""

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
# 限速
# ---------------------------------------------------------------------------
class RateLimiter:
    """令牌桶限速，rate_per_min 为每分钟请求数上限，burst 为突发容量。线程安全。"""

    def __init__(self, rate_per_min, burst):
        self._rate = max(rate_per_min, 1) / 60.0
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


# ---------------------------------------------------------------------------
# URL / 请求头 / 会话
# ---------------------------------------------------------------------------
def book_url(book_id):
    return f"{BASE_URL}/{book_id // 1000}/{book_id}.txt"


def _browser_headers():
    return {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": REFERER,
        "Connection": "keep-alive",
    }


def _make_session(engine):
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


def _is_cf_challenge(status, content):
    if status in (403, 503, 429):
        head = content[:4096].decode("utf-8", "ignore")
        if CF_RE.search(head):
            return True
    return False


class DownloadClient:
    """多线程安全的 HTTP 客户端：每线程独立 Session + 全局引擎降级 + 全局限速/冷却 + 重试。"""

    def __init__(self, engine="auto", timeout=120, retries=3, cookie="",
                 req_per_min=40, workers=8):
        if engine == "auto":
            engine = "curl_cffi"
            try:
                import curl_cffi  # noqa: F401
            except Exception:
                engine = "requests"
        self._engine_idx = ENGINES.index(engine)
        self._timeout = timeout
        self._retries = retries
        self._cookie = cookie
        self._limiter = RateLimiter(req_per_min, max(workers * 4, 4))
        self._cooldown_until = 0.0
        self._cooldown_lock = threading.Lock()
        self._local = threading.local()
        self._idx_lock = threading.Lock()

    def _engine_name(self):
        with self._idx_lock:
            return ENGINES[self._engine_idx]

    def _get(self, url, headers):
        with self._idx_lock:
            global_idx = self._engine_idx
        local_idx = getattr(self._local, "idx", None)
        if local_idx != global_idx:
            self._local.sess = None
            self._local.idx = global_idx
        sess = getattr(self._local, "sess", None)
        if sess is None:
            sess = _make_session(ENGINES[global_idx])
            self._local.sess = sess
            self._local.idx = global_idx
        if self._cookie:
            headers = dict(headers)
            headers["Cookie"] = self._cookie
        return sess.get(url, headers=headers, timeout=self._timeout)

    def _escalate_engine(self):
        with self._idx_lock:
            if self._engine_idx < len(ENGINES) - 1:
                self._engine_idx += 1
                return True
            return False

    def _wait_cooldown(self):
        """任何线程被限速后，所有线程都暂停到冷却结束，避免惊群重试。"""
        while True:
            with self._cooldown_lock:
                until = self._cooldown_until
            now = time.monotonic()
            if until <= now:
                return
            time.sleep(min(until - now, 1.0))

    def _note_rate_limited(self, wait):
        """记录一次全局限速：至少让全体冷却 10 秒，更长的按 wait 算。"""
        with self._cooldown_lock:
            self._cooldown_until = max(self._cooldown_until,
                                       time.monotonic() + max(wait, 10))

    @staticmethod
    def _retry_after(resp):
        try:
            ra = resp.headers.get("Retry-After")
        except Exception:
            return 0
        return int(ra) if ra and str(ra).isdigit() else 0

    @staticmethod
    def _atomic_write(path, data):
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def download(self, book_id, out_path, overwrite=False):
        """返回状态: ok / missing / exists / failed。"""
        if not overwrite and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return "exists"
        url = book_url(book_id)
        last_err = None
        for attempt in range(self._retries + 1):
            self._wait_cooldown()
            self._limiter.acquire()
            try:
                resp = self._get(url, _browser_headers())
                status = int(getattr(resp, "status_code", 0))
                content = getattr(resp, "content", None) or b""
                if _is_cf_challenge(status, content):
                    if self._escalate_engine():
                        log(f"book {book_id} 检测到 Cloudflare 挑战({status})，升级引擎 -> {self._engine_name()}", "WARN")
                        continue
                    last_err = f"Cloudflare 挑战 (HTTP {status})"
                    log(f"book {book_id} 所有引擎均被 Cloudflare 拦截", "WARN")
                elif status == 404:
                    return "missing"
                elif status < 400:
                    self._atomic_write(out_path, content)
                    return "ok"
                elif status == 429:
                    wait = min(max(self._retry_after(resp), 5 * (2 ** attempt)), 60)
                    last_err = "HTTP 429 限速"
                    self._note_rate_limited(wait)
                    log(f"book {book_id} 被限速(429)，全局冷却 {max(wait, 10)}s 后重试 {attempt + 1}/{self._retries}", "WARN")
                    if attempt < self._retries:
                        time.sleep(wait)
                        continue
                else:
                    last_err = f"HTTP {status}"
            except Exception as e:
                last_err = str(e)
            if attempt < self._retries:
                wait = min(2 ** attempt, 30)
                log(f"book {book_id} 下载失败({last_err})，{wait}s 后重试 {attempt + 1}/{self._retries}", "WARN")
                time.sleep(wait)
        return "failed"


# ---------------------------------------------------------------------------
# 参数 / 配置
# ---------------------------------------------------------------------------
def parse_spec(spec):
    """解析下载规格：单 ID(5)、区间(1-1000)、列表(5,68) 或混合(1-10,20,30-35)。返回去重保序的 ID 列表。"""
    ids = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"参数中包含空片段: {spec!r}")
        if "-" in part:
            left, right = part.split("-", 1)
            start, end = int(left), int(right)
            if start < 1:
                raise ValueError(f"起始 ID({start}) 必须 >= 1")
            if end < start:
                raise ValueError(f"结束 ID({end}) 不能小于起始 ID({start})")
            ids.extend(range(start, end + 1))
        else:
            value = int(part)
            if value < 1:
                raise ValueError(f"书籍 ID({value}) 必须 >= 1")
            ids.append(value)
    return list(dict.fromkeys(ids))


def load_config(path):
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG)
        log(f"未找到配置文件，已生成默认配置: {path}", "INFO")
    else:
        cfg.read(path, encoding="utf-8")
    sec = cfg["download"] if cfg.has_section("download") else {}
    return {
        "threads": max(1, int(sec.get("threads", 8))),
        "engine": sec.get("engine", "auto").strip().lower() or "auto",
        "output_dir": (sec.get("output_dir", "txt") or "txt").strip(),
        "timeout": max(1, int(sec.get("timeout", 120))),
        "retries": max(0, int(sec.get("retries", 3))),
        "req_per_min": max(1, int(sec.get("req_per_min", 40))),
        "request_delay": max(0.0, float(sec.get("request_delay", 0))),
        "cookie": (sec.get("cookie") or "").strip(),
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="wenku8 轻小说 txt 批量下载器")
    ap.add_argument("-d", "--download", required=True,
                    help="下载规格：单个 ID(如 1)、区间(如 1-1000) 或列表(如 5,68)，可混合(如 1-10,20,30-35)")
    ap.add_argument("-c", "--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini"),
                    help="配置文件路径（默认脚本同级 config.ini）")
    ap.add_argument("--overwrite", action="store_true", help="强制重下已存在的文件")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        book_ids = list(parse_spec(args.download))
    except ValueError as e:
        log(f"参数错误: {e}", "ERROR")
        sys.exit(1)

    cfg = load_config(args.config)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(base_dir, cfg["output_dir"])
    os.makedirs(out_dir, exist_ok=True)

    cores = os.cpu_count()
    threads = cfg["threads"]
    log(f"CPU 核心数: {cores}，下载线程数: {threads}，限速: {cfg['req_per_min']} 请求/分钟"
        f"（config: {args.config}）", "INFO")
    log(f"待下载: {len(book_ids)} 本，输出目录: {out_dir}", "INFO")

    client = DownloadClient(engine=cfg["engine"], timeout=cfg["timeout"],
                            retries=cfg["retries"], cookie=cfg["cookie"],
                            req_per_min=cfg["req_per_min"], workers=threads)
    delay = cfg["request_delay"]

    total = len(book_ids)
    done = 0
    counter = {"ok": 0, "missing": 0, "exists": 0, "failed": 0}
    failed_ids, missing_ids = [], []
    _prog_lock = threading.Lock()


    def worker(bid):
        result = client.download(bid, os.path.join(out_dir, f"{bid}.txt"), overwrite=args.overwrite)
        if delay > 0:
            time.sleep(delay * (0.5 + random.random()))
        return bid, result

    try:
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futures = {ex.submit(worker, bid): bid for bid in book_ids}
            for fut in as_completed(futures):
                bid = futures[fut]
                try:
                    bid, result = fut.result()
                except Exception as e:
                    result = "failed"
                    log(f"book {bid} 任务异常: {e}", "ERROR")
                counter[result] += 1
                if result == "failed":
                    failed_ids.append(bid)
                elif result == "missing":
                    missing_ids.append(bid)
                with _prog_lock:
                    done += 1
                log(f"进度 {done}/{total}: book {bid} -> {result}", "INFO")
    except KeyboardInterrupt:
        log("用户中断，正在汇总已完成的下载...", "WARN")

    log("==== 下载完成汇总 ====", "INFO")
    log(f"成功: {counter['ok']}    不存在(404): {counter['missing']}    "
        f"已存在跳过: {counter['exists']}    失败: {counter['failed']}", "INFO")
    if missing_ids:
        log(f"不存在的书: {missing_ids[:50]}{' ...' if len(missing_ids) > 50 else ''}", "WARN")
    if failed_ids:
        log(f"失败的书: {failed_ids[:50]}{' ...' if len(failed_ids) > 50 else ''}（可用 --overwrite 重试）", "ERROR")
    log(f"文件已保存到: {out_dir}", "INFO")
    sys.exit(1 if failed_ids else 0)


if __name__ == "__main__":
    main()
