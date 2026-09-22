#!/usr/bin/env python3
"""抓取期刊新论文并生成中英对照的本地网页。

数据来源：
- Crossref  `api.crossref.org/journals/{issn}/works` —— 标题、作者、卷期页码、日期、摘要
- OpenAlex  `api.openalex.org/works/doi:{doi}`          —— Crossref 没给摘要时的兜底

翻译走 OpenAI 兼容接口（默认华东师大 ecnu-plus），标题和摘要一次调用翻完，
结果按 DOI 缓存到 `.cache_papers/`，重跑只翻新增的。

用法：
    python papers_digest.py --days 30              # 抓近 30 天并翻译，生成网页
    python papers_digest.py --days 30 --no-llm     # 只抓取不翻译（英文页面）
    python papers_digest.py --days 7 --limit 20    # 小样本试跑
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

CACHE_SCHEMA = 2
CACHE_DIR = ROOT / ".cache_papers"
JOURNALS_FILE = ROOT / "journals.json"
TEMPLATE_FILE = ROOT / "papers_viewer.template.html"

MAILTO = os.getenv("CROSSREF_MAILTO", "tinytiger98@163.com")
HEADERS = {"User-Agent": f"papers-digest/0.1 (mailto:{MAILTO})"}

# 摘要在 Crossref 里是 JATS XML，这些标签直接去掉不留空
DROP_TAGS = {"script", "style"}

# 期刊扉页/目录这类不是论文的条目，标题命中就丢掉
SKIP_TITLES = {
    "issue publication information",
    "issue editorial masthead",
    "issue information",
    "front matter",
    "back matter",
    "table of contents",
    "editorial board",
    "in this issue",
}

# 单篇翻译失败时最多再试几次（定时任务无人值守，偶发失败不该永久留坑）
TRANSLATE_RETRIES = 2
# 摘要超长时截断，避免极端输入拖垮单次调用
ABSTRACT_MAX_CHARS = 4000

# 一次请求翻译多少篇。批量能大幅减少请求数（1390 篇从 1390 次降到 ~140 次），
# 又不会让单次响应长到容易漏段；调大之前先想想出错时的重试成本。
BATCH_SIZE = 10


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


def load_journals(path: Path = JOURNALS_FILE) -> list[dict[str, str]]:
    """读取期刊配置。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    journals = data.get("journals", [])
    if not journals:
        raise ValueError(f"{path} 里没有配置任何期刊")
    return journals


def get_llm_config() -> dict[str, str]:
    """读取 LLM 配置。优先 LLM_*，回退到仓库原有的 OPENAI_*。"""
    base_url = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("LLM_MODEL") or os.getenv("MODEL_NAME")
    if not all([base_url, api_key, model]):
        raise ValueError(
            "缺少 LLM 配置，请在 .env 里设置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL"
        )
    return {
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "model": model,
    }


# --------------------------------------------------------------------------
# 文本清洗
# --------------------------------------------------------------------------


def clean_text(value: str | None) -> str:
    """去掉 HTML/JATS 标签、还原实体、压平空白。"""
    if not value:
        return ""
    text = BeautifulSoup(html.unescape(value), "html.parser")
    for tag in text.find_all(DROP_TAGS):
        tag.decompose()
    return re.sub(r"\s+", " ", text.get_text(" ")).strip()


def clean_abstract(value: str | None) -> str:
    """清洗摘要，并去掉 JATS 的 <jats:title>Abstract</jats:title> 留下的前缀。"""
    text = clean_text(value)
    return re.sub(r"^(?:Graphical\s+)?Abstract[:\s]+", "", text, flags=re.IGNORECASE).strip()


def normalize_title(value: str | None) -> str:
    """Crossref 的标题里常有换行和多余空格，压成一行。"""
    return clean_text(value)


# --------------------------------------------------------------------------
# 抓取：Crossref
# --------------------------------------------------------------------------


def request_json(
    url: str, params: dict[str, Any], retries: int = 3, timeout: int = 45
) -> dict[str, Any]:
    """带退避重试的 GET，返回 JSON。404 返回空 dict。"""
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if response.status_code == 404:
                return {}
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(2**attempt)
    raise RuntimeError(f"请求失败（重试 {retries} 次）：{url}") from last_error


def date_parts_to_date(value: dict[str, Any] | None) -> date | None:
    """取 Crossref 日期字段里的完整年月日；只有年份的返回 None。"""
    parts = (value or {}).get("date-parts") or [[None]]
    first = parts[0]
    if len(first) < 3 or not all(first[:3]):
        return None
    try:
        return date(*first[:3])
    except ValueError:
        return None


