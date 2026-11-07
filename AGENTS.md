# Repository Guidelines

## Project Structure & Module Organization
- 论文速览（与 Miniflux 无关，直接走 Crossref/OpenAlex）：
  - `papers_digest.py` — 抓取 + 翻译 + 渲染，产出 `papers-digest.html`（自包含、可双击打开）
  - `papers_server.py` — 本地服务（手动更新 / 按需摘要翻译 / 想读与阅读记录），HTTP 服务仅标准库
  - `journals.json` — 期刊列表（刊名 + ISSN），加刊改这里
  - `papers_viewer.template.html` — 页面模板，改样式改这里
- Root Python scripts: `rss_news_translator.py` (RSS fetch + translate) and `journal_search.py` (keyword search + translate).
- Entrypoint stub: `main.py` (hello-world placeholder).
- Generated outputs: `rss-news-YYYY-MM-DD.md`, `journal-papers-YYYY-MM-DD.md`, `papers-digest-YYYY-MM-DD.html`.
- Configuration/assets: `subscriptions.xml`, `recommended-feeds.md`, and `dev_files/` (reference docs).
- Python packaging: `pyproject.toml`, `uv.lock`.

## Build, Test, and Development Commands
- Install deps (uv): `uv sync` (uses `pyproject.toml`/`uv.lock`).
- Run RSS translator: `python rss_news_translator.py --hours 24 --limit 10 --print`.
- Run journal search: `python journal_search.py --preset ml-potential --days 7`.
- 生成论文速览页面: `python papers_digest.py --days 30`（默认只翻新标题；`--no-llm` 只抓不翻，`--limit N` 小样本试跑）。
- 起本地服务: `python papers_server.py`（默认 8770，自动开浏览器）。
- Optional script entrypoints (if installed): `rss-news` and `journal-search`.

## Configuration & Secrets
- Create a `.env` file with:
  - `MINIFLUX_URL`, `MINIFLUX_USERNAME`, `MINIFLUX_PASSWORD`
  - `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `MODEL_NAME`（旧的中转，论文速览不用它）
  - 论文速览用 `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`（默认指向华东师大 ECNU 的 OpenAI 兼容接口），
    以及 `CROSSREF_MAILTO`（Crossref 的 polite pool 要一个邮箱）
- Default Miniflux credentials are assumed to be local; update for your environment.
- ECNU 接口对**每个模型限并发 3**，所以 `papers_digest.py` 的 `--workers` 默认 3，别调高。

## 论文速览的约定
- 分类（journal）、日期、DOI、摘要都来自 Crossref；Crossref 没给摘要的用 OpenAlex 兜底。
  RSC 期刊的 `published-online` 只精确到年，所以窗口过滤走 `from-created-date` 再按 `best_date` 二次筛。
- 更新时只翻译标题，任何批量和单篇标题请求都不能包含摘要。摘要默认收起，点击「显示摘要」才在本地服务翻译；已有摘要译文直接显示，不重复调用模型。Crossref 缺摘要时，在用户查看时用 OpenAlex 补齐。
- 翻译缓存按 DOI 存在 `.cache_papers/`（已 gitignore），schema 3 分开保存 `title_hash` / `abstract_hash`；兼容读取 schema 2 的完整原文指纹。标题和摘要写缓存时用文件锁、原子替换，不能互相覆盖另一种内容的译文。改缓存结构要同步升 `CACHE_SCHEMA`。
- 标题翻译走**批量优先 + 单篇兜底**（`--batch-size` 默认 10）：一次请求翻多篇标题，省请求数、摊薄系统提示；
  模型偶尔会漏段或合并编号，漏掉的那几篇再用单篇请求补，保证标题没有漏翻；调用失败仍降级英文。
  批量返回按 `【N】` 编号切块（`split_batch_translation`），所以 prompt 里必须强调编号原样回显。
- 单篇标题翻译失败必须降级成英文、不能中断整批；按需摘要翻译失败时保留英文原文并允许重试。
- 页面数据内联在 `<script id="article-data">` 里，为的是 `file://` 直接双击也能看；
  代价是文件大，所以卡片要分批渲染（IntersectionObserver），别改回一次性全渲染。
