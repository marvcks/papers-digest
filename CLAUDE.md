# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这个仓库里有两套互不相干的系统

改任何代码前先确认自己在哪一套里，它们的依赖和数据流完全不重叠：

| | 论文速览（主线） | Miniflux 系（旧） |
|---|---|---|
| 入口 | `papers_digest.py` / `papers_server.py` | `rss_news_translator.py` / `journal_search.py` / `journal_weekly_interest_summary.py` / `blogs_yesterday_summary.py` |
| 数据源 | Crossref + OpenAlex（直连，不需要任何服务） | Miniflux `http://localhost:8080`（Docker） |
| 产物 | `papers-digest.html`（自包含网页） | `*.md` 摘要文件 |
| 现状 | 在用 | **Miniflux 容器已不在本机，跑不了**；要用得先重新 `docker compose up` |

Miniflux 的 docker-compose 配置和 API 用法见 `dev_files/miniflux-api.md`。

## 论文速览：架构

数据流跨 4 个文件，按顺序读才能看懂：

```
journals.json                     期刊清单（刊名 + ISSN）
      │
      ▼  collect_articles()       Crossref /journals/{issn}/works，游标分页
      │                          拿标题、作者、卷期页码、日期、摘要
      ▼  translate_all()          ecnu-plus 只翻新标题，按 DOI 缓存
      │
      ▼  merge_archive()          按 DOI 合并完整历史，不随日期窗口删除
      │
      ▼  render_html()            papers_viewer.template.html 填数据 → HTML
      │
papers-digest.html ──────────────► 双击直接看（数据内联）
      │
papers_server.py ────────────────► 同一个文件的本地服务版：
                                    /api/translate  点击「显示摘要」后按需翻译
                                                    缺摘要时才用 OpenAlex 补
                                    /api/mark       想读标记 → papers-marks.json
                                    /api/reading    阅读记录 → papers-reading.json
                                    /api/update     POST 手动更新，GET 查询进度
                                    /api/health     页面、记录与任务状态
```

关键点：

- **想读和阅读状态都有服务端与浏览器副本**。想读存 `papers-marks.json` + localStorage，服务端与本地加载时取并集；阅读状态存 `papers-reading.json` + localStorage `papers-digest-reading`，按 DOI / `updatedAt` 合并。静态页内联记录只作初始值，不能覆盖浏览器已经保存的取消收藏或更新状态。
- 阅读状态 `unread` / `browsed` / `full` 对应未浏览 / 已浏览 / 已读全文；`full` 同时算已浏览。只有明确点击才改状态，滚动和展开摘要不自动标记，批量已浏览不能降级已读全文。「已浏览」按钮在标题旁，点击区域不小于 44px，保留按天和勾选批量操作。
- **Zotero 功能已移除**。用户从 DOI 页面用 Chrome 插件导入；旧 `papers-zotero.json` 仅保留为历史记录，不再读写，也没有 Zotero API 或筛选器。
- `papers-archive.json` 保存完整原文和译文历史，首次可从每日 HTML 恢复，之后按 DOI 增量合并；`first_seen_at` 必须保留首次收录时间。首页默认近 30 天，新增、阅读状态和想读视图默认全部历史，日期 / 期刊 / 关键词可组合筛选。新增比较首次收录时间与上次访问时间，首次使用展示上次成功更新之后收录的内容。
- `papers-update-status.json` 记录更新结果。全部抓取失败或零条结果必须失败并保留旧页面；部分失败保留历史并标出失败期刊。页面、历史和状态原子写入，更新与按需翻译共用历史文件锁，防止互相覆盖。