def best_date(item: dict[str, Any]) -> date | None:
    """挑一个最能代表「这篇啥时候出来的」的日期。

    RSC 的 published-online 只精确到年，所以完整日期优先看 created
    （DOI 注册时间，约等于上线时间），再退回 print / issued。
    """
    for key in ("published-online", "created", "published-print", "issued"):
        value = date_parts_to_date(item.get(key))
        if value:
            return value
    return None


def crossref_url(doi: str) -> str:
    return f"https://doi.org/{doi}"


def article_from_crossref(item: dict[str, Any], journal: dict[str, str]) -> dict[str, Any]:
    """把一条 Crossref 记录整理成我们内部的结构。"""
    doi = item.get("DOI", "").strip()
    titles = item.get("title") or []
    title = normalize_title(titles[0] if titles else "")
    if title.lower().rstrip(".") in SKIP_TITLES:
        return {}
    abstract_raw = item.get("abstract") or ""
    return {
        "doi": doi,
        "journal": journal["short"],
        "journal_name": journal["name"],
        "title": title,
        "abstract": clean_abstract(abstract_raw),
        "abstract_source": "crossref" if abstract_raw else "",
        "authors": [
            {"given": a.get("given", ""), "family": a.get("family", "")}
            for a in (item.get("author") or [])
            if a.get("family") or a.get("given")
        ],
        "volume": item.get("volume") or "",
        "issue": item.get("issue") or "",
        "page": item.get("page") or "",
        "published": (best_date(item) or date.today()).isoformat(),
        "url": crossref_url(doi) if doi else "",
    }


def fetch_journal(
    journal: dict[str, str], days: int, today: date
) -> list[dict[str, Any]]:
    """抓一个期刊最近 days 天的文章。

    先用 created（DOI 注册时间）拉一个更宽的窗口，再按 best_date 二次过滤 ——
    RSC 这类刊的 published-online 只有年份，直接按发布日期过滤会一篇都抓不到。
    """
    since = today - timedelta(days=days)
    created_from = today - timedelta(days=days + 30)
    filters = (
        f"type:journal-article,"
        f"from-created-date:{created_from.isoformat()},"
        f"until-created-date:{today.isoformat()}"
    )
    select = (
        "DOI,title,abstract,author,volume,issue,page,"
        "published-online,published-print,issued,created,type"
    )

    articles: list[dict[str, Any]] = []
    cursor = "*"
    while cursor:
        data = request_json(
            f"https://api.crossref.org/journals/{journal['issn']}/works",
            {
                "filter": filters,
                "select": select,
                "rows": 200,
                "cursor": cursor,
                "mailto": MAILTO,
            },
        )
        message = data.get("message")
        if not message:
            break
        items = message.get("items") or []
        for item in items:
            published = best_date(item)
            if not published or not (since <= published <= today):
                continue
            article = article_from_crossref(item, journal)
            if article:  # 扉页/目录等非论文条目会被过滤成空 dict
                articles.append(article)
        cursor = message.get("next-cursor") if items else None
        if cursor:
            time.sleep(0.2)
    return articles


def collect_articles(
    journals: list[dict[str, str]], days: int, workers: int = 4
) -> list[dict[str, Any]]:
    """并发抓所有期刊，按 DOI 去重。"""
    today = date.today()
    by_doi: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(journals))) as executor:
        futures = {executor.submit(fetch_journal, j, days, today): j for j in journals}
        for future in as_completed(futures):
            journal = futures[future]
            try:
                for article in future.result():
                    if article["doi"]:
                        by_doi.setdefault(article["doi"], article)
            except Exception as exc:  # 单个期刊失败不影响其它期刊
                print(f"  ! {journal['short']} 抓取失败：{exc}", file=sys.stderr)
    articles = sorted(by_doi.values(), key=lambda a: a["published"], reverse=True)
    return articles


# --------------------------------------------------------------------------
# 抓取：OpenAlex 摘要兜底
# --------------------------------------------------------------------------


def reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str:
    """OpenAlex 的 abstract_inverted_index 还原成正文。"""
    if not inverted_index:
        return ""
    positioned: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        positioned.extend((position, word) for position in positions)
    positioned.sort(key=lambda pair: pair[0])
    return " ".join(word for _, word in positioned)


