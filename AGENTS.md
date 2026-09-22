# Repository Guidelines

## Project Structure & Module Organization
- 论文速览（与 Miniflux 无关，直接走 Crossref/OpenAlex）：
  - `papers_digest.py` — 抓取 + 翻译 + 渲染，产出 `papers-digest.html`（自包含、可双击打开）
  - `papers_server.py` — 本地服务（按需翻译 / 想读标记 / 存入 Zotero），仅标准库
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
- 生成论文速览页面: `python papers_digest.py --days 30`（`--no-llm` 只抓不翻，`--limit N` 小样本试跑）。
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
- 翻译缓存按 DOI 存在 `.cache_papers/`（已 gitignore），所以重跑只翻新增的；改缓存结构要同步升 `CACHE_SCHEMA`。
- 翻译走**批量优先 + 单篇兜底**（`--batch-size` 默认 10）：一次请求翻多篇，省请求数、摊薄系统提示；
  模型偶尔会漏段或合并编号，漏掉的那几篇再用单篇请求补，保证不会有条目静默地留在英文。
  批量返回按 `【N】` 编号切块（`split_batch_translation`），所以 prompt 里必须强调编号原样回显。
- 单篇翻译失败必须降级成英文、不能中断整批 —— 这是定时任务无人值守的前提。
- 页面数据内联在 `<script id="article-data">` 里，为的是 `file://` 直接双击也能看；
  代价是文件大，所以卡片要分批渲染（IntersectionObserver），别改回一次性全渲染。
- 「想读」标记同时写 `papers-marks.json`（服务端）和 localStorage（静态模式），加载时取并集。
- 「已加入 Zotero」的记录在 `papers-zotero.json`，由 `papers_server.py` 在入库成功后追加。
  `papers_digest.py` 渲染时把它读出来、烤成每条数据的 `inZotero` 字段，
  这样不开服务直接双击静态文件时，「已加入 Zotero」筛选器照样能用。
- 页面顶部的视图筛选（全部 / 已收藏 / 已加入 Zotero）就是 `state.view`，对应 `matches()` 里的两个判断。
  改筛选逻辑要同时看 `rebuild()` 和 `applyZoteroResult()` —— 后者在入库后会就地刷卡片而不是整页重排。
- 模板里的 JS 改完**务必做一次语法检查**（`node --check`），一个引号打错整个页面会白屏且没有明显报错。

## Coding Style & Naming Conventions
- Python, 4-space indentation, PEP 8 naming (`snake_case` for functions/vars).
- Keep CLI flags descriptive (`--hours`, `--limit`, `--preset`).
- Prefer small, pure helpers (e.g., `strip_html`, `format_output`).

## Testing Guidelines
- No automated test suite is present yet.
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
