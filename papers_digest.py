#!/usr/bin/env python3
"""抓取期刊新论文并生成中英对照的本地网页。

数据来源：
- Crossref  `api.crossref.org/journals/{issn}/works` —— 标题、作者、卷期页码、日期、摘要
- OpenAlex  `api.openalex.org/works/doi:{doi}`          —— Crossref 没给摘要时的兜底

更新时只翻译标题，摘要由本地服务在点击「显示摘要」时按需翻译。
结果按 DOI 缓存到 `.cache_papers/`，标题和摘要分别校验原文。

用法：
    python papers_digest.py --days 30              # 抓近 30 天并翻译，生成网页
    python papers_digest.py --days 30 --no-llm     # 只抓取不翻译（英文页面）
    python papers_digest.py --days 7 --limit 20    # 小样本试跑
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

CACHE_SCHEMA = 3
CACHE_DIR = ROOT / ".cache_papers"
JOURNALS_FILE = ROOT / "journals.json"
TEMPLATE_FILE = ROOT / "papers_viewer.template.html"
ARCHIVE_FILE = ROOT / "papers-archive.json"
UPDATE_FILE = ROOT / "papers-update-status.json"
READING_FILE = ROOT / "papers-reading.json"

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
    "contents list",
    "front cover",
    "back cover",
    "inside front cover",
    "inside back cover",
}

# 单篇翻译失败时最多再试几次（定时任务无人值守，偶发失败不该永久留坑）
TRANSLATE_RETRIES = 2

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
    journal: dict[str, str], days: int, today: date,
    start_date: date | None = None, end_date: date | None = None,
) -> list[dict[str, Any]]:
    """抓一个期刊最近 days 天或指定起止日期的文章。

    先用 created（DOI 注册时间）拉一个更宽的窗口，再按 best_date 二次过滤 ——
    RSC 这类刊的 published-online 只有年份，直接按发布日期过滤会一篇都抓不到。
    指定范围时再合并发布日期查询，补上较晚注册 DOI 的论文。
    """
    since = start_date or today - timedelta(days=days)
    until = end_date or today
    created_from = since - timedelta(days=30)
    created_until = min(today, until + timedelta(days=30)) if start_date else today
    filters = [(
        f"type:journal-article,"
        f"from-created-date:{created_from.isoformat()},"
        f"until-created-date:{created_until.isoformat()}"
    )]
    if start_date:
        # 补查发布日期，覆盖 DOI 较晚注册的旧论文；RSC 年份不完整仍走 created。
        filters.append(f"type:journal-article,from-pub-date:{since.isoformat()},"
                       f"until-pub-date:{until.isoformat()}")
    select = (
        "DOI,title,abstract,author,volume,issue,page,"
        "published-online,published-print,issued,created,type"
    )

    by_doi: dict[str, dict[str, Any]] = {}
    for query_filter in filters:
        cursor = "*"
        while cursor:
            data = request_json(
                f"https://api.crossref.org/journals/{journal['issn']}/works",
                {"filter": query_filter, "select": select, "rows": 200,
                 "cursor": cursor, "mailto": MAILTO},
            )
            message = data.get("message")
            if not message:
                break
            items = message.get("items") or []
            for item in items:
                published = best_date(item)
                if not published or not (since <= published <= until):
                    continue
                article = article_from_crossref(item, journal)
                if article and article["doi"]:
                    by_doi.setdefault(article["doi"], article)
            cursor = message.get("next-cursor") if items else None
            if cursor:
                time.sleep(0.2)
    return list(by_doi.values())


def collect_articles(
    journals: list[dict[str, str]], days: int, workers: int = 4,
    failures: list[str] | None = None,
    start_date: date | None = None, end_date: date | None = None,
) -> list[dict[str, Any]]:
    """并发抓所有期刊，按 DOI 去重。"""
    today = date.today()
    by_doi: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(journals))) as executor:
        futures = {executor.submit(fetch_journal, j, days, today, start_date, end_date): j
                   for j in journals}
        for future in as_completed(futures):
            journal = futures[future]
            try:
                for article in future.result():
                    if article["doi"]:
                        by_doi.setdefault(article["doi"], article)
            except Exception as exc:  # 单个期刊失败不影响其它期刊
                if failures is not None:
                    failures.append(journal["short"])
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


def translate_title(title: str, llm: dict[str, str]) -> str:
    """只翻译标题，绝不把摘要发送给模型。"""
    rules = "\n".join(f"{i}. {rule}" for i, rule in enumerate(TRANSLATION_RULES, 1))
    prompt = (
        "请将以下学术论文的标题翻译为中文。要求：\n"
        f"{rules}\n"
        "3. 只输出译文，不要任何解释或额外说明\n\n"
        f"标题: {title}\n\n"
        "严格按以下格式输出：\n\n"
        "## 标题\n[翻译后的标题]"
    )
    content = call_llm(prompt, llm)
    title_cn, _ = split_translation(content)
    if not title_cn:
        raise RuntimeError("模型返回内容解析不出标题")
    return title_cn


def translate_abstract(abstract: str, llm: dict[str, str]) -> str:
    """用户明确查看时才翻译完整摘要，不重复翻译标题。"""
    rules = "\n".join(f"{i}. {rule}" for i, rule in enumerate(TRANSLATION_RULES, 1))
    content = call_llm(
        "请将以下学术论文摘要完整翻译为中文。要求：\n"
        f"{rules}\n3. 不要概括、删减或补充原文没有的信息\n"
        "4. 只输出摘要译文，不要标题、开场白或额外说明\n\n"
        f"摘要原文：\n{abstract}", llm,
    ).strip()
    if not content:
        raise RuntimeError("模型未返回摘要译文")
    translated = re.sub(r"^\s*(?:#{1,4}\s*)?摘要(?:\s*[:：]\s*|\s*\n)", "", content).strip()
    if not translated:
        raise RuntimeError("模型未返回摘要译文")
    return translated


def build_batch_prompt(papers: list[dict[str, Any]]) -> str:
    """把多篇拼成一个请求。编号必须原样回显，否则对不上号。"""
    count = len(papers)
    rules = "\n".join(f"{i}. {rule}" for i, rule in enumerate(TRANSLATION_RULES, 1))
    head = (
        f"请把下面 {count} 篇学术论文的标题分别翻译成中文。要求：\n"
        f"{rules}\n"
        f"{len(TRANSLATION_RULES) + 1}. 只输出译文，不要任何解释或额外说明\n"
        f"{len(TRANSLATION_RULES) + 2}. 必须输出 {count} 段，每段以原编号开头，"
        "编号和顺序都不能改、不能合并、不能省略\n\n"
        "输出格式（每篇一段）：\n"
        "【1】\n"
        "标题: [第 1 篇的中文标题]\n"
        "\n"
        "【2】\n"
        "标题: ...\n"
        "\n"
        "待翻译的原文：\n"
    )
    lines = [head]
    for index, paper in enumerate(papers, 1):
        lines.append(f"\n【{index}】")
        lines.append(f"标题: {paper['title']}")
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
) -> dict[str, str]:
    """一次调用只翻译多篇标题。返回 {doi: 中文标题}。

    只返回解析成功的；对不上号的（模型漏了/合并了）留给调用方逐篇补。
    """
    if not papers:
        return {}
    if len(papers) == 1:
        paper = papers[0]
        return {paper["doi"]: translate_title(paper["title"], llm)}

    # 批量响应更长，超时按篇数放宽
    timeout = min(90 + 20 * len(papers), 600)
    content = call_llm(build_batch_prompt(papers), llm, timeout=timeout)
    blocks = split_batch_translation(content)

    results: dict[str, str] = {}
    for index, paper in enumerate(papers, 1):
        block = blocks.get(index)
        if not block:
            continue
        title_cn, _ = split_translation(block)
        if title_cn:
            results[paper["doi"]] = title_cn
    return results


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------


def doi_cache_name(doi: str) -> str:
    return doi.replace("/", "_") + ".json"


def content_hash(article: dict[str, Any]) -> str:
    """兼容 schema 2 的标题+摘要指纹。"""
    payload = f"{article.get('title', '')}\x00{article.get('abstract', '')}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


@contextmanager
def file_lock(path: Path):
    """协调后台标题更新和服务端按需翻译，防止互相覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def read_cache_entry(cache_dir: Path, doi: str) -> dict[str, Any]:
    path = cache_dir / doi_cache_name(doi)
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if entry.get("doi") == doi and entry.get("schema") in (2, CACHE_SCHEMA):
        return entry
    return {}


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
        if entry.get("schema") in (2, CACHE_SCHEMA) and entry.get("doi"):
            cache[entry["doi"]] = entry
    return cache