- 「想读」标记同时写 `papers-marks.json`（服务端）和 localStorage（静态模式），加载时取并集。
- 论文完整原文/译文历史在 `papers-archive.json`；首次从每日 HTML 恢复，之后按 DOI 增量合并，旧论文不能随窗口过期删除。`first_seen_at` 是首次收录时间，不能每次重跑重置。
- `papers-update-status.json` 记录最近更新结果。全部抓取失败/零条结果必须返回失败且保留旧页面；部分失败保留历史数据并标出失败期刊。页面、历史库和状态文件使用原子替换写入。
- 阅读状态在 `papers-reading.json` 与 localStorage `papers-digest-reading`，按 DOI/`updatedAt` 合并。`unread` / `browsed` / `full` 分别为未浏览/已浏览/已读全文，`full` 同时算已浏览。只有明确点击才改状态，批量已浏览不能降级已读全文。
- 首页默认近 30 天；新增、阅读状态、想读视图默认查询全部历史，日期/期刊/关键词仍可组合筛选。新增以首次收录时间和上次访问时间比较，首次使用展示上次成功更新之后收录的内容。静态页内联阅读记录与想读记录作初始值，不可覆盖浏览器已经保存的取消收藏/更新状态。
- 「已浏览」按钮位于标题旁，点击区域不小于 44px；保留按天/勾选的批量操作，不能把滚动或展开摘要自动算成已浏览。
- 页面 Zotero 功能已经移除；用户从 DOI 页面使用 Chrome 插件导入。旧 `papers-zotero.json` 仅作为历史记录保留，不再读写。
- 独立 CLI `papers_to_zotero.py` 通过 Zotero 本地 HTTP 接口核验收藏和 PDF；默认只检查，`--import` 才导入缺少的条目，不恢复页面 Zotero 功能、不读写旧 `papers-zotero.json`。已有 DOI 不重复创建；已有条目缺 PDF、验证码/权限失败必须单独报告。回归：`.venv/bin/python -m unittest test_papers_to_zotero -v`。
- 更新由页面「更新文献」按钮显式触发：`POST /api/update` 启动后台任务，`GET /api/update` 查询进度；同一时间只能有一个任务。后台标题更新使用 2 workers，按需翻译串行，合计不超过 ECNU 单模型并发 3。更新成功重载页面时保留筛选条件与本次新增范围。
- 「选择日期」支持整月或自定义起止日（含两个端点）；选择仅筛选历史，点击「更新文献」才抓取。API 接受 `startDate` / `endDate`，CLI 对应 `--start-date` / `--end-date`；缺省仍抓近 30 天。历史范围查询合并 created 窗口和发布日期查询，再按 `best_date` 筛选、按 DOI 去重；结果增量入历史库，更新后保留日期与其他筛选。日期必须完整、有序且不晚于今天。
- macOS 每天 07:10 的 `com.xhxu.papers-digest` 已停用，plist 保留为 `.plist.disabled`。本地阅读服务可随登录启动，但服务启动/打开页面都不得自动抓取论文或调用模型。
- 模板里的 JS 改完**务必做一次语法检查**（`node --check`），一个引号打错整个页面会白屏且没有明显报错。
- 暗色模式：三档主题（自动 / 浅色 / 深色），存 localStorage `papers-digest-theme`；CSS 走三套调色板（`:root` 默认浅色、`html.theme-dark` 显式暗色、`@media (prefers-color-scheme: dark) html.theme-auto` 跟系统），**改暗色色值要三处同步**。`<head>` 末尾的内联 JS 在渲染前挂 `theme-*` 类以避免 FOUC。

## 新闻速览
- `news_sources.json` 保存已启用新闻源；用户已授权在首批 8 源上继续扩充，新增前实际获取并解析，检查新鲜度和实质摘要。`news_digest.py` 直接抓 RSS，独立历史库 `news-archive.json`，独立模板 `news_viewer.template.html`。界面与进度中的来源数从配置计算。
- 页面与论文通过“论文 / 新闻”入口切换；不把新闻混入论文列表、DOI 历史、阅读记录或翻译缓存。
- `POST /api/news/update` 显式启动单个后台任务，`GET /api/news/update` 只查询进度。服务启动、打开页面均不抓取、不调用模型。
- 英文标题和源摘要一起翻译、按各自原文是否变化复用译文；失败保留英文并在下次更新重试。IT之家等正文型源截取正文并明确标注，不冒充来源摘要。
- 新闻模型请求与论文按需翻译共用 `papers_server._translate_lock` 的一个通道，加论文后台 2 workers，总并发不超过 3。
- 部分源失败保留历史；全部失败保留原页面与历史库。新闻历史、状态与页面原子写入。
- 回归：`.venv/bin/python -m unittest test_news_digest test_papers_digest -v`；两份模板的内联 JS 都要 `node --check`。

## Coding Style & Naming Conventions
- Python, 4-space indentation, PEP 8 naming (`snake_case` for functions/vars).
- Keep CLI flags descriptive (`--hours`, `--limit`, `--preset`).
- Prefer small, pure helpers (e.g., `strip_html`, `format_output`).

## Testing Guidelines
- 论文历史、阅读记录、标题/摘要缓存与手动更新回归检查：`.venv/bin/python -m unittest test_papers_digest -v`（使用临时目录和模拟抓取，不调用外部接口）。
- When adding tests, match module names (e.g., `test_rss_news_translator.py`) and document how to run them.

## Commit & Pull Request Guidelines
- No commit history exists; establish a simple convention such as:
  - `feat:`, `fix:`, `docs:`, `chore:`
- PRs should include: summary, sample output file name (if applicable), and any config changes.

## Agent-Specific Notes
- Avoid committing secrets; `.env` should stay local.
- Output files are content artifacts; keep them if needed for history, otherwise clean before release.
- For Journal weekly interest digests, prefer `journal_weekly_interest_summary.py` over `journal_search.py`.
- Default Journal digest scope: last 7 days in `Journal` category.
- Use interest profile `user-interest` and grouped output by theme:
  - `分子设计`
  - `分子性质优化`
  - `电子结构+神经网络`
- Preferred command for this repo task:
  - `python journal_weekly_interest_summary.py --category Journal --preset all --profile user-interest --days 7 --group-by-theme --output journal-interest-themed-YYYY-MM-DD.md`
- Output format requirement for each entry:
  - `标题 + 链接 + 中文概括`
- Unless explicitly requested otherwise, do not provide run instructions to the user; execute and deliver file paths/results directly.

## Prior Open Questions (Resolved)
- Miniflux service availability and URL (default `http://localhost:8080`).
- Target category name for batch feed import (`RSS-Feeds`).
- Scope of import (all feeds vs. a subset).
