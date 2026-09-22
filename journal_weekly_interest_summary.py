#!/usr/bin/env python3
"""
Generate weekly Journal interest summaries from Miniflux.
Output format: title + link + Chinese summary in Markdown.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


load_dotenv()


PRESET_KEYWORDS = {
    "ml-potential": [
        "machine learning potential",
        "neural network potential",
        "deep learning potential",
        "neural network potentials",
    ],
    "ml-methods": [
        "molecular dynamics",
        "QM/MM",
        "NNP/MM",
        "graph neural network",
        "Molecular mechanics",
    ],
    "all": [
        "molecular dynamics",
        "neural network potentials",
        "QM/MM",
        "NNP/MM",
        "graph neural network",
        "deep learning potential",
        "machine learning potential",
        "neural network potential",
        "Molecular mechanics",
    ],
}

USER_INTEREST_THEME_KEYWORDS = {
    "分子设计": [
        "molecular design",
        "inverse molecular design",
        "de novo molecular design",
        "drug design",
        "alchemical space exploration",
    ],
    "分子性质优化": [
        "molecular property optimization",
        "property optimization",
        "optimization",
        "screening",
        "high-throughput",
    ],
    "电子结构+神经网络": [
        "electronic structure neural network",
        "electronic structure information neural network",
        "electronic structure",
        "quantum modeling",
        "graph neural network",
    ],
}


def get_miniflux_config():
    return {
        "url": os.getenv("MINIFLUX_URL", "http://localhost:8080").rstrip("/"),
        "username": os.getenv("MINIFLUX_USERNAME", "admin"),
        "password": os.getenv("MINIFLUX_PASSWORD", "test123"),
    }


def get_llm_config():
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    model_name = os.getenv("MODEL_NAME")
    if not all([api_key, base_url, model_name]):
        raise ValueError(
            "Missing LLM config. Set OPENAI_API_KEY, OPENAI_BASE_URL, MODEL_NAME."
        )
    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model_name": model_name,
    }


def strip_html(html_content):
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    return soup.get_text(separator=" ", strip=True)


def get_category_id(config, category_name="Journal"):
    auth = (config["username"], config["password"])
    resp = requests.get(f"{config['url']}/v1/categories", auth=auth, timeout=30)
    resp.raise_for_status()
    for category in resp.json():
        title = category.get("title", "")
        if title == category_name or title.lower() == category_name.lower():
            return category.get("id"), category.get("title", category_name)
    return None, category_name


def search_entries(config, category_id, keywords_list, days=7, limit=200):
    auth = (config["username"], config["password"])
    after_ts = int((datetime.now() - timedelta(days=days)).timestamp())

    seen = {}
    matched_keywords_by_id = {}
    for keyword in keywords_list:
        params = {
            "category_id": category_id,
            "search": keyword,
            "published_after": after_ts,
            "order": "published_at",
            "direction": "desc",
            "limit": limit,
        }
        resp = requests.get(
            f"{config['url']}/v1/entries", params=params, auth=auth, timeout=30
        )
        resp.raise_for_status()
        for entry in resp.json().get("entries", []):
            entry_id = entry.get("id")
            if entry_id is None:
                continue
            if entry_id not in seen:
                seen[entry_id] = entry
                matched_keywords_by_id[entry_id] = set()
            matched_keywords_by_id[entry_id].add(keyword)

    sorted_entries = sorted(
        seen.values(), key=lambda x: x.get("published_at", ""), reverse=True
    )
    return sorted_entries, matched_keywords_by_id


def summarize_in_chinese(llm_config, title, content, url):
    clean_content = strip_html(content)
    if len(clean_content) > 2200:
        clean_content = clean_content[:2200] + "..."

    prompt = f"""请将下面论文内容概括成中文（2-3句话，准确、克制、保留关键信息）：