def write_cache(
    cache_dir: Path, article: dict[str, Any], title_only: bool = False,
    abstract_only: bool = False,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / doi_cache_name(article["doi"])
    with file_lock(target.with_suffix(".lock")):
        previous = read_cache_entry(cache_dir, article["doi"])
        if previous.get("schema") == 2:
            if previous.get("en_hash") == content_hash(article):
                previous.update(title_hash=text_hash(article["title"]),
                                abstract_hash=text_hash(article.get("abstract", "")))
            else:
                previous = {}
        payload = dict(previous)
        payload.update(schema=CACHE_SCHEMA, doi=article["doi"])
        payload.pop("en_hash", None)
        if article.get("title_cn") and (not abstract_only or not payload.get("title_cn")):
            payload.update(title_cn=article["title_cn"], title_hash=text_hash(article["title"]))
        # 标题更新不抹掉另一进程刚刚翻好的摘要。
        if not title_only or (article.get("abstract_cn") and not payload.get("abstract_cn")):
            abstract_hash = text_hash(article.get("abstract", ""))
            if article.get("abstract_cn") or payload.get("abstract_hash") != abstract_hash:
                payload.update(abstract_cn=article.get("abstract_cn", ""), abstract_hash=abstract_hash)
        payload.update(model=article.get("translated_by", previous.get("model", "")),
                       translated_at=article.get("translated_at", previous.get("translated_at", "")))
        atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2))