def fetch_openalex_abstract(doi: str, session: requests.Session) -> str:
    """从 OpenAlex 按 DOI 取摘要，失败返回空串。"""
    try:
        response = session.get(
            f"https://api.openalex.org/works/doi:{quote(doi, safe='')}",
            params={"select": "abstract_inverted_index", "mailto": MAILTO},
            timeout=30,
        )
        if response.status_code != 200:
            return ""
        data = response.json()
    except (requests.RequestException, ValueError):
        return ""
    return clean_abstract(reconstruct_abstract(data.get("abstract_inverted_index")))


def fill_missing_abstracts(articles: list[dict[str, Any]], workers: int = 4) -> int:
    """给 Crossref 没给摘要的文章补 OpenAlex 摘要。返回补上的篇数。"""
    targets = [a for a in articles if not a["abstract"]]
    if not targets:
        return 0
    session = requests.Session()
    session.headers.update(HEADERS)
    filled = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_openalex_abstract, a["doi"], session): a for a in targets
        }
        for future in as_completed(futures):
            article = futures[future]
            try:
                abstract = future.result()
            except Exception:
                abstract = ""
            if abstract:
                article["abstract"] = abstract
                article["abstract_source"] = "openalex"
                filled += 1
    return filled


# --------------------------------------------------------------------------
# 翻译
# --------------------------------------------------------------------------


# 标题/摘要的分节标记。要求后面跟冒号或行尾，避免把「标题和摘要分别翻译」这类句子误判成分节。
_HEADING_RE = re.compile(
    r"^[*#\s]*(标题|摘要)[*#\s]*(?:[:：]\s*(.*)|\s*)$"
)


def split_translation(text: str) -> tuple[str, str]:
    """解析模型返回的标题 / 摘要两段。

    容忍 `## 标题` + 换行内容、`**标题**: 同行内容`、以及开场白等噪声。
    解析不出来返回两个空串，由调用方决定是否重试。
    """
    sections: dict[str, list[str]] = {"标题": [], "摘要": []}
    current: str | None = None
    for line in text.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match:
            current = match.group(1)
            inline = (match.group(2) or "").strip()
            if inline:
                sections[current].append(inline)
        elif current:
            sections[current].append(line)

    title = re.sub(r"\s+", " ", " ".join(sections["标题"])).strip().strip("*# ")
    abstract = "\n".join(sections["摘要"]).strip().strip("*# ")
    abstract = "\n".join(
        re.sub(r"[ \t]+", " ", line).strip() for line in abstract.splitlines()
    ).strip()
    return title, abstract


# 批量返回的段落标记：`【3】` / `[3]` / `### 3.` 都认
_BATCH_MARK_RE = re.compile(
    r"^\s*(?:[【\[]\s*(\d+)\s*[】\]]|#{1,4}\s*(\d+)\s*[.、）)]?)\s*[:：]?\s*$"
)

SYSTEM_PROMPT = (
    "你是一个专业的学术文献翻译助手，"
    "擅长将化学、物理、材料与机器学习领域的学术论文翻译为准确的中文。"
)

# 每篇统一的要求，单篇和批量共用
TRANSLATION_RULES = [
    "保持专业性和准确性，使用标准的学术术语",
    "化学式、缩写、模型名（如 DFT、QM/MM、JACS）保留英文原文",
]


def call_llm(prompt: str, llm: dict[str, str], timeout: int = 120) -> str:
    """发一次 chat/completions，返回正文。429 退避重试。"""
    last_error: Exception | None = None
    for attempt in range(TRANSLATE_RETRIES + 1):
        try:
            response = requests.post(
                f"{llm['base_url']}/chat/completions",
                headers={
                    "Authorization": f"Bearer {llm['api_key']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": llm["model"],
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.2,
                },
                timeout=timeout,
            )
            if response.status_code == 429 and attempt < TRANSLATE_RETRIES:
                time.sleep(5 * (attempt + 1))  # 撞并发上限，退避后重试
                continue
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            last_error = exc
            if attempt < TRANSLATE_RETRIES:
                time.sleep(2**attempt)
    raise RuntimeError(f"调用失败：{last_error}")


