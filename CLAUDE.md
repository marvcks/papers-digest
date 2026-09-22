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
      ▼  fill_missing_abstracts() Crossref 没给摘要的，用 OpenAlex 补
      │
      ▼  translate_all()          ecnu-plus 翻译标题+摘要，按 DOI 缓存
      │
      ▼  render_html()            papers_viewer.template.html 填数据 → HTML
      │
papers-digest.html ──────────────► 双击直接看（数据内联）
      │
papers_server.py ────────────────► 同一个文件的本地服务版，多三个 API：
                                    /api/translate  历史/漏翻的按需翻译
                                    /api/mark       想读标记 → papers-marks.json
                                    /api/zotero     写进正在运行的 Zotero
                                                    → papers-zotero.json（记下已入库的 DOI）
```

关键点：

- **两个筛选状态都存在两个地方**：想读（`papers-marks.json` + localStorage）和「已加入 Zotero」
  （`papers-zotero.json` + 生成页面时烤进数据的 `inZotero` 字段 + localStorage）。
  加载时取并集。Zotero 那个额外烤进 HTML，是为了不开服务、直接双击静态文件时筛选器也能用
  —— 因为**只有服务在时才能入库**，光靠 localStorage 在 `file://` 下会丢。

- **`papers_viewer.template.html` 是纯模板，不含数据**。改样式、改交互都改它；`papers_digest.py` 只负责把 JSON 塞进 `__DATA__` 占位符。页面里的 `<script id="article-data">` 就是全部数据。
- **数据必须内联在 HTML 里**，不能拆成旁挂的 .json —— 否则 `file://` 打开时会被 CORS 拦住。代价是文件 3.5MB，所以卡片用 IntersectionObserver 分批渲染（`CHUNK = 60`），**别改回一次性全渲染**。
- **翻译是「批量优先 + 单篇兜底」**（`BATCH_SIZE = 10`）。一次请求翻 10 篇省请求数（1390 篇 → 139 次）；模型偶尔会漏段或合并编号（实测 136 批漏了 7 篇），漏掉的那几篇走单篇请求补。**去掉兜底会让条目静默留在英文**。
- 译文按 DOI 缓存在 `.cache_papers/`（已 gitignore），重跑只翻新增的。改缓存结构要同步升 `CACHE_SCHEMA`。
- 页面语言开关靠 body 上的 `lang-both` / `lang-zh` / `lang-en` 类控制 CSS 显隐。**`title-fallback` / `abs-fallback` 是「没有中文译文」的条目用的类名**，承载的是英文原文，在任何语言模式下都不能被隐藏 —— 改了类名要同步改 CSS 规则。

## 论文速览：命令

```bash
python papers_digest.py --days 30              # 抓取 + 翻译 + 生成页面（日常全流程）
python papers_digest.py --days 30 --no-llm     # 只抓不翻，快速验证抓取层
python papers_digest.py --days 7 --limit 20    # 小样本试跑，别动全量缓存
python papers_server.py                        # 起本地服务（默认 8770，自动开浏览器）

launchctl kickstart -k gui/$(id -u)/com.xhxu.papers-digest   # 手动触发定时任务
tail -f ~/Library/Logs/papers-digest.log                     # 看定时任务日志
```

launchd 每天 07:10 自动跑一次（选在 ECNU 峰时 8:00 之前按谷时计价），plist 在
`~/Library/LaunchAgents/com.xhxu.papers-digest.plist`。它**不继承 shell 环境**，所以脚本内部的路径全是绝对的、`.env` 从脚本所在目录读。

## 踩过的坑（改抓取逻辑前务必知道）

- **RSC 期刊（ChemSci / PCCP）的 `published-online` 只精确到年**，用 `from-online-pub-date` 过滤会一篇都抓不到。所以走 `from-created-date` 拉一个更宽的窗口（`days + 30`），再按 `best_date()` 二次过滤。
- **Crossref 的摘要是 JATS XML**（`<jats:title>Abstract</jats:title>`），要过 `clean_abstract()`，否则页面上会留下 "Abstract" 前缀。
- Crossref 标题里常嵌换行，必须 `normalize_title()` 压平。
- 期刊扉页/目录（"Issue Publication Information" 之类）会被 `SKIP_TITLES` 滤掉，返回空 dict，调用方要判空。
- **ECNU 接口对每个模型限并发 3**，`--workers` 默认就是 3，别调高。

## 配置

`.env`（gitignored）里有两组 LLM 配置，论文速览只用 `LLM_*`：

- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` — 华东师大 ECNU 的 OpenAI 兼容接口，默认 `ecnu-plus`
- `CROSSREF_MAILTO` — Crossref polite pool 要的邮箱
- `OPENAI_*` / `MODEL_NAME` — 旧中转，论文速览不用

## 其他约定

- `AGENTS.md` 是仓库自己的约定文件（输出命名 `<topic>-YYYY-MM-DD.*`、提交规范、论文速览的若干设计决定）。**改论文速览时两份都要更新**，别只改一处。
- 依赖只有 `requests` / `beautifulsoup4` / `python-dotenv`（见 `pyproject.toml`）。`papers_server.py` 只用标准库。加依赖前先想想能不能不加。
- `papers-marks.json` 是「想读」的用户数据，已 gitignore。