def apply_cache(article: dict[str, Any], cache: dict[str, dict[str, Any]]) -> bool:
    """分别复用标题/摘要缓存；返回是否有可用的标题缓存。"""
    entry = cache.get(article["doi"])
    if not entry:
        return False
    if entry.get("schema") == 2:
        title_match = abstract_match = entry.get("en_hash") == content_hash(article)
    else:
        title_match = entry.get("title_hash") == text_hash(article.get("title", ""))
        abstract_match = entry.get("abstract_hash") == text_hash(article.get("abstract", ""))
    if abstract_match and entry.get("abstract_cn"):
        article["abstract_cn"] = entry["abstract_cn"]
    if not title_match or not entry.get("title_cn"):
        return False
    article["title_cn"] = entry.get("title_cn", "")
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
    llm: dict[str, str],
    cache_dir: Path,
) -> None:
    article["title_cn"] = title_cn
    article["translated_by"] = llm["model"]
    article["translated_at"] = datetime.now().isoformat(timespec="seconds")
    write_cache(cache_dir, article, title_only=True)


def apply_cached_translations(articles: list[dict[str, Any]], cache_dir: Path) -> int:
    """把缓存里已有的译文贴回文章上，返回命中数。不碰网络。"""
    cache = read_cache(cache_dir)
    return sum(1 for a in articles if apply_cache(a, cache) or a.get("title_cn"))


def translate_all(
    articles: list[dict[str, Any]],
    llm: dict[str, str],
    cache_dir: Path,
    workers: int,
    batch_size: int = BATCH_SIZE,
) -> tuple[int, int]:
    """只翻译还没有中文标题的文章。返回（复用标题数，新翻译数）。

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
                    record_translation(article, got, llm, cache_dir)
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
                executor.submit(translate_title, a["title"], llm): a
                for a in leftovers
            }
            for count, future in enumerate(as_completed(futures), 1):
                article = futures[future]
                try:
                    title_cn = future.result()
                    record_translation(article, title_cn, llm, cache_dir)
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
        print(f"  ⚠️ {len(failed)} 篇标题翻译失败，这些条目显示英文标题。", file=sys.stderr)
    return cached, translated


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def atomic_write_text(path: Path, text: str) -> None:
    """先完整写临时文件，再替换，避免读取到半份页面/状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(text)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    # 持久数据损坏时显式报错，不能悄悄当空库覆盖。
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 必须是 JSON 对象")
    return value


def is_paper(article: dict[str, Any]) -> bool:
    return bool(article.get("doi")) and (
        article.get("title", "").strip().lower().rstrip(".") not in SKIP_TITLES
    )


def keep_missing_text(article: dict[str, Any], old: dict[str, Any]) -> None:
    """补回暂时缺失的内容；原文发生变化时不能贴回过期译文。"""
    if not article.get("abstract") and old.get("abstract"):
        article["abstract"] = old["abstract"]
        article["abstract_source"] = old.get("abstract_source", "")
    if article.get("title") == old.get("title") and not article.get("title_cn"):
        article["title_cn"] = old.get("title_cn", "")
    if article.get("abstract") == old.get("abstract") and not article.get("abstract_cn"):
        article["abstract_cn"] = old.get("abstract_cn", "")