def translate(title: str, abstract: str, llm: dict[str, str]) -> tuple[str, str]:
    """单篇兜底路径：一次调用把标题和摘要都翻成中文。"""
    abstract = abstract[:ABSTRACT_MAX_CHARS]
    rules = "\n".join(f"{i}. {rule}" for i, rule in enumerate(TRANSLATION_RULES, 1))
    prompt = (
        "请将以下学术论文的标题和摘要翻译为中文。要求：\n"
        f"{rules}\n"
        "3. 只输出译文，不要任何解释或额外说明\n\n"
        f"标题: {title}\n\n"
        f"摘要: {abstract or '（无）'}\n\n"
        "严格按以下格式输出：\n\n"
        "## 标题\n[翻译后的标题]\n\n"
        "## 摘要\n[翻译后的摘要]"
    )
    content = call_llm(prompt, llm)
    title_cn, abstract_cn = split_translation(content)
    if not title_cn:
        raise RuntimeError("模型返回内容解析不出标题")
    return title_cn, abstract_cn


def build_batch_prompt(papers: list[dict[str, Any]]) -> str:
    """把多篇拼成一个请求。编号必须原样回显，否则对不上号。"""
    count = len(papers)
    rules = "\n".join(f"{i}. {rule}" for i, rule in enumerate(TRANSLATION_RULES, 1))
    head = (
        f"请把下面 {count} 篇学术论文的标题和摘要分别翻译成中文。要求：\n"
        f"{rules}\n"
        f"{len(TRANSLATION_RULES) + 1}. 只输出译文，不要任何解释或额外说明\n"
        f"{len(TRANSLATION_RULES) + 2}. 必须输出 {count} 段，每段以原编号开头，"
        "编号和顺序都不能改、不能合并、不能省略\n\n"
        "输出格式（每篇一段）：\n"
        "【1】\n"
        "标题: [第 1 篇的中文标题]\n"
        "摘要: [第 1 篇的中文摘要]\n\n"
        "【2】\n"
        "标题: ...\n"
        "摘要: ...\n\n"
        "待翻译的原文：\n"
    )
    lines = [head]
    for index, paper in enumerate(papers, 1):
        abstract = (paper.get("abstract") or "")[:ABSTRACT_MAX_CHARS]
        lines.append(f"\n【{index}】")
        lines.append(f"标题: {paper['title']}")
        lines.append(f"摘要: {abstract or '（无摘要）'}")
    return "\n".join(lines)


def split_batch_translation(text: str) -> dict[int, str]:
    """把批量返回按编号切块，返回 {编号: 该篇的文本块}。"""
    blocks: dict[int, str] = {}
    current: int | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        match = _BATCH_MARK_RE.match(line)
        if match:
            if current is not None:
                blocks[current] = "\n".join(buffer)
            current = int(match.group(1) or match.group(2))
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        blocks[current] = "\n".join(buffer)
    return blocks


def translate_batch(
    papers: list[dict[str, Any]], llm: dict[str, str]
) -> dict[str, tuple[str, str]]:
    """一次调用翻译多篇。返回 {doi: (中文标题, 中文摘要)}。

    只返回解析成功的；对不上号的（模型漏了/合并了）留给调用方逐篇补。
    """
    if not papers:
        return {}
    if len(papers) == 1:
        paper = papers[0]
        return {paper["doi"]: translate(paper["title"], paper["abstract"], llm)}

    # 批量响应更长，超时按篇数放宽
    timeout = min(90 + 20 * len(papers), 600)
    content = call_llm(build_batch_prompt(papers), llm, timeout=timeout)
    blocks = split_batch_translation(content)

    results: dict[str, tuple[str, str]] = {}
    for index, paper in enumerate(papers, 1):
        block = blocks.get(index)
        if not block:
            continue
        title_cn, abstract_cn = split_translation(block)
        if title_cn:
            results[paper["doi"]] = (title_cn, abstract_cn)
    return results


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------


def doi_cache_name(doi: str) -> str:
    return doi.replace("/", "_") + ".json"


def content_hash(article: dict[str, Any]) -> str:
    """英文原文的指纹，原文变了就重翻。"""
    payload = f"{article.get('title', '')}\x00{article.get('abstract', '')}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def read_cache(cache_dir: Path) -> dict[str, dict[str, Any]]:
    """把所有缓存读进内存，键是 DOI。"""
    cache: dict[str, dict[str, Any]] = {}
    if not cache_dir.is_dir():
        return cache
    for path in cache_dir.glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if entry.get("schema") == CACHE_SCHEMA and entry.get("doi"):
            cache[entry["doi"]] = entry
    return cache