- **`papers_viewer.template.html` 是纯模板，不含数据**。改样式、改交互都改它；`papers_digest.py` 只负责把 JSON 塞进 `__DATA__` 占位符。页面里的 `<script id="article-data">` 就是全部数据。
- **数据必须内联在 HTML 里**，不能拆成旁挂的 .json —— 否则 `file://` 打开时会被 CORS 拦住。代价是文件 3.5MB，所以卡片用 IntersectionObserver 分批渲染（`CHUNK = 60`），**别改回一次性全渲染**。
- **更新只翻标题，批量和单篇标题 prompt 都不能包含摘要**。标题翻译是「批量优先 + 单篇兜底」（`--batch-size` 默认 10），按 `【N】` 编号切块，prompt 必须要求原样回显；漏项走单篇补齐，调用失败降级英文且不中断整批。
- **摘要默认收起，点击「显示摘要」才按需翻译**。已有译文直接复用；Crossref 缺摘要时才在查看时用 OpenAlex 补齐。按需翻译失败保留英文并允许重试；静态模式可读已有内容，实时翻译和更新需要本地服务。
- 译文按 DOI 缓存在 `.cache_papers/`（已 gitignore）。`CACHE_SCHEMA = 3` 分开保存 `title_hash` / `abstract_hash`，兼容 schema 2 完整原文指纹；缓存写入使用文件锁和原子替换，标题和摘要不能覆盖对方译文，原文变化不能复用过期译文。改缓存结构要同步升 `CACHE_SCHEMA`。
- 模板 JS 改完必须抽出脚本执行 `node --check`，避免一个语法错误让页面白屏。
- 页面语言开关靠 body 上的 `lang-both` / `lang-zh` / `lang-en` 类控制 CSS 显隐。**`title-fallback` / `abs-fallback` 是「没有中文译文」的条目用的类名**，承载的是英文原文，在任何语言模式下都不能被隐藏 —— 改了类名要同步改 CSS 规则。
- **暗色模式**：三档主题切换（自动 / 浅色 / 深色），选择存 localStorage `papers-digest-theme`。`<head>` 末尾有一段内联 JS 在页面渲染前就把 `theme-*` 类挂到 `<html>`，避免首屏闪一下亮色再切暗色（FOUC）。CSS 走三套调色板：`:root`（浅色默认）、`html.theme-dark`（显式锁暗色）、`@media (prefers-color-scheme: dark) html.theme-auto`（自动模式跟系统）。**改暗色色值要三处同步**。`theme-light` 不带 `.theme-auto`，所以即便系统是暗色也吃不到 `@media` 覆盖，保持浅色。

## 论文速览：命令

```bash
python papers_digest.py --days 30              # 抓取 + 只翻新标题 + 合并历史 + 生成页面
python papers_digest.py --days 30 --no-llm     # 只抓不翻，快速验证抓取层
python papers_digest.py --days 7 --limit 20    # 小样本试跑，别动全量缓存
python papers_server.py                        # 起本地服务（默认 8770，自动开浏览器）

.venv/bin/python -m unittest test_papers_digest -v  # 临时目录、模拟接口，无外部调用
```

日常更新由页面「更新文献」按钮显式触发，后台同时只能有一个更新任务，页面展示进度；成功后重载并保留筛选条件与本次新增范围。

原每天 07:10 的 `com.xhxu.papers-digest` 已停用，配置保留为
`~/Library/LaunchAgents/com.xhxu.papers-digest.plist.disabled`，不要自行重新启用。
`~/Library/LaunchAgents/com.xhxu.papers-server.plist` 随登录启动本地服务（127.0.0.1:8770）；**启动服务和打开页面都不得自动抓取或调用模型**。
服务日志在 `~/Library/Logs/papers-server.log` 与 `papers-server.err.log`。launchd 不继承 shell 环境，所以使用绝对路径，`.env` 从脚本目录读。

## 踩过的坑（改抓取逻辑前务必知道）

- **RSC 期刊（ChemSci / PCCP）的 `published-online` 只精确到年**，用 `from-online-pub-date` 过滤会一篇都抓不到。所以走 `from-created-date` 拉一个更宽的窗口（`days + 30`），再按 `best_date()` 二次过滤。
- **Crossref 的摘要是 JATS XML**（`<jats:title>Abstract</jats:title>`），要过 `clean_abstract()`，否则页面上会留下 "Abstract" 前缀。
- Crossref 标题里常嵌换行，必须 `normalize_title()` 压平。
- 期刊扉页/目录（"Issue Publication Information" 之类）会被 `SKIP_TITLES` 滤掉，返回空 dict，调用方要判空。
- **ECNU 接口对每个模型限并发 3**，CLI 的 `--workers` 默认 3，别调高。页面后台更新使用 2 workers，按需翻译串行，合计不超过 3；不要同时另起 CLI 翻译任务导致超限。

## 配置

`.env`（gitignored）里有两组 LLM 配置，论文速览只用 `LLM_*`：

- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` — 华东师大 ECNU 的 OpenAI 兼容接口，默认 `ecnu-plus`
- `CROSSREF_MAILTO` — Crossref polite pool 要的邮箱
- `OPENAI_*` / `MODEL_NAME` — 旧中转，论文速览不用

## 其他约定

- `AGENTS.md` 是仓库自己的约定文件（输出命名 `<topic>-YYYY-MM-DD.*`、提交规范、论文速览的若干设计决定）。**改论文速览时两份都要更新**，别只改一处。
- 依赖只有 `requests` / `beautifulsoup4` / `python-dotenv`（见 `pyproject.toml`）。`papers_server.py` 的 HTTP 服务使用标准库，导入 `papers_digest.py` 复用抓取与翻译功能。加依赖前先想想能不能不加。
- `papers-marks.json`、`papers-reading.json`、`papers-archive.json` 是用户数据；它们和更新状态、缓存、锁文件都已 gitignore，不可为测试清空或覆盖真实记录。
- `test_papers_digest.py` 回归检查历史保留、失败更新保护、阅读状态合并、标题与摘要缓存、按需翻译、手动更新去重和本地请求来源限制；测试使用临时目录和模拟接口，不调用真实模型。