def load_archive() -> dict[str, Any]:
    """首次升级从每日 HTML 恢复原文及译文；此后只读持久库。"""
    if ARCHIVE_FILE.exists():
        archive = read_json_file(ARCHIVE_FILE)
        if archive.get("schema") != 1 or not isinstance(archive.get("articles"), list):
            raise ValueError("论文历史库结构不受支持，请保留文件并检查")
        return archive
    by_doi: dict[str, dict[str, Any]] = {}
    last_success = ""
    previous_success = ""
    sources = sorted(ROOT.glob("papers-digest-20??-??-??.html"))
    sources.append(ROOT / "papers-digest.html")
    for path in sources:
        if not path.exists():
            continue
        page = path.read_text(encoding="utf-8")
        match = re.search(r'<script id="article-data"[^>]*>(.*?)</script>', page, re.S)
        generated = re.search(r"生成于 (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", page)
        if not match or not generated:
            continue
        entries = json.loads(match.group(1))
        if not entries:
            continue
        timestamp = datetime.fromisoformat(generated.group(1)).astimezone().isoformat()
        if timestamp > last_success:
            previous_success, last_success = last_success, timestamp
        for item in entries:
            if not is_paper(item):
                continue
            old = by_doi.get(item["doi"], {})
            article = {
                "doi": item["doi"], "journal": item["journal"],
                "journal_name": item.get("journalName", ""), "title": item["title"],
                "title_cn": item.get("titleCn", ""), "abstract": item.get("abstract", ""),
                "abstract_cn": item.get("abstractCn", ""),
                "abstract_source": item.get("abstractSource", ""),
                "authors": item.get("authors", []), "volume": item.get("volume", ""),
                "issue": item.get("issue", ""), "page": item.get("page", ""),
                "published": item.get("published", ""), "url": item.get("url", ""),
                "first_seen_at": old.get("first_seen_at", item.get("firstSeenAt", timestamp)),
            }
            # 后来的数据源缺字段时，不抹掉已经收集的摘要和译文。
            keep_missing_text(article, old)
            by_doi[item["doi"]] = article
    return {"schema": 1, "articles": list(by_doi.values()),
            "lastSuccessAt": last_success, "previousSuccessAt": previous_success}


def merge_archive(
    archive: dict[str, Any], incoming: list[dict[str, Any]], timestamp: str
) -> tuple[list[dict[str, Any]], int]:
    by_doi = {a["doi"]: dict(a) for a in archive.get("articles", []) if is_paper(a)}
    added = 0
    for item in incoming:
        if not is_paper(item):
            continue
        old = by_doi.get(item["doi"])
        article = dict(item)
        article["first_seen_at"] = old.get("first_seen_at", timestamp) if old else timestamp
        if old:
            keep_missing_text(article, old)
        else:
            added += 1
        by_doi[item["doi"]] = article
    return sorted(by_doi.values(), key=lambda a: (a["published"], a["doi"]), reverse=True), added