def write_cache(cache_dir: Path, article: dict[str, Any]) -> None:
    cache_dir.mkdir(exist_ok=True)
    payload = {
        "schema": CACHE_SCHEMA,
        "doi": article["doi"],
        "en_hash": content_hash(article),
        "title_cn": article.get("title_cn", ""),
        "abstract_cn": article.get("abstract_cn", ""),
        "model": article.get("translated_by", ""),
        "translated_at": article.get("translated_at", ""),
    }
    (cache_dir / doi_cache_name(article["doi"])).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def apply_cache(article: dict[str, Any], cache: dict[str, dict[str, Any]]) -> bool:
    """命中缓存就把译文贴上，返回是否已翻译。"""
    entry = cache.get(article["doi"])
    if not entry or entry.get("en_hash") != content_hash(article):
        return False
    if not entry.get("title_cn"):
        return False
    article["title_cn"] = entry.get("title_cn", "")
    article["abstract_cn"] = entry.get("abstract_cn", "")
    article["translated_by"] = entry.get("model", "")
    article["translated_at"] = entry.get("translated_at", "")
    article["from_cache"] = True
    return True


# --------------------------------------------------------------------------
# 翻译编排
# --------------------------------------------------------------------------


def record_translation(
    article: dict[str, Any],
    title_cn: str,
    abstract_cn: str,
    llm: dict[str, str],
    cache_dir: Path,
) -> None:
    article["title_cn"] = title_cn
    article["abstract_cn"] = abstract_cn
    article["translated_by"] = llm["model"]
    article["translated_at"] = datetime.now().isoformat(timespec="seconds")
    write_cache(cache_dir, article)


def apply_cached_translations(articles: list[dict[str, Any]], cache_dir: Path) -> int:
    """把缓存里已有的译文贴回文章上，返回命中数。不碰网络。"""
    cache = read_cache(cache_dir)
    return sum(1 for a in articles if apply_cache(a, cache))


def translate_all(
    articles: list[dict[str, Any]],
    llm: dict[str, str],
    cache_dir: Path,
    workers: int,
    batch_size: int = BATCH_SIZE,
) -> tuple[int, int]:
    """翻译所有还没译过的文章。返回（命中缓存数，新翻译数）。

    先用一个请求批量翻多篇（省请求数、摊薄系统提示和格式说明），
    批量里漏掉的再逐篇补 —— 模型偶尔会漏段或合并编号，
    逐篇兜底保证不会有条目静默地留在英文。
    """
    cached = apply_cached_translations(articles, cache_dir)
    pending = [a for a in articles if not a.get("title_cn")]
    if not pending:
        return cached, 0

    batch_size = max(1, batch_size)
    batches = [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]
    print(
        f"  缓存命中 {cached} 篇，待翻译 {len(pending)} 篇 → "
        f"{len(batches)} 批（每批 {batch_size} 篇，并发 {workers}）"
    )

    translated = 0
    leftovers: list[dict[str, Any]] = []
    finished = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(translate_batch, b, llm): b for b in batches}
        for future in as_completed(futures):
            batch = futures[future]
            finished += 1
            try:
                results = future.result()
            except Exception as exc:
                print(f"\n  ! 一批 {len(batch)} 篇失败：{exc}", file=sys.stderr)
                results = {}
            for article in batch:
                got = results.get(article["doi"])
                if got:
                    record_translation(article, got[0], got[1], llm, cache_dir)
                    translated += 1
                else:
                    leftovers.append(article)
            print(
                f"\r  批量 {finished}/{len(batches)} 批"
                f"（已译 {translated} 篇，待补 {len(leftovers)} 篇）",
                end="",
                flush=True,
            )
    print()

    failed: list[dict[str, Any]] = []
    if leftovers:
        print(f"  批量里漏掉 {len(leftovers)} 篇，逐篇补齐…")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(translate, a["title"], a["abstract"], llm): a
                for a in leftovers
            }
            for count, future in enumerate(as_completed(futures), 1):
                article = futures[future]
                try:
                    title_cn, abstract_cn = future.result()
                    record_translation(article, title_cn, abstract_cn, llm, cache_dir)
                    translated += 1
                except Exception as exc:
                    failed.append(article)
                    print(f"\n  ! 翻译失败 {article['doi']}：{exc}", file=sys.stderr)
                print(
                    f"\r  逐篇补齐 {count}/{len(leftovers)}（失败 {len(failed)}）",
                    end="",
                    flush=True,
                )
        print()

    if failed and len(failed) == len(pending):
        print(
            "  ⚠️ 所有翻译都失败了 —— 多半是 LLM_API_KEY 失效或额度用尽，"
            "先手工确认 .env 里的配置。本次仍然生成了英文版页面。",
            file=sys.stderr,
        )
    elif failed:
        print(f"  ⚠️ {failed} 篇翻译失败，这些条目在页面上会显示英文原文。", file=sys.stderr)
    return cached, translated


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def load_zotero_dois() -> set[str]:
    """读出已存入 Zotero 的 DOI（由 papers_server.py 维护）。

    页面的「已加入 Zotero」筛选器靠它 —— 烤进 HTML 里，
    这样不开本地服务、直接双击静态文件时也知道哪些已经入库了。
    """
    path = ROOT / "papers-zotero.json"
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("dois", []))
    except (OSError, ValueError):
        return set()


