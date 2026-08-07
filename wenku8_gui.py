#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wenku8 工具箱 GUI —— 整合 download_novel.py 与 build_epub.py

用法：
  python wenku8_gui.py                # 开发模式运行
  WENKU8_GUI_SMOKE=30 python wenku8_gui.py   # 冒烟测试：渲染 30 帧后自动退出

技术要点：
  - 直接 import 两个脚本模块并在后台线程调用其 main()（脚本带 __main__ 保护，
    可安全 import）；monkey-patch 模块级 log 捕获结构化日志/进度。
  - 打包（PyInstaller）后脚本逻辑被内嵌进 exe，无需外部文件。
"""
from contextlib import contextmanager

import ctypes
import json
import os
import queue
import re
import sys
import threading
import time

import dearpygui.dearpygui as dpg

import build_epub
import download_novel

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    # 打包后 __file__ 指向 _MEIPASS 临时目录，重置为 exe 所在目录，
    # 使脚本的相对路径基准（config 默认路径 / output_dir）落在可写目录。
    download_novel.__file__ = os.path.join(APP_DIR, "download_novel.py")
    build_epub.__file__ = os.path.join(APP_DIR, "build_epub.py")
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

APP_NAME = "wenku8 GUI"
SETTINGS_PATH = os.path.join(APP_DIR, "gui_settings.json")
DEFAULT_CONFIG_PATH = os.path.join(APP_DIR, "config.ini")
DEFAULT_TXT_OUT = os.path.join(APP_DIR, "txt")
DEFAULT_EPUB_OUT = os.path.join(APP_DIR, "epub_out")

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]
ENGINE_CHOICES = ["auto", "curl_cffi", "requests", "cloudscraper"]
COVER_CHOICES = ["auto", "api"]

# ---------------------------------------------------------------------------
# DPI 适配
# ---------------------------------------------------------------------------
# DPG 在 Windows 上使用物理像素（无自动缩放），高 DPI 屏（125%+）下界面会偏小。
# 启动时按系统 DPI 计算缩放比，放大字体、窗口与控件尺寸。
# 环境变量 WENKU8_GUI_DPI_OVERRIDE 可强制覆盖（用于测试 / 非标准布局）。
_DPI_SCALE = 1.0


def _detect_dpi():
    global _DPI_SCALE
    try:
        override = float(os.environ.get("WENKU8_GUI_DPI_OVERRIDE", ""))
        if override > 0:
            _DPI_SCALE = override
            return override
    except ValueError:
        pass
    # per-monitor v2；进程级 DPI 感知只能设置一次，失败（已声明）则忽略
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        _DPI_SCALE = max(1.0, dpi / 96.0)
    except Exception:
        _DPI_SCALE = 1.0
    return _DPI_SCALE


def _s(v):
    """按 DPI scale 放大像素值。"""
    return int(round(v * _DPI_SCALE))


RE_DL_PROGRESS = re.compile(r"进度\s*(\d+)\s*/\s*(\d+)")
RE_DL_TOTAL = re.compile(r"待下载:\s*(\d+)\s*本")
RE_DL_SUMMARY = re.compile(r"成功:\s*(\d+).*不存在\(404\):\s*(\d+).*已存在跳过:\s*(\d+).*失败:\s*(\d+)")
RE_EPUB_IMG_PROGRESS = re.compile(r"图片进度\s*(\d+)\s*/\s*(\d+)")
RE_EPUB_BOOK_START = re.compile(r"==== 处理\s+.+\s+\(aid=(\d+)\)\s*====")

# ---------------------------------------------------------------------------
# i18n
# ---------------------------------------------------------------------------
_T = {
    # 菜单
    "menu.file":       {"en": "File", "zh": "文件"},
    "menu.file.config": {"en": "Select config.ini...", "zh": "选择 config.ini..."},
    "menu.file.openappdir": {"en": "Open app folder", "zh": "打开程序目录"},
    "menu.file.exit":  {"en": "Exit", "zh": "退出"},
    "menu.view":       {"en": "View", "zh": "视图"},
    "menu.view.lang":  {"en": "Language", "zh": "语言"},
    "menu.view.lang.en": {"en": "English", "zh": "英语"},
    "menu.view.lang.zh": {"en": "中文", "zh": "中文"},
    "menu.view.theme": {"en": "Theme", "zh": "主题"},
    "menu.view.theme.dark": {"en": "Dark", "zh": "暗黑"},
    "menu.view.theme.light": {"en": "Light", "zh": "明亮"},
    "menu.help":       {"en": "Help", "zh": "帮助"},
    "menu.help.about": {"en": "About", "zh": "关于"},
    # Tab
    "tab.download":    {"en": "Downloader", "zh": "下载器"},
    "tab.build":       {"en": "EPUB Builder", "zh": "EPUB 生成"},
    # 下载器
    "dl.settings":     {"en": "Settings", "zh": "设置"},
    "dl.spec":         {"en": "Book range (e.g. 1-1000)", "zh": "书籍范围（如 1-1000）"},
    "dl.outdir":       {"en": "Output folder", "zh": "输出文件夹"},
    "dl.browse":       {"en": "Browse", "zh": "浏览"},
    "dl.threads":      {"en": "Threads", "zh": "线程数"},
    "dl.engine":       {"en": "HTTP engine", "zh": "HTTP 引擎"},
    "dl.timeout":      {"en": "Timeout (s)", "zh": "超时（秒）"},
    "dl.retries":      {"en": "Retries", "zh": "重试次数"},
    "dl.delay":        {"en": "Request delay (s)", "zh": "请求间隔（秒）"},
    "dl.cookie":       {"en": "Cookie (optional)", "zh": "Cookie（可选）"},
    "dl.overwrite":    {"en": "Overwrite existing files", "zh": "覆盖已存在文件"},
    "dl.config":       {"en": "Config file", "zh": "配置文件"},
    "dl.start":        {"en": "Start", "zh": "开始"},
    "dl.cancel":       {"en": "Cancel", "zh": "取消"},
    "dl.stats":        {"en": "OK: 0   Missing: 0   Skipped: 0   Failed: 0", "zh": "成功: 0   不存在: 0   已存在: 0   失败: 0"},
    "dl.logtitle":     {"en": "Log", "zh": "日志"},
    # EPUB 生成
    "bd.settings":     {"en": "Settings", "zh": "设置"},
    "bd.txt":          {"en": "TXT file / folder", "zh": "TXT 文件或文件夹"},
    "bd.txt.file":     {"en": "File", "zh": "文件"},
    "bd.txt.dir":      {"en": "Folder", "zh": "文件夹"},
    "bd.out":          {"en": "EPUB output folder", "zh": "EPUB 输出文件夹"},
    "bd.aid":          {"en": "Book id (aid, optional)", "zh": "书籍 id（aid，可选）"},
    "bd.engine":       {"en": "HTTP engine", "zh": "HTTP 引擎"},
    "bd.workers":      {"en": "Image workers", "zh": "图片并发数"},
    "bd.delay":        {"en": "Request delay (s)", "zh": "请求间隔（秒）"},
    "bd.rpm":          {"en": "Requests / min", "zh": "每分钟请求上限"},
    "bd.cache":        {"en": "Image cache folder", "zh": "图片缓存文件夹"},
    "bd.jar":          {"en": "Cookie jar (optional)", "zh": "Cookie 保存文件（可选）"},
    "bd.cover":        {"en": "Cover", "zh": "封面"},
    "bd.epubcheck":    {"en": "epubcheck.jar (optional)", "zh": "epubcheck.jar（可选）"},
    "bd.resume":       {"en": "Resume (reuse image cache)", "zh": "断点续跑（复用图片缓存）"},
    "bd.scanall":      {"en": "Scan all chapters for images", "zh": "扫描所有章节提取图片"},
    "bd.nocover":      {"en": "No cover", "zh": "不设置封面"},
    "bd.start":        {"en": "Start", "zh": "开始"},
    "bd.cancel":       {"en": "Cancel", "zh": "取消"},
    "bd.progress":     {"en": "Books", "zh": "书籍"},
    "bd.imgprogress":  {"en": "Images", "zh": "图片"},
    "bd.logtitle":     {"en": "Log", "zh": "日志"},
    # 通用
    "common.starting": {"en": "Starting...", "zh": "启动中..."},
    "common.busy":     {"en": "Running", "zh": "运行中"},
    "status.idle":     {"en": "Ready", "zh": "就绪"},
    "status.running_dl": {"en": "Downloading...", "zh": "下载中..."},
    "status.running_build": {"en": "Building EPUB...", "zh": "正在生成 EPUB..."},
    "status.done_ok":  {"en": "Finished", "zh": "完成"},
    "status.done_fail": {"en": "Finished with errors", "zh": "完成（有错误）"},
    "status.cancelled": {"en": "Cancelled", "zh": "已取消"},
    "status.task_dl":  {"en": "download", "zh": "下载"},
    "status.task_build": {"en": "EPUB build", "zh": "EPUB 生成"},
    "about.title":     {"en": "About", "zh": "关于"},
    "about.text":      {"en": "wenku8 toolbox GUI.\nIntegrates download_novel.py and build_epub.py.\n\nBoth scripts run inside this app (no external files needed).\nBe polite: keep the default rate limits.", "zh": "wenku8 工具箱 GUI。\n整合 download_novel.py 与 build_epub.py。\n\n两个脚本在本程序内运行（无需外部文件）。\n请保持礼貌：勿调高请求频率，以免被 Cloudflare 封禁。"},
    "about.close":     {"en": "Close", "zh": "关闭"},
    "msg.txt_required": {"en": "Please choose a TXT file or folder first.", "zh": "请先选择 TXT 文件或文件夹。"},
    "msg.spec_required": {"en": "Please enter a book range, e.g. 1-1000.", "zh": "请输入书籍范围，如 1-1000。"},
    "msg.busy":        {"en": "Another task is running. Please wait.", "zh": "已有任务在运行，请稍候。"},
}
LANG = "en"

# (tag, attr) 列表，语言切换时更新
_I18N = {}


def t(key):
    return _T[key][LANG]


def _reg(key, tag, attr="label"):
    _I18N.setdefault(key, []).append((tag, attr))


def set_language(lang):
    global LANG
    LANG = lang
    for key, items in _I18N.items():
        val = _T[key][lang]
        for tag, attr in items:
            try:
                dpg.configure_item(tag, **{attr: val})
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 主题（暗黑 / 明亮）
# ---------------------------------------------------------------------------
DARK_PALETTE = {
    "colors": {
        "mvThemeCol_Text": (232, 234, 240, 255),
        "mvThemeCol_TextDisabled": (140, 144, 154, 255),
        "mvThemeCol_WindowBg": (26, 28, 34, 255),
        "mvThemeCol_ChildBg": (20, 22, 27, 255),
        "mvThemeCol_PopupBg": (32, 34, 40, 255),
        "mvThemeCol_Border": (56, 60, 70, 255),
        "mvThemeCol_BorderShadow": (0, 0, 0, 0),
        "mvThemeCol_FrameBg": (42, 45, 53, 255),
        "mvThemeCol_FrameBgHovered": (54, 58, 68, 255),
        "mvThemeCol_FrameBgActive": (64, 68, 80, 255),
        "mvThemeCol_TitleBg": (24, 26, 31, 255),
        "mvThemeCol_TitleBgActive": (48, 66, 112, 255),
        "mvThemeCol_TitleBgCollapsed": (28, 30, 36, 255),
        "mvThemeCol_MenuBarBg": (30, 32, 38, 255),
        "mvThemeCol_ScrollbarBg": (28, 30, 36, 255),
        "mvThemeCol_ScrollbarGrab": (70, 74, 86, 255),
        "mvThemeCol_ScrollbarGrabHovered": (84, 88, 102, 255),
        "mvThemeCol_ScrollbarGrabActive": (98, 104, 120, 255),
        "mvThemeCol_CheckMark": (120, 180, 255, 255),
        "mvThemeCol_SliderGrab": (120, 180, 255, 255),
        "mvThemeCol_SliderGrabActive": (150, 200, 255, 255),
        "mvThemeCol_Button": (58, 86, 150, 255),
        "mvThemeCol_ButtonHovered": (72, 104, 178, 255),
        "mvThemeCol_ButtonActive": (48, 72, 128, 255),
        "mvThemeCol_Header": (52, 70, 118, 255),
        "mvThemeCol_HeaderHovered": (64, 88, 148, 255),
        "mvThemeCol_HeaderActive": (44, 62, 106, 255),
        "mvThemeCol_Separator": (56, 60, 70, 255),
        "mvThemeCol_SeparatorHovered": (72, 104, 178, 255),
        "mvThemeCol_Tab": (36, 40, 50, 255),
        "mvThemeCol_TabHovered": (52, 70, 118, 255),
        "mvThemeCol_TabActive": (58, 86, 150, 255),
        "mvThemeCol_TabUnfocused": (36, 40, 50, 255),
        "mvThemeCol_TabUnfocusedActive": (46, 52, 66, 255),
        "mvThemeCol_PlotHistogram": (64, 130, 220, 255),
        "mvThemeCol_PlotHistogramHovered": (84, 150, 235, 255),
        "mvThemeCol_TableHeaderBg": (36, 40, 50, 255),
        "mvThemeCol_TableRowBg": (0, 0, 0, 0),
        "mvThemeCol_TableRowBgAlt": (34, 36, 44, 255),
    },
    "styles": {
        "mvStyleVar_WindowPadding": (12, 12),
        "mvStyleVar_FramePadding": (7, 5),
        "mvStyleVar_ItemSpacing": (8, 8),
        "mvStyleVar_ItemInnerSpacing": (6, 6),
        "mvStyleVar_FrameRounding": (4, 0),
        "mvStyleVar_TabRounding": (4, 0),
        "mvStyleVar_WindowBorderSize": (1, 0),
        "mvStyleVar_FrameBorderSize": (1, 0),
        "mvStyleVar_ScrollbarRounding": (3, 0),
        "mvStyleVar_ScrollbarSize": (12, 0),
    },
}

LIGHT_PALETTE = {
    "colors": {
        "mvThemeCol_Text": (40, 42, 50, 255),
        "mvThemeCol_TextDisabled": (150, 152, 160, 255),
        "mvThemeCol_WindowBg": (247, 247, 249, 255),
        "mvThemeCol_ChildBg": (240, 240, 244, 255),
        "mvThemeCol_PopupBg": (252, 252, 254, 255),
        "mvThemeCol_Border": (204, 206, 214, 255),
        "mvThemeCol_BorderShadow": (0, 0, 0, 0),
        "mvThemeCol_FrameBg": (235, 235, 240, 255),
        "mvThemeCol_FrameBgHovered": (224, 225, 232, 255),
        "mvThemeCol_FrameBgActive": (210, 212, 220, 255),
        "mvThemeCol_TitleBg": (222, 224, 230, 255),
        "mvThemeCol_TitleBgActive": (58, 110, 180, 255),
        "mvThemeCol_TitleBgCollapsed": (232, 232, 238, 255),
        "mvThemeCol_MenuBarBg": (238, 238, 242, 255),
        "mvThemeCol_ScrollbarBg": (240, 240, 244, 255),
        "mvThemeCol_ScrollbarGrab": (180, 184, 194, 255),
        "mvThemeCol_ScrollbarGrabHovered": (160, 165, 176, 255),
        "mvThemeCol_ScrollbarGrabActive": (140, 146, 158, 255),
        "mvThemeCol_CheckMark": (30, 80, 140, 255),
        "mvThemeCol_SliderGrab": (58, 110, 180, 255),
        "mvThemeCol_SliderGrabActive": (40, 88, 150, 255),
        "mvThemeCol_Button": (58, 110, 180, 255),
        "mvThemeCol_ButtonHovered": (70, 125, 200, 255),
        "mvThemeCol_ButtonActive": (48, 95, 160, 255),
        "mvThemeCol_Header": (58, 110, 180, 255),
        "mvThemeCol_HeaderHovered": (70, 125, 200, 255),
        "mvThemeCol_HeaderActive": (48, 95, 160, 255),
        "mvThemeCol_Separator": (204, 206, 214, 255),
        "mvThemeCol_SeparatorHovered": (58, 110, 180, 255),
        "mvThemeCol_Tab": (230, 230, 236, 255),
        "mvThemeCol_TabHovered": (210, 216, 230, 255),
        "mvThemeCol_TabActive": (58, 110, 180, 255),
        "mvThemeCol_TabUnfocused": (230, 230, 236, 255),
        "mvThemeCol_TabUnfocusedActive": (214, 214, 222, 255),
        "mvThemeCol_PlotHistogram": (58, 130, 210, 255),
        "mvThemeCol_PlotHistogramHovered": (70, 145, 225, 255),
        "mvThemeCol_TableHeaderBg": (230, 230, 236, 255),
        "mvThemeCol_TableRowBg": (0, 0, 0, 0),
        "mvThemeCol_TableRowBgAlt": (243, 243, 247, 255),
    },
    "styles": DARK_PALETTE["styles"],
}

_themes = {}


def apply_theme(name):
    th = _themes.get(name)
    if th is not None:
        dpg.bind_theme(th)


def _build_themes():
    for name, palette in (("dark", DARK_PALETTE), ("light", LIGHT_PALETTE)):
        with dpg.theme() as th:
            with dpg.theme_component(dpg.mvThemeCat_Core):
                for col, val in palette["colors"].items():
                    dpg.add_theme_color(getattr(dpg, col), val, category=dpg.mvThemeCat_Core)
                for st, v in palette["styles"].items():
                    dpg.add_theme_style(getattr(dpg, st), _s(v[0]), _s(v[1]),
                                        category=dpg.mvThemeCat_Core)
        _themes[name] = th


# ---------------------------------------------------------------------------
# 日志视图（DPG 2.x 已移除 add_logger，自实现）
# ---------------------------------------------------------------------------
LOG_COLORS = {
    "INFO": None,
    "WARN": (240, 180, 90, 255),
    "ERROR": (240, 100, 100, 255),
    "EPUBCHECK": (120, 180, 220, 255),
}


class LogView:
    def __init__(self, container_tag, max_lines=2000):
        self.container = container_tag
        self.max_lines = max_lines
        self._tags = []

    def append(self, tag, msg):
        color = LOG_COLORS.get(tag)
        text = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        kw = {"parent": self.container, "wrap": 0}
        if color:
            kw["color"] = color
        line = dpg.add_text(text, **kw)
        self._tags.append(line)
        if len(self._tags) > self.max_lines:
            old = self._tags.pop(0)
            if dpg.does_item_exist(old):
                dpg.delete_item(old)
        dpg.set_y_scroll(self.container, 1e9)

    def clear(self):
        for line in self._tags:
            if dpg.does_item_exist(line):
                dpg.delete_item(line)
        self._tags.clear()


# ---------------------------------------------------------------------------
# 任务运行器（后台线程 + 队列；UI 每帧消费）
# ---------------------------------------------------------------------------
class TaskRunner:
    def __init__(self):
        self._queue = queue.Queue()
        self._thread = None
        self._cancel = threading.Event()
        self._active = False
        self._lock = threading.Lock()

    @property
    def busy(self):
        with self._lock:
            return self._active

    def start(self, kind):
        self._cancel.clear()
        self._active = True
        self._thread = threading.Thread(target=self._wrap, args=(kind,), daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel.set()

    def _wrap(self, kind):
        self._queue.put(("begin", kind, None))
        code = 0
        cancelled = False
        try:
            if kind == "download":
                self._run_download()
            else:
                self._run_build()
        except KeyboardInterrupt:
            cancelled = self._cancel.is_set()
            code = -1
            self._queue.put(("log", kind, "WARN", "Task interrupted." if LANG == "en" else "任务已中断。"))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 0
        except Exception as e:
            code = 1
            self._queue.put(("log", kind, "ERROR", "Unhandled error: %r" % (e,)))
        finally:
            self._queue.put(("end", kind, code, cancelled))
            with self._lock:
                self._active = False

    def _make_log(self, kind):
        def _log(msg, tag="INFO"):
            if self._cancel.is_set():
                raise KeyboardInterrupt()
            self._queue.put(("log", kind, tag, msg))
        return _log

    def _run_download(self):
        download_novel.log = self._make_log("download")
        spec = _get_str("in_dl_spec")
        config_path = _get_str("in_dl_config") or DEFAULT_CONFIG_PATH
        overwrite = _get_bool("chk_dl_overwrite")
        _write_config(config_path, spec)
        sys.argv = ["download_novel.py", "-d", spec, "-c", config_path]
        if overwrite:
            sys.argv.append("--overwrite")
        download_novel.main()

    def _run_build(self):
        build_epub.log = self._make_log("build")
        argv = ["--txt", _get_str("in_bd_txt"), "--out", _get_str("in_bd_out") or DEFAULT_EPUB_OUT,
                "--engine", _get_str("cmb_bd_engine"),
                "--workers", str(_get_int("in_bd_workers", 3)),
                "--delay", str(_get_float("in_bd_delay", 0.0)),
                "--req-per-min", str(_get_int("in_bd_rpm", 60))]
        aid = _get_str("in_bd_aid").strip()
        if aid:
            argv += ["--aid", aid]
        cache = _get_str("in_bd_cache").strip()
        if cache:
            argv += ["--cache", cache]
        jar = _get_str("in_bd_jar").strip()
        if jar:
            argv += ["--cookie-jar", jar]
        ec = _get_str("in_bd_epubcheck").strip()
        if ec:
            argv += ["--epubcheck", ec]
        if _get_bool("chk_bd_resume") is False:
            argv.append("--no-resume")
        if _get_bool("chk_bd_scanall"):
            argv.append("--scan-all")
        if _get_bool("chk_bd_nocover"):
            argv.append("--no-cover")
        cover = _get_str("cmb_bd_cover")
        if cover != "auto":
            argv += ["--cover", cover]
        sys.argv = ["build_epub.py"] + argv
        build_epub.main()


# ---------------------------------------------------------------------------
# 控件取值辅助
# ---------------------------------------------------------------------------
def _get_str(tag):
    return str(dpg.get_value(tag) or "")


def _get_bool(tag):
    return bool(dpg.get_value(tag))


def _get_int(tag, default=0):
    try:
        return int(float(dpg.get_value(tag) or default))
    except (TypeError, ValueError):
        return default


def _get_float(tag, default=0.0):
    try:
        return float(dpg.get_value(tag) or default)
    except (TypeError, ValueError):
        return default


def _write_config(path, spec):
    """按 GUI 表单生成 download_novel 的 config.ini（output_dir 用绝对路径）。"""
    out_dir = _get_str("in_dl_outdir").strip() or DEFAULT_TXT_OUT
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = (
        "[download]\n"
        "threads = %d\n" % _get_int("in_dl_threads", 32)
        + "engine = %s\n" % _get_str("cmb_dl_engine")
        + "output_dir = %s\n" % out_dir
        + "timeout = %d\n" % _get_int("in_dl_timeout", 30)
        + "retries = %d\n" % _get_int("in_dl_retries", 3)
        + "request_delay = %s\n" % _get_float("in_dl_delay", 0.0)
        + "cookie = %s\n" % _get_str("in_dl_cookie")
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------
_runner = TaskRunner()
_views = {}          # kind -> LogView
_stats = {"ok": 0, "missing": 0, "exists": 0, "failed": 0}
_dl_total = 0
_dl_done = 0
_build_total = 1
_build_done = 0
_img_done = 0
_img_total = 0


def _set_progress(tag, done, total):
    frac = min(1.0, done / total) if total > 0 else 0.0
    dpg.set_value(tag, frac)
    dpg.configure_item(tag, overlay="%d / %d   (%.0f%%)" % (done, total, frac * 100))


def _stats_text():
    if LANG == "en":
        return ("OK: %d   Missing: %d   Skipped: %d   Failed: %d"
                % (_stats["ok"], _stats["missing"], _stats["exists"], _stats["failed"]))
    return ("成功: %d   不存在: %d   已存在: %d   失败: %d"
            % (_stats["ok"], _stats["missing"], _stats["exists"], _stats["failed"]))


def _set_status(key, extra=""):
    text = _T[key][LANG]
    if extra:
        text += "   —   " + extra
    dpg.set_value("tx_status", text)


def _set_busy_ui(busy):
    dpg.configure_item("btn_dl_start", enabled=not busy)
    dpg.configure_item("btn_build_start", enabled=not busy)
    dpg.configure_item("btn_dl_cancel", enabled=busy)
    dpg.configure_item("btn_build_cancel", enabled=busy)


# ---------------------------------------------------------------------------
# 队列消费
# ---------------------------------------------------------------------------
def _poll_queue():
    while True:
        try:
            msg = _runner._queue.get_nowait()
        except queue.Empty:
            break
        _handle_msg(msg)


def _handle_msg(msg):
    global _dl_total, _dl_done, _build_total, _build_done, _img_done, _img_total
    kind = msg[1]
    if msg[0] == "begin":
        _views[kind].clear()
        dpg.set_value("pb_dl", 0.0)
        dpg.configure_item("pb_dl", overlay="")
        dpg.set_value("pb_build", 0.0)
        dpg.configure_item("pb_build", overlay="")
        dpg.set_value("pb_build_img", 0.0)
        dpg.configure_item("pb_build_img", overlay="")
        _dl_total = _dl_done = 0
        _build_total = 1
        _build_done = _img_done = _img_total = 0
        _stats.update(ok=0, missing=0, exists=0, failed=0)
        dpg.set_value("tx_dl_stats", _stats_text())
        if kind == "download":
            txt = _get_str("in_bd_txt")
            if os.path.isdir(txt):
                _build_total = len([f for f in os.listdir(txt) if re.search(r"\d+\.txt$", f, re.I)])
            else:
                _build_total = 1
            _set_busy_ui(True)
            _set_status("status.running_dl")
        else:
            _set_busy_ui(True)
            _set_status("status.running_build")
        return
    if msg[0] == "log":
        _, kind, tag, text = msg
        _views[kind].append(tag, text)
        if kind == "download":
            m = RE_DL_PROGRESS.search(text)
            if m:
                _dl_done, _dl_total = int(m.group(1)), int(m.group(2))
                _set_progress("pb_dl", _dl_done, _dl_total)
            m = RE_DL_TOTAL.search(text)
            if m and _dl_total == 0:
                _dl_total = int(m.group(1))
            m = RE_DL_SUMMARY.search(text)
            if m:
                _stats.update(ok=int(m.group(1)), missing=int(m.group(2)),
                              exists=int(m.group(3)), failed=int(m.group(4)))
                dpg.set_value("tx_dl_stats", _stats_text())
        else:
            m = RE_EPUB_IMG_PROGRESS.search(text)
            if m:
                _img_done, _img_total = int(m.group(1)), int(m.group(2))
                _set_progress("pb_build_img", _img_done, _img_total)
            m = RE_EPUB_BOOK_START.search(text)
            if m:
                _build_done += 1
                _set_progress("pb_build", _build_done, _build_total)
        return
    if msg[0] == "end":
        _, kind, code, cancelled = msg
        _set_busy_ui(False)
        if cancelled:
            _set_status("status.cancelled")
        elif code in (0, None):
            _set_status("status.done_ok")
        else:
            _set_status("status.done_fail")
        return


# ---------------------------------------------------------------------------
# 设置持久化
# ---------------------------------------------------------------------------
def _default_settings():
    return {
        "lang": "en",
        "theme": "dark",
        "width": 1180,
        "height": 760,
        "dl": {
            "spec": "1-1000", "outdir": "", "threads": 32, "engine": "auto",
            "timeout": 30, "retries": 3, "delay": 0.0, "cookie": "",
            "overwrite": False, "config": "",
        },
        "build": {
            "txt": "", "out": "", "aid": "", "engine": "auto", "workers": 3,
            "delay": 0.0, "rpm": 60, "cache": "", "jar": "", "cover": "auto",
            "epubcheck": "", "resume": True, "scanall": False, "nocover": False,
        },
    }


def _load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = _default_settings()
        for k in ("dl", "build"):
            if isinstance(data.get(k), dict):
                merged[k].update(data[k])
        for k in ("lang", "theme", "width", "height"):
            if k in data:
                merged[k] = data[k]
        return merged
    except Exception:
        return _default_settings()


def _save_settings():
    data = _default_settings()
    data["lang"] = LANG
    data["theme"] = _current_theme
    # 保存逻辑尺寸（除以 DPI scale），恢复时再乘回
    data["width"] = round(dpg.get_viewport_width() / _DPI_SCALE)
    data["height"] = round(dpg.get_viewport_height() / _DPI_SCALE)
    data["dl"] = {
        "spec": _get_str("in_dl_spec"), "outdir": _get_str("in_dl_outdir"),
        "threads": _get_int("in_dl_threads", 32), "engine": _get_str("cmb_dl_engine"),
        "timeout": _get_int("in_dl_timeout", 30), "retries": _get_int("in_dl_retries", 3),
        "delay": _get_float("in_dl_delay", 0.0), "cookie": _get_str("in_dl_cookie"),
        "overwrite": _get_bool("chk_dl_overwrite"), "config": _get_str("in_dl_config"),
    }
    data["build"] = {
        "txt": _get_str("in_bd_txt"), "out": _get_str("in_bd_out"),
        "aid": _get_str("in_bd_aid"), "engine": _get_str("cmb_bd_engine"),
        "workers": _get_int("in_bd_workers", 3), "delay": _get_float("in_bd_delay", 0.0),
        "rpm": _get_int("in_bd_rpm", 60), "cache": _get_str("in_bd_cache"),
        "jar": _get_str("in_bd_jar"), "cover": _get_str("cmb_bd_cover"),
        "epubcheck": _get_str("in_bd_epubcheck"), "resume": _get_bool("chk_bd_resume"),
        "scanall": _get_bool("chk_bd_scanall"), "nocover": _get_bool("chk_bd_nocover"),
    }
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 回调
# ---------------------------------------------------------------------------
def _on_start_download(sender, app_data):
    if _runner.busy:
        _views["download"].append("WARN", t("msg.busy"))
        return
    spec = _get_str("in_dl_spec").strip()
    if not spec:
        _views["download"].append("WARN", t("msg.spec_required"))
        return
    _runner.start("download")


def _on_start_build(sender, app_data):
    if _runner.busy:
        _views["build"].append("WARN", t("msg.busy"))
        return
    txt = _get_str("in_bd_txt").strip()
    if not txt or not os.path.exists(txt):
        _views["build"].append("WARN", t("msg.txt_required"))
        return
    _runner.start("build")


def _on_cancel(sender, app_data):
    _runner.cancel()


def _on_theme_change(sender, app_data, user_data):
    global _current_theme
    _current_theme = user_data
    apply_theme(user_data)


def _on_lang_change(sender, app_data, user_data):
    set_language(user_data)


def _on_open_appdir(sender, app_data):
    os.startfile(APP_DIR)


def _on_exit(sender, app_data):
    dpg.stop_dearpygui()


def _on_about(sender, app_data):
    if dpg.does_item_exist("win_about"):
        dpg.delete_item("win_about")
    with dpg.window(label=t("about.title"), width=_s(480), height=_s(240), modal=True,
                    no_resize=True, no_move=True, tag="win_about"):
        dpg.add_text(t("about.text"), wrap=_s(440))
        dpg.add_spacer(height=_s(8))
        with dpg.group(horizontal=True):
            dpg.add_spacer(width=_s(400))
            dpg.add_button(label=t("about.close"), width=_s(80),
                           callback=lambda s, a: dpg.delete_item("win_about"))


# ---------------------------------------------------------------------------
# 文件对话框
# ---------------------------------------------------------------------------
def _make_fd(tag, directory_selector, callback):
    dpg.add_file_dialog(
        tag=tag, callback=callback, directory_selector=directory_selector,
        show=False, default_path=os.path.expanduser("~"))


def _fd_set_input(sender, app_data, target_tag):
    path = ""
    if isinstance(app_data, dict):
        path = app_data.get("file_path_name") or ""
    else:
        path = str(app_data)
    if path:
        dpg.set_value(target_tag, path)


# ---------------------------------------------------------------------------
# UI 构建
# ---------------------------------------------------------------------------
@contextmanager
def _param_table():
    with dpg.table(header_row=False, borders_innerH=False, borders_outerH=False,
                   borders_innerV=False, borders_outerV=False, width=-1):
        dpg.add_table_column(width_fixed=True, width=_s(200))
        dpg.add_table_column()
        yield


def _build_ui(settings):
    with dpg.window(tag="main", label=APP_NAME, width=_s(settings["width"]),
                    height=_s(settings["height"]), min_size=(_s(980), _s(620))):
        with dpg.menu_bar():
            with dpg.menu(label=t("menu.file")):
                dpg.add_menu_item(label=t("menu.file.config"),
                                  callback=lambda s, a: dpg.configure_item("fd_dl_config", show=True))
                dpg.add_menu_item(label=t("menu.file.openappdir"), callback=_on_open_appdir)
                dpg.add_separator()
                dpg.add_menu_item(label=t("menu.file.exit"), callback=_on_exit)
            with dpg.menu(label=t("menu.view")):
                with dpg.menu(label=t("menu.view.lang")):
                    dpg.add_menu_item(label=t("menu.view.lang.en"), check=True,
                                      callback=_on_lang_change, user_data="en")
                    dpg.add_menu_item(label=t("menu.view.lang.zh"), callback=_on_lang_change,
                                      user_data="zh")
                with dpg.menu(label=t("menu.view.theme")):
                    dpg.add_menu_item(label=t("menu.view.theme.dark"), check=True,
                                      callback=_on_theme_change, user_data="dark")
                    dpg.add_menu_item(label=t("menu.view.theme.light"), callback=_on_theme_change,
                                      user_data="light")
            with dpg.menu(label=t("menu.help")):
                dpg.add_menu_item(label=t("menu.help.about"), callback=_on_about)

        with dpg.tab_bar():
            # ---------------- Downloader ----------------
            with dpg.tab(label=t("tab.download"), tag="tab_dl"):
                with dpg.child_window(height=-1, border=True):
                    with dpg.collapsing_header(label=t("dl.settings"), default_open=True):
                        with _param_table():
                            with dpg.table_row():
                                dpg.add_text(t("dl.spec"))
                                with dpg.group(horizontal=True):
                                    dpg.add_input_text(tag="in_dl_spec", width=-1)
                            with dpg.table_row():
                                dpg.add_text(t("dl.outdir"))
                                with dpg.group(horizontal=True):
                                    dpg.add_input_text(tag="in_dl_outdir", width=-1)
                                    dpg.add_button(label=t("dl.browse"), callback=lambda s, a: dpg.configure_item("fd_dl_outdir", show=True))
                            with dpg.table_row():
                                dpg.add_text(t("dl.threads"))
                                dpg.add_input_int(tag="in_dl_threads", min_value=1, min_clamped=True, width=-1)
                            with dpg.table_row():
                                dpg.add_text(t("dl.engine"))
                                dpg.add_combo(tag="cmb_dl_engine", items=ENGINE_CHOICES, width=-1)
                            with dpg.table_row():
                                dpg.add_text(t("dl.timeout"))
                                dpg.add_input_int(tag="in_dl_timeout", min_value=1, min_clamped=True, width=-1)
                            with dpg.table_row():
                                dpg.add_text(t("dl.retries"))
                                dpg.add_input_int(tag="in_dl_retries", min_value=0, min_clamped=True, width=-1)
                            with dpg.table_row():
                                dpg.add_text(t("dl.delay"))
                                dpg.add_input_float(tag="in_dl_delay", min_value=0.0, min_clamped=True, width=-1)
                            with dpg.table_row():
                                dpg.add_text(t("dl.cookie"))
                                dpg.add_input_text(tag="in_dl_cookie", width=-1)
                            with dpg.table_row():
                                dpg.add_text(t("dl.config"))
                                dpg.add_input_text(tag="in_dl_config", default_value=DEFAULT_CONFIG_PATH, width=-1)
                            with dpg.table_row():
                                dpg.add_text("")
                                dpg.add_checkbox(tag="chk_dl_overwrite", label=t("dl.overwrite"))
                    with dpg.group(horizontal=True):
                        dpg.add_button(tag="btn_dl_start", label=t("dl.start"), width=_s(120),
                                       height=_s(30), callback=_on_start_download)
                        dpg.add_button(tag="btn_dl_cancel", label=t("dl.cancel"), width=_s(120),
                                       height=_s(30), callback=_on_cancel, enabled=False)
                    dpg.add_progress_bar(tag="pb_dl", default_value=0.0, overlay="",
                                         width=-1, height=_s(24))
                    dpg.add_text(tag="tx_dl_stats", default_value=t("dl.stats"))
                    dpg.add_separator()
                    dpg.add_text(t("dl.logtitle"))
                    with dpg.child_window(tag="dl_log", height=-1, border=False):
                        pass
                    _views["download"] = LogView("dl_log")

            # ---------------- EPUB Builder ----------------
            with dpg.tab(label=t("tab.build"), tag="tab_build"):
                with dpg.child_window(height=-1, border=True):
                    with dpg.collapsing_header(label=t("bd.settings"), default_open=True):
                        with _param_table():
                            with dpg.table_row():
                                dpg.add_text(t("bd.txt"))
                                with dpg.group(horizontal=True):
                                    dpg.add_input_text(tag="in_bd_txt", width=-1)
                                    dpg.add_button(label=t("bd.txt.file"), callback=lambda s, a: dpg.configure_item("fd_bd_txt", show=True))
                                    dpg.add_button(label=t("bd.txt.dir"), callback=lambda s, a: dpg.configure_item("fd_bd_txtdir", show=True))
                            with dpg.table_row():
                                dpg.add_text(t("bd.out"))
                                with dpg.group(horizontal=True):
                                    dpg.add_input_text(tag="in_bd_out", width=-1)
                                    dpg.add_button(label=t("dl.browse"), callback=lambda s, a: dpg.configure_item("fd_bd_out", show=True))
                            with dpg.table_row():
                                dpg.add_text(t("bd.aid"))
                                dpg.add_input_text(tag="in_bd_aid", width=-1)
                            with dpg.table_row():
                                dpg.add_text(t("bd.engine"))
                                dpg.add_combo(tag="cmb_bd_engine", items=ENGINE_CHOICES, width=-1)
                            with dpg.table_row():
                                dpg.add_text(t("bd.workers"))
                                dpg.add_input_int(tag="in_bd_workers", min_value=1, min_clamped=True, width=-1)
                            with dpg.table_row():
                                dpg.add_text(t("bd.delay"))
                                dpg.add_input_float(tag="in_bd_delay", min_value=0.0, min_clamped=True, width=-1)
                            with dpg.table_row():
                                dpg.add_text(t("bd.rpm"))
                                dpg.add_input_int(tag="in_bd_rpm", min_value=1, min_clamped=True, width=-1)
                            with dpg.table_row():
                                dpg.add_text(t("bd.cache"))
                                with dpg.group(horizontal=True):
                                    dpg.add_input_text(tag="in_bd_cache", width=-1)
                                    dpg.add_button(label=t("dl.browse"), callback=lambda s, a: dpg.configure_item("fd_bd_cache", show=True))
                            with dpg.table_row():
                                dpg.add_text(t("bd.jar"))
                                with dpg.group(horizontal=True):
                                    dpg.add_input_text(tag="in_bd_jar", width=-1)
                                    dpg.add_button(label=t("dl.browse"), callback=lambda s, a: dpg.configure_item("fd_bd_jar", show=True))
                            with dpg.table_row():
                                dpg.add_text(t("bd.cover"))
                                dpg.add_combo(tag="cmb_bd_cover", items=COVER_CHOICES, width=-1)
                            with dpg.table_row():
                                dpg.add_text(t("bd.epubcheck"))
                                with dpg.group(horizontal=True):
                                    dpg.add_input_text(tag="in_bd_epubcheck", width=-1)
                                    dpg.add_button(label=t("dl.browse"), callback=lambda s, a: dpg.configure_item("fd_bd_epubcheck", show=True))
                            with dpg.table_row():
                                dpg.add_text("")
                                dpg.add_checkbox(tag="chk_bd_resume", label=t("bd.resume"), default_value=True)
                            with dpg.table_row():
                                dpg.add_text("")
                                dpg.add_checkbox(tag="chk_bd_scanall", label=t("bd.scanall"))
                            with dpg.table_row():
                                dpg.add_text("")
                                dpg.add_checkbox(tag="chk_bd_nocover", label=t("bd.nocover"))
                    with dpg.group(horizontal=True):
                        dpg.add_button(tag="btn_build_start", label=t("bd.start"), width=_s(120),
                                       height=_s(30), callback=_on_start_build)
                        dpg.add_button(tag="btn_build_cancel", label=t("bd.cancel"), width=_s(120),
                                       height=_s(30), callback=_on_cancel, enabled=False)
                    dpg.add_text(t("bd.progress"))
                    dpg.add_progress_bar(tag="pb_build", default_value=0.0, overlay="",
                                         width=-1, height=_s(22))
                    dpg.add_text(t("bd.imgprogress"))
                    dpg.add_progress_bar(tag="pb_build_img", default_value=0.0, overlay="",
                                         width=-1, height=_s(18))
                    dpg.add_separator()
                    dpg.add_text(t("bd.logtitle"))
                    with dpg.child_window(tag="bd_log", height=-1, border=False):
                        pass
                    _views["build"] = LogView("bd_log")

        dpg.add_separator()
        with dpg.group(horizontal=True):
            dpg.add_text(t("status.idle"), tag="tx_status")

    # 文件对话框（保持隐藏，点击时 show）
    _make_fd("fd_dl_config", False, lambda s, a, u: _fd_set_input(s, a, "in_dl_config"))
    _make_fd("fd_dl_outdir", True, lambda s, a, u: _fd_set_input(s, a, "in_dl_outdir"))
    _make_fd("fd_bd_txt", False, lambda s, a, u: _fd_set_input(s, a, "in_bd_txt"))
    _make_fd("fd_bd_txtdir", True, lambda s, a, u: _fd_set_input(s, a, "in_bd_txt"))
    _make_fd("fd_bd_out", True, lambda s, a, u: _fd_set_input(s, a, "in_bd_out"))
    _make_fd("fd_bd_cache", True, lambda s, a, u: _fd_set_input(s, a, "in_bd_cache"))
    _make_fd("fd_bd_jar", True, lambda s, a, u: _fd_set_input(s, a, "in_bd_jar"))
    _make_fd("fd_bd_epubcheck", False, lambda s, a, u: _fd_set_input(s, a, "in_bd_epubcheck"))

    # i18n 注册
    _reg("tab.download", "tab_dl")
    _reg("tab.build", "tab_build")
    for key, tag in (
        ("dl.settings", None), ("bd.settings", None), ("dl.spec", "in_dl_spec"),
        ("dl.outdir", "in_dl_outdir"), ("dl.threads", "in_dl_threads"),
        ("dl.engine", "cmb_dl_engine"), ("dl.timeout", "in_dl_timeout"),
        ("dl.retries", "in_dl_retries"), ("dl.delay", "in_dl_delay"),
        ("dl.cookie", "in_dl_cookie"), ("dl.config", "in_dl_config"),
        ("dl.overwrite", "chk_dl_overwrite"), ("dl.start", "btn_dl_start"),
        ("dl.cancel", "btn_dl_cancel"), ("bd.txt", "in_bd_txt"),
        ("bd.out", "in_bd_out"), ("bd.aid", "in_bd_aid"),
        ("bd.engine", "cmb_bd_engine"), ("bd.workers", "in_bd_workers"),
        ("bd.delay", "in_bd_delay"), ("bd.rpm", "in_bd_rpm"),
        ("bd.cache", "in_bd_cache"), ("bd.jar", "in_bd_jar"),
        ("bd.cover", "cmb_bd_cover"), ("bd.epubcheck", "in_bd_epubcheck"),
        ("bd.resume", "chk_bd_resume"), ("bd.scanall", "chk_bd_scanall"),
        ("bd.nocover", "chk_bd_nocover"), ("bd.start", "btn_build_start"),
        ("bd.cancel", "btn_build_cancel"),
    ):
        if tag:
            _reg(key, tag)

    # 状态栏 text 的 default_value 用语言文本（切换时不改 key 内的文本）
    return


def _restore_settings(settings):
    dl = settings["dl"]
    bd = settings["build"]
    dpg.set_value("in_dl_spec", dl["spec"])
    dpg.set_value("in_dl_outdir", dl["outdir"])
    dpg.set_value("in_dl_threads", dl["threads"])
    dpg.set_value("cmb_dl_engine", dl["engine"])
    dpg.set_value("in_dl_timeout", dl["timeout"])
    dpg.set_value("in_dl_retries", dl["retries"])
    dpg.set_value("in_dl_delay", dl["delay"])
    dpg.set_value("in_dl_cookie", dl["cookie"])
    dpg.set_value("chk_dl_overwrite", dl["overwrite"])
    dpg.set_value("in_dl_config", dl["config"] or DEFAULT_CONFIG_PATH)
    dpg.set_value("in_bd_txt", bd["txt"])
    dpg.set_value("in_bd_out", bd["out"])
    dpg.set_value("in_bd_aid", bd["aid"])
    dpg.set_value("cmb_bd_engine", bd["engine"])
    dpg.set_value("in_bd_workers", bd["workers"])
    dpg.set_value("in_bd_delay", bd["delay"])
    dpg.set_value("in_bd_rpm", bd["rpm"])
    dpg.set_value("in_bd_cache", bd["cache"])
    dpg.set_value("in_bd_jar", bd["jar"])
    dpg.set_value("cmb_bd_cover", bd["cover"])
    dpg.set_value("in_bd_epubcheck", bd["epubcheck"])
    dpg.set_value("chk_bd_resume", bd["resume"])
    dpg.set_value("chk_bd_scanall", bd["scanall"])
    dpg.set_value("chk_bd_nocover", bd["nocover"])


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
_current_theme = "dark"


def main():
    global _current_theme
    _detect_dpi()  # 必须先于任何窗口创建，进程级 DPI 感知只能设置一次
    settings = _load_settings()
    _current_theme = settings.get("theme", "dark")

    dpg.create_context()

    # 字体（中文支持；2x 字号 + 0.5 全局缩放保证清晰，字号随 DPI 放大）
    for path in FONT_CANDIDATES:
        try:
            if os.path.isfile(path):
                with dpg.font_registry():
                    font = dpg.add_font(path, _s(34))
                dpg.bind_font(font)
                dpg.set_global_font_scale(0.5)
                break
        except Exception:
            continue

    _build_themes()
    apply_theme(_current_theme)

    _build_ui(settings)
    _restore_settings(settings)
    set_language(settings.get("lang", "en"))

    # 标题必须为纯 ASCII：DPG 在 Windows 上按 ANSI(GBK) 转换标题，
    # 非 ASCII 字符（如 ——）会被错误解码导致乱码。
    dpg.create_viewport(title="wenku8 GUI - wenku8 toolbox",
                        width=_s(settings["width"]), height=_s(settings["height"]),
                        min_width=_s(980), min_height=_s(620))
    dpg.setup_dearpygui()
    dpg.set_primary_window("main", True)
    dpg.show_viewport()

    smoke = 0
    try:
        smoke = int(os.environ.get("WENKU8_GUI_SMOKE", "0"))
    except ValueError:
        smoke = 0

    frames = 0
    while dpg.is_dearpygui_running():
        _poll_queue()
        dpg.render_dearpygui_frame()
        frames += 1
        if smoke and frames >= smoke:
            dpg.stop_dearpygui()

    _save_settings()
    dpg.destroy_context()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