def render_html(
    articles: list[dict[str, Any]],
    output_path: Path,
    days: int,
    translated: bool,
    metadata: dict[str, Any] | None = None,
) -> None:
    """把数据内联进模板，写出一个自包含的 HTML。"""
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    marks = read_json_file(ROOT / "papers-marks.json").get("starred", [])
    reading = read_json_file(READING_FILE).get("articles", {})
    metadata = metadata or {}
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
            "starred": a["doi"] in marks,
            "reading": reading.get(a["doi"]),
            "firstSeenAt": a.get("first_seen_at", ""),
        }
        for a in articles
    ]
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html_out = (
        template.replace("__DATA__", payload)
        .replace("__META__", json.dumps(metadata, ensure_ascii=False).replace("</", "<\\/"))
        .replace("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))
        .replace("__COUNT__", str(len(data)))
        .replace("__DAYS__", str(days))
        .replace("__TRANSLATED__", "true" if translated else "false")
    )
    atomic_write_text(output_path, html_out)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def resolve_date_range(
    start: str | None, end: str | None, today: date | None = None,
) -> tuple[date, date] | None:
    """起止日期必须成对、完整、递增且不晚于今天（含两个端点）。"""
    if start is None and end is None:
        return None
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("请选择完整的开始日期和结束日期")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
        raise ValueError("日期格式应为 YYYY-MM-DD")
    try:
        since, until = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise ValueError("日期无效，请检查年月日") from exc
    if since > until:
        raise ValueError("开始日期不能晚于结束日期")
    if until > (today or date.today()):
        raise ValueError("结束日期不能晚于今天")
    return since, until


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取期刊新论文，翻译成中英对照，生成可双击打开的本地网页。"
    )
    parser.add_argument("--days", type=int, default=30, help="回溯天数（默认 30）")
    parser.add_argument("--start-date", help="抓取开始日期 YYYY-MM-DD（含当天）")
    parser.add_argument("--end-date", help="抓取结束日期 YYYY-MM-DD（含当天）")
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
    args = parser.parse_args()
    try:
        resolve_date_range(args.start_date, args.end_date)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main() -> None:
    args = parse_args()
    journals = load_journals()

    selected_range = resolve_date_range(getattr(args, "start_date", None),
                                       getattr(args, "end_date", None))
    date_options = dict(zip(("start_date", "end_date"), selected_range)) if selected_range else {}
    range_metadata = {"startDate": selected_range[0].isoformat(),
                      "endDate": selected_range[1].isoformat()} if selected_range else {}
    scope = f"{selected_range[0]} 至 {selected_range[1]}" if selected_range else f"最近 {args.days} 天"
    print(f"抓取 {len(journals)} 个期刊{scope}的文章…")
    failures: list[str] = []
    articles = collect_articles(journals, args.days, workers=args.fetch_workers,
                                failures=failures, **date_options)
    print(f"  Crossref 拿到 {len(articles)} 篇")
    if not articles:
        status = {"state": "failed", "attemptedAt": datetime.now().astimezone().isoformat(),
                  "failedJournals": sorted(failures),
                  "message": "所选范围未取得论文，已保留上次成功页面", **range_metadata}
        atomic_write_text(UPDATE_FILE, json.dumps(status, ensure_ascii=False, indent=2))
        print("更新失败：未取得论文，保留上次成功页面。", file=sys.stderr)
        raise SystemExit(1)

    # 复用历史标题和摘要；缺少摘要的 OpenAlex 兜底推迟到用户查看时。
    archive = load_archive()
    previous = {a["doi"]: a for a in archive.get("articles", [])}
    for article in articles:
        old = previous.get(article["doi"])
        if old:
            keep_missing_text(article, old)

    if args.limit:
        articles = articles[: args.limit]
        print(f"  --limit 生效，只保留最新 {len(articles)} 篇")

    if args.no_llm:
        # 只是不调 LLM，缓存里已有的译文照样要用 —— 否则 --no-llm 会生成纯英文页面
        cached = apply_cached_translations(articles, args.cache_dir)
        print(f"跳过翻译（--no-llm），从缓存贴回 {cached} 篇")
    else:
        llm = get_llm_config()
        print(f"只翻译新标题（模型 {llm['model']}），摘要按需翻译…")
        cached, fresh = translate_all(
            articles,
            llm,
            args.cache_dir,
            workers=args.workers,
            batch_size=args.batch_size,
        )
        print(f"  缓存命中 {cached} 篇，新翻译 {fresh} 篇")

    translated = any(a.get("title_cn") for a in articles)
    metadata: dict[str, Any] = {}
    if not args.limit:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with file_lock(ARCHIVE_FILE.with_suffix(".lock")):
            # 翻译期间可能刚有人点开摘要，必须重读并合并最新历史。
            archive = load_archive()
            articles, added = merge_archive(archive, articles, timestamp)
            metadata = {
                "state": "partial" if failures else "ok", "attemptedAt": timestamp,
                "lastSuccessAt": archive.get("lastSuccessAt", "") if failures else timestamp,
                "previousSuccessAt": archive.get("lastSuccessAt", ""),
                "failedJournals": sorted(failures), "added": added,
                "translationPolicy": "titles-only",
                **range_metadata,
                "message": "部分期刊更新失败，保留其历史记录" if failures else "更新成功",
            }
            archive.update({"articles": articles, "lastSuccessAt": metadata["lastSuccessAt"],
                            "previousSuccessAt": metadata["previousSuccessAt"]})
            atomic_write_text(ARCHIVE_FILE, json.dumps(archive, ensure_ascii=False))
            atomic_write_text(UPDATE_FILE, json.dumps(metadata, ensure_ascii=False, indent=2))
        translated = any(a.get("title_cn") for a in articles)

    missing = sum(1 for a in articles if not a.get("title") or not a.get("abstract"))
    if missing:
        print(f"  注意：{missing} 篇缺标题或摘要")

    output = Path(args.output) if args.output else ROOT / "papers-digest.html"
    render_html(articles, output, args.days, translated, metadata)
    dated = output.with_name(f"{output.stem}-{date.today().isoformat()}{output.suffix}")
    if dated != output:
        render_html(articles, dated, args.days, translated, metadata)

    print(f"完成：{len(articles)} 篇 → {output}")
    print(f"                              {dated}")


if __name__ == "__main__":
    main()