def render_html(
    articles: list[dict[str, Any]],
    output_path: Path,
    days: int,
    translated: bool,
) -> None:
    """把数据内联进模板，写出一个自包含的 HTML。"""
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    zotero_dois = load_zotero_dois()
    data = [
        {
            "doi": a["doi"],
            "journal": a["journal"],
            "journalName": a["journal_name"],
            "title": a["title"],
            "titleCn": a.get("title_cn", ""),
            "abstract": a["abstract"],
            "abstractCn": a.get("abstract_cn", ""),
            "abstractSource": a.get("abstract_source", ""),
            "authors": a["authors"],
            "volume": a["volume"],
            "issue": a["issue"],
            "page": a["page"],
            "published": a["published"],
            "url": a["url"],
            "inZotero": a["doi"] in zotero_dois,
        }
        for a in articles
    ]
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html_out = (
        template.replace("__DATA__", payload)
        .replace("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))
        .replace("__COUNT__", str(len(data)))
        .replace("__DAYS__", str(days))
        .replace("__TRANSLATED__", "true" if translated else "false")
    )
    output_path.write_text(html_out, encoding="utf-8")


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取期刊新论文，翻译成中英对照，生成可双击打开的本地网页。"
    )
    parser.add_argument("--days", type=int, default=30, help="回溯天数（默认 30）")
    parser.add_argument("--output", type=str, default=None, help="输出去向（默认 papers-digest.html）")
    parser.add_argument("--limit", type=int, default=None, help="只处理最新的 N 篇（调试用）")
    parser.add_argument("--workers", type=int, default=3, help="翻译并发数（ECNU 单模型上限 3）")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=f"一次请求翻译多少篇（默认 {BATCH_SIZE}，设 1 则逐篇翻）",
    )
    parser.add_argument("--fetch-workers", type=int, default=4, help="抓取并发数")
    parser.add_argument("--no-llm", action="store_true", help="只抓取不翻译（英文页面）")
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR, help="缓存目录")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    journals = load_journals()

    print(f"抓取 {len(journals)} 个期刊最近 {args.days} 天的文章…")
    articles = collect_articles(journals, args.days, workers=args.fetch_workers)
    print(f"  Crossref 拿到 {len(articles)} 篇")

    filled = fill_missing_abstracts(articles, workers=args.fetch_workers)
    if filled:
        print(f"  OpenAlex 补了 {filled} 篇摘要")

    if args.limit:
        articles = articles[: args.limit]
        print(f"  --limit 生效，只保留最新 {len(articles)} 篇")

    if args.no_llm:
        # 只是不调 LLM，缓存里已有的译文照样要用 —— 否则 --no-llm 会生成纯英文页面
        cached = apply_cached_translations(articles, args.cache_dir)
        print(f"跳过翻译（--no-llm），从缓存贴回 {cached} 篇")
    else:
        llm = get_llm_config()
        print(f"翻译中（模型 {llm['model']}）…")
        cached, fresh = translate_all(
            articles,
            llm,
            args.cache_dir,
            workers=args.workers,
            batch_size=args.batch_size,
        )
        print(f"  缓存命中 {cached} 篇，新翻译 {fresh} 篇")

    translated = any(a.get("title_cn") for a in articles)

    missing = sum(1 for a in articles if not a.get("title") or not a.get("abstract"))
    if missing:
        print(f"  注意：{missing} 篇缺标题或摘要")

    output = Path(args.output) if args.output else ROOT / "papers-digest.html"
    render_html(articles, output, args.days, translated)
    dated = output.with_name(f"{output.stem}-{date.today().isoformat()}{output.suffix}")
    if dated != output:
        render_html(articles, dated, args.days, translated)

    print(f"完成：{len(articles)} 篇 → {output}")
    print(f"                              {dated}")


if __name__ == "__main__":
    main()
