# wenku8 工具箱

面向 [wenku8](https://www.wenku8.cc/)（轻小说文库）的实用工具集合：两个浏览器用户脚本，一个批量下载轻小说 txt 的 Python 脚本，以及一个把本地下载的 txt 生成带插图 EPUB 的 Python 脚本。

## 目录内容

- `js插件/` — 两个相互独立的 Greasyfork 用户脚本（不同作者，代码刻意不共享）
  - `轻小说文库+-2.31.2.js` —「轻小说文库+」by PY-DNG。全方位的站点体验改善：阅读、下载、书架、推荐、书评、账号、页面个性化等。
  - `轻小说文库下载-2.2.6.js` —「轻小说文库下载」by HaoaW。生成分卷与全本 ePub、插图拖放排版，以及部分小说的在线阅读。
- `download_novel.py` — **wenku8 轻小说 txt 批量下载器**：模拟浏览器请求头、默认 8 线程 + 限速（40 请求/分钟）、失败自动重试（默认 3 次）、404 自动跳过、Cloudflare 引擎兜底。
- `config.ini` — 首次运行 `download_novel.py` 自动生成，可调整线程数、重试次数等。
- `build_epub.py` — **本地 txt → 带插图 EPUB 生成器**（本仓库的核心工具）。
- `requirements.txt` — `build_epub.py` 的 Python 依赖。
- `2163.txt` — 已下载的 txt 样本（aid 为 `2163`）。
- `2163_images/` — `build_epub.py` 生成的图片缓存（重跑时复用）。
- `AGENTS.md` — 面向开发者/agent 的仓库约定。

## 安装用户脚本

在 Greasyfork 打开脚本页面，用 Tampermonkey / Violentmonkey 一键安装：

- 轻小说文库+：<https://greasyfork.org/scripts/539514>
- 轻小说文库下载：<https://greasyfork.org/scripts/407369>

两者运行于 `wenku8.com` / `wenku8.net` / `wenku8.cc` 页面。它们出自不同作者、彼此独立——不要期望二者功能或代码互通。

## download_novel.py

从 `dl2.wenku8.com` 批量下载轻小说 txt 的独立脚本：

- **链接规则**：`https://dl2.wenku8.com/txtutf8/{id // 1000}/{id}.txt`——第 1 本 → `0/1.txt`，第 2676 本 → `2/2676.txt`。
- **模拟浏览器请求头**（Chrome UA + Referer），绕过基础反爬。
- **多线程下载**：默认 8 线程 + 全局限速（默认 40 请求/分钟），启动时打印本机 CPU 核心数。
- **失败自动重试**：默认 3 次（指数退避），重试耗尽的书计入失败清单。
- **限速保护**：被限速（HTTP 429）时尊重 `Retry-After` 并触发全局冷却，避免多线程惊群，重试可自动恢复。
- **404 自动跳过**：链接确实不存在的书直接跳过，不重试。
- **Cloudflare 兜底**：检测到 CF 挑战时自动在 `curl_cffi` → `requests` → `cloudscraper` 间升级引擎（`dl2` txt 端点实测不拦非浏览器客户端）。
- **断点续下**：已存在的文件默认跳过，`--overwrite` 强制重下。
- **原子写盘**：先写临时文件再改名，避免半截文件。
- 输出到脚本同级的 `txt/` 目录。

### 配置 config.ini

首次运行自动生成（位于脚本同级目录）：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `threads` | `8` | 下载线程数 |
| `engine` | `auto` | `auto` / `curl_cffi` / `requests` / `cloudscraper` |
| `output_dir` | `txt` | 输出文件夹（相对脚本目录） |
| `timeout` | `120` | 单请求超时（秒） |
| `retries` | `3` | 下载失败重试次数 |
| `req_per_min` | `40` | 全局限速：每分钟请求数上限 |
| `request_delay` | `0` | 请求间附加延时（秒），被限速时可调大 |
| `cookie` | （空） | 被 Cloudflare 拦截时填浏览器解出的 `cf_clearance` 等 cookie |

### 用法

```bash
python download_novel.py -d 1             # 下载单本
python download_novel.py -d 1-1000        # 下载范围
python download_novel.py -d 5,68          # 下载指定多本
python download_novel.py -d 1-10,20,30-35 # 混合：范围 + 指定
python download_novel.py -d 1-5000 -c my.ini --overwrite
```

下载完成后，`txt/<aid>.txt` 可直接交给 `build_epub.py` 批量生成 EPUB：

```bash
python build_epub.py --txt I:\wenku8\txt
```

## build_epub.py

把本地已下载的 wenku8 txt 生成**符合 Sigil 规范的 EPUB3**：

- 解析 txt（自动探测编码：UTF-8 → GB18030 → Big5），并依据 API 的「卷名 章节名」标题行切分为卷/章。
- 通过中继 API（`wenku8-relay.mewx.org`，采用加密的 Android 请求格式）拉取目录与章节。
- 识别「插图」章节（标题启发式 `/插图|插畫|img|写真/`，或用 `--scan-all` 全量扫描），从正文提取 `<!--image-->` 图片 URL。
- 以**模拟浏览器请求头**下载全部插图，绕过站点的防盗链 / Cloudflare 防护。
- 在插图章节位置嵌入图片；**封面默认取第一卷「插图」章节的第一张图**（全书无插图时回退到 API 缩略图）。
- 内置样式表实现排版：卷名/章节名居中、正文段落首行缩进 2 字符。
- 写盘前自动自校验（XML 合法性、manifest/spine/nav 引用、锚点、图片魔数），也可选调用官方 **epubcheck**。

### 安装

```bash
pip install -r requirements.txt
```

### 用法

单文件：

```bash
python build_epub.py --txt I:\wenku8\2163.txt
```

目录批量（处理其中每个 `<aid>.txt`）：

```bash
python build_epub.py --txt I:\wenku8 --out epub_out
```

输出名为 `<书名> - <aid>.epub`。默认 `--cover auto`：封面为第一卷插图章首图；若第一卷没有插图，则改用 API 缩略图（`action=book&do=cover`）。

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--txt` | *（必填）* | txt 文件或目录（目录即批量处理其中所有 `\d+.txt`） |
| `--out` | 当前目录 | EPUB 输出目录 |
| `--aid` | 从文件名解析 | 强制指定文库书籍 id（默认解析 `<数字>.txt`） |
| `--engine` | `auto` | HTTP 引擎：`auto` / `curl_cffi` / `requests` / `cloudscraper`（见下文反爬说明） |
| `--workers` | `3` | 图片下载并发数 |
| `--delay` | `0.0` | 请求间附加延时（秒） |
| `--req-per-min` | `60` | 限速：每分钟请求上限 |
| `--cache` | `<txt 目录>/<aid>_images` | 图片缓存目录 |
| `--cookie-jar` | – | 会话 Cookie 持久化到 JSON 文件 |
| `--no-resume` | 关闭 | 关闭断点续跑（默认复用已下载图片） |
| `--scan-all` | 关闭 | 扫描所有章节提取图片（不限于标题含「插图」的章节） |
| `--cover` | `auto` | `auto` = 第一卷插图章首图（无则回退 API 缩略图）；`api` = 始终 API 缩略图 |
| `--no-cover` | 关闭 | 不设置封面 |
| `--epubcheck` | – | `epubcheck.jar` 路径；构建后运行官方校验器 |

### 反爬与礼貌请求

- **引擎降级链**：`curl_cffi`（拟真 Chrome 的 TLS/HTTP2 指纹）→ `requests` → `cloudscraper`（可解 JS 验证）。检测到 Cloudflare 挑战时自动升级到下一档引擎。
- **加密 API body**（`appver`/`request`/`timetoken`）+ Android UA，符合 app 接口要求。
- **限速**：令牌桶每分钟上限、指数退避重试、尊重 `Retry-After`、会话内 Cookie 复用。
- **磁盘缓存 + 断点续跑**：图片 URL 不可变，重跑不会重复请求（实测样本第二次运行 0 重复下载）。

### EPUB 合规

生成的 EPUB3 通过 **epubcheck 5.3.0：0 errors / 0 warnings**（样本实测）。包含 `nav.xhtml` + `toc.ncx`、合规的 `cover-image` 清单项、可达的封面页（无「Guide」区）、文本转义与 ASCII 安全路径。

如需自行运行官方校验，把 `--epubcheck` 指向 `epubcheck.jar`。若网络直连 GitHub 被墙，可从 npm 获取：

```bash
npm pack epubcheck-static   # 解压 epubcheck-static-*.tgz；jar 在 package/vendor/epubcheck.jar
java -jar <epubcheck.jar 路径> <书>.epub
```

### 注意事项

- txt 文件名必须包含书籍 id（如 `2163.txt`），否则请用 `--aid` 指定。
- 「插图」章节通常是每卷末尾的空占位章节，图片就嵌入在它的位置。
- 在 Cloudflare 限速下，请保持默认的 `--workers`/`--req-per-min`（或加大 `--delay`），不要高频打端点。

## GUI（wenku8_gui.py）

基于 Dear PyGui 的桌面 GUI，在一个窗口内整合 `download_novel.py` 与 `build_epub.py`：

```bash
pip install dearpygui
python wenku8_gui.py
```

- 两个页签：**下载器**（范围、线程/引擎/限速/超时、输出目录、cookie、`--overwrite`）与 **EPUB 生成**（txt 文件/文件夹、输出目录、并发数、限速、封面、epubcheck、断点续跑/全量扫描等开关）。
- 界面可切换**中英文**（默认英文）与**暗黑/明亮**主题（均在「视图」菜单），自动从系统加载中文字体。
- **实时进度条**（书级 + 图片级）与着色日志。设置保存在脚本同目录的 `gui_settings.json`。
- 两个脚本在 GUI **内部**运行（非子进程）；同一时刻只允许一个任务。

打包为独立 exe（仅打包需要 PyInstaller）：

```bash
pyinstaller --noconfirm --clean --onefile --windowed --name wenku8-gui `
  --collect-all curl_cffi --collect-all cloudscraper --collect-all ebooklib `
  --distpath release --workpath build --specpath build wenku8_gui.py
```

产物为自包含的 `release/wenku8-gui.exe`，两个脚本与全部依赖已内嵌。修改 `build_epub.py`/`download_novel.py` 后需重新打包才会生效。

## 许可证

两个用户脚本各自带有许可证（见各自的 `// @license` 头：轻小说文库+ 为 GPL-3.0-or-later）。本 README 与 `build_epub.py` 按现状提供，仅供个人使用。