标题：{title}
链接：{url}
摘要或正文：{clean_content}
"""

    api_url = f"{llm_config['base_url']}/chat/completions"
    resp = requests.post(
        api_url,
        headers={
            "Authorization": f"Bearer {llm_config['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "model": llm_config["model_name"],
            "messages": [
                {"role": "system", "content": "你是一个专业的学术文献中文编辑。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def detect_themes_for_entry(entry, matched_keywords, theme_keyword_map):
    text = f"{entry.get('title', '')} {strip_html(entry.get('content', ''))}".lower()
    matched_keywords_lower = {kw.lower() for kw in matched_keywords}
    themes = []
    for theme, keywords in theme_keyword_map.items():
        # Priority 1: explicit Miniflux search hit keyword
        explicit_match = any(k.lower() in matched_keywords_lower for k in keywords)
        # Priority 2: fallback text contains keyword
        text_match = any(k.lower() in text for k in keywords)
        if explicit_match or text_match:
            themes.append(theme)
    return themes


def write_markdown(
    entries,
    summaries,
    output_path,
    category_name,
    days,
    preset,
    keywords,
    themes_by_entry_idx,
    grouped,
):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_str = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# {category_name} 最近{days}天兴趣文章汇总（{date_str}）",
        "",
        f"- 兴趣关键词预设：{preset}",
        f"- 关键词：{', '.join(keywords)}",
        f"- 文章数：{len(entries)}",
        f"- 生成时间：{now}",
        "",
        "---",
        "",
    ]

    if grouped:
        grouped_entries = {}
        for idx, (entry, summary) in enumerate(zip(entries, summaries), start=1):
            themes = themes_by_entry_idx.get(idx, [])
            bucket_names = themes or ["其他相关"]
            for theme_name in bucket_names:
                grouped_entries.setdefault(theme_name, []).append((idx, entry, summary))

        for theme_name, items in grouped_entries.items():
            lines.append(f"## {theme_name}（{len(items)} 篇）")
            lines.append("")
            for idx, entry, summary in items:
                title = entry.get("title", "No Title")
                url = entry.get("url", "")
                lines.append(f"### {idx}. {title}")
                lines.append("")
                lines.append(f"- 链接：{url}")
                lines.append(f"- 中文概括：{summary}")
                lines.append("")
    else:
        for idx, (entry, summary) in enumerate(zip(entries, summaries), start=1):
            title = entry.get("title", "No Title")
            url = entry.get("url", "")
            lines.append(f"## {idx}. {title}")
            lines.append("")
            lines.append(f"- 链接：{url}")
            lines.append(f"- 中文概括：{summary}")
            lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def main():
    parser = argparse.ArgumentParser(description="Weekly Journal interest summary")
    parser.add_argument("--category", default="Journal", help="Miniflux category name")
    parser.add_argument(
        "--preset",
        default="all",
        choices=list(PRESET_KEYWORDS.keys()),
        help="Interest keyword preset",
    )
    parser.add_argument(
        "--profile",
        choices=["none", "user-interest"],
        default="user-interest",
        help="Keyword profile: none or user-interest",
    )
    parser.add_argument(
        "--extra-keyword",
        action="append",
        default=[],
        help="Extra keyword to include (can be specified multiple times)",
    )
    parser.add_argument(
        "--group-by-theme",
        action="store_true",
        help="Group output by interest themes",
    )
    parser.add_argument("--days", type=int, default=7, help="Lookback days")
    parser.add_argument("--output", help="Output markdown file path")
    args = parser.parse_args()

    try:
        miniflux = get_miniflux_config()
        llm = get_llm_config()

        category_id, category_title = get_category_id(miniflux, args.category)
        if not category_id:
            raise ValueError(f"Category not found: {args.category}")

        preset_keywords = PRESET_KEYWORDS[args.preset]
        profile_keywords = []
        theme_keyword_map = {}
        if args.profile == "user-interest":
            theme_keyword_map = USER_INTEREST_THEME_KEYWORDS
            for kw_list in USER_INTEREST_THEME_KEYWORDS.values():
                profile_keywords.extend(kw_list)

        raw_keywords = preset_keywords + profile_keywords + args.extra_keyword
        # Deduplicate while preserving order.
        keywords = list(dict.fromkeys(k.strip() for k in raw_keywords if k.strip()))
        entries, matched_keywords_by_id = search_entries(
            miniflux, category_id, keywords, days=args.days
        )
        if not entries:
            print("No matching entries found.")
            return

        output = args.output or f"journal-interest-{datetime.now().strftime('%Y-%m-%d')}.md"

        summaries = []
        total = len(entries)
        for i, entry in enumerate(entries, start=1):
            title = entry.get("title", "No Title")
            print(f"[{i}/{total}] Summarizing: {title[:80]}...")
            try:
                summary = summarize_in_chinese(
                    llm, title, entry.get("content", ""), entry.get("url", "")
                )
            except Exception as exc:
                summary = f"概括失败：{exc}"
            summaries.append(summary)

        themes_by_entry_idx = {}
        for i, entry in enumerate(entries, start=1):
            entry_id = entry.get("id")
            matched_keywords = matched_keywords_by_id.get(entry_id, set())
            themes = detect_themes_for_entry(entry, matched_keywords, theme_keyword_map)
            themes_by_entry_idx[i] = themes

        write_markdown(
            entries,
            summaries,
            output,
            category_title,
            args.days,
            args.preset,
            keywords,
            themes_by_entry_idx,
            args.group_by_theme,
        )
        print(f"Saved: {output}")

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
