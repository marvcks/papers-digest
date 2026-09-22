#!/usr/bin/env python3
"""
Journal Article Searcher
从 Miniflux Journal 分类中搜索关键词相关的文章并使用 LLM 翻译
"""

import os
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup

# 加载 .env 文件
load_dotenv()


# 预设关键词组合
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


def get_miniflux_config():
    """获取 Miniflux 配置"""
    return {
        'url': os.getenv('MINIFLUX_URL', 'http://localhost:8080'),
        'username': os.getenv('MINIFLUX_USERNAME', 'admin'),
        'password': os.getenv('MINIFLUX_PASSWORD', 'test123'),
    }


def get_llm_config():
    """获取 LLM 配置"""
    api_key = os.getenv('OPENAI_API_KEY')
    base_url = os.getenv('OPENAI_BASE_URL')
    model_name = os.getenv('MODEL_NAME')

    if not all([api_key, base_url, model_name]):
        raise ValueError("缺少 LLM 配置，请检查 .env 文件中的 OPENAI_API_KEY, OPENAI_BASE_URL, MODEL_NAME")

    return {
        'api_key': api_key,
        'base_url': base_url,
        'model_name': model_name,
    }


def get_category_id(config, category_name="Journal"):
    """获取指定分类的 ID"""
    auth = (config['username'], config['password'])
    response = requests.get(f'{config["url"]}/v1/categories', auth=auth)
    response.raise_for_status()

    categories = response.json()
    for cat in categories:
        if cat.get('title') == category_name:
            return cat.get('id')
    return None


def search_articles(config, category_id, keywords_list, days=7, limit=50):
    """搜索文章（支持多个关键词，去重）"""
    auth = (config['username'], config['password'])
    base_url = config['url']

    # 计算 N 天前的时间戳
    after_timestamp = int((datetime.now() - timedelta(days=days)).timestamp())

    # 收集所有文章，使用 entry ID 去重
    all_entries = {}
    seen_ids = set()

    # 对每个关键词分别搜索
    for keyword in keywords_list:
        params = {
            'category_id': category_id,
            'search': keyword,
            'published_after': after_timestamp,
            'order': 'published_at',
            'direction': 'desc',
            'limit': limit,
        }

        response = requests.get(f'{base_url}/v1/entries', params=params, auth=auth)
        response.raise_for_status()

        data = response.json()
        entries = data.get('entries', [])

        for entry in entries:
            entry_id = entry.get('id')
            if entry_id not in seen_ids:
                seen_ids.add(entry_id)
                all_entries[entry_id] = entry

    # 按发布时间排序
    sorted_entries = sorted(
        all_entries.values(),
        key=lambda x: x.get('published_at', ''),
        reverse=True
    )

    return sorted_entries[:limit]


def strip_html(html_content):
    """移除 HTML 标签，保留纯文本"""
    if not html_content:
        return ''
    soup = BeautifulSoup(html_content, 'html.parser')
    return soup.get_text(separator=' ', strip=True)


def translate_with_llm(title, abstract, llm_config):
    """使用 LLM 翻译标题和摘要"""
    # 截断过长的摘要
    if len(abstract) > 2000:
        abstract = abstract[:2000] + '...'

    prompt = f"""请将以下学术论文的标题和摘要翻译为中文。要求：
1. 保持专业性和准确性
2. 使用标准的学术术语
3. 标题和摘要分别翻译

标题: {title}

摘要: {abstract}

请按以下格式输出：

## 标题
[翻译后的标题]

## 摘要
[翻译后的摘要]
"""

    # 处理 base_url，确保没有重复的斜杠
    base_url = llm_config['base_url'].rstrip('/')
    api_url = f"{base_url}/chat/completions"

    try:
        response = requests.post(
            api_url,
            headers={
                "Authorization": f"Bearer {llm_config['api_key']}",
                "Content-Type": "application/json"
            },
            json={
                "model": llm_config['model_name'],
                "messages": [
                    {"role": "system", "content": "你是一个专业的学术文献翻译助手，擅长将化学、物理、机器学习等领域的学术论文翻译为准确的中文。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3
            },
            timeout=60
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]

        # 解析返回的内容
        lines = content.split('\n')
        translated_title = ""
        translated_abstract = ""
        current_section = None

        for line in lines:
            line = line.strip()
            if line.startswith("## 标题") or line.startswith("标题:"):
                current_section = "title"
                continue
            elif line.startswith("## 摘要") or line.startswith("摘要:"):
                current_section = "abstract"
                continue

            if current_section == "title" and line:
                if translated_title:
                    translated_title += " " + line
                else:
                    translated_title = line
            elif current_section == "abstract" and line:
                if translated_abstract:
                    translated_abstract += " " + line
                else:
                    translated_abstract = line

        # 如果解析失败，返回原始内容
        if not translated_title:
            translated_title = title
        if not translated_abstract:
            translated_abstract = abstract

        return translated_title, translated_abstract

    except Exception as e:
        print(f"  [翻译失败: {str(e)}]", file=sys.stderr)
        return title, abstract


def write_header(file, keywords_list, days, total_entries):
    """写入文件头部"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    file.write(f"# 文献搜索结果 - {datetime.now().strftime('%Y-%m-%d')}\n\n")
    file.write("## 搜索概览\n")
    file.write(f"- 搜索关键词 ({len(keywords_list)} 个):\n")
    for kw in keywords_list:
        file.write(f"  - {kw}\n")
    file.write(f"- 分类: Journal\n")
    file.write(f"- 时间范围: 最近 {days} 天\n")
    file.write(f"- 找到文章: {total_entries} 篇\n")
    file.write(f"- 生成时间: {now}\n")
    file.write("\n---\n\n")
    file.write("## 文献列表\n\n")


def write_entry(file, index, entry, translated_title, translated_abstract):
    """写入单篇文章到文件"""
    title = entry.get('title', 'No Title')
    url = entry.get('url', '')
    feed_title = entry.get('feed', {}).get('title', 'Unknown')
    published_at = entry.get('published_at', '')

    # 处理发布时间
    if published_at:
        try:
            dt = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
            published_time = dt.strftime('%Y-%m-%d %H:%M')
        except:
            published_time = published_at
    else:
        published_time = 'Unknown'

    file.write(f"### {index}. {translated_title}\n\n")
    file.write(f"**原文标题**: {title}\n\n")
    file.write(f"**摘要**: {translated_abstract}\n\n")
    file.write(f"**来源**: {feed_title}\n")
    file.write(f"**发布时间**: {published_time}\n")
    file.write(f"**原文链接**: {url}\n")
    file.write("\n---\n\n")


def main():
    parser = argparse.ArgumentParser(description='Journal 文献搜索翻译器')
    parser.add_argument('keywords', nargs='*', help='搜索关键词（如不指定则使用 --preset）')
    parser.add_argument('--preset', type=str, choices=list(PRESET_KEYWORDS.keys()),
                        help='使用预设关键词组合: %(choices)s')
    parser.add_argument('--category', type=str, default='Journal', help='分类名称 (默认: Journal)')
    parser.add_argument('--days', type=int, default=7, help='搜索最近 N 天 (默认: 7)')
    parser.add_argument('--limit', type=int, default=50, help='最多返回 N 篇 (默认: 50)')
    parser.add_argument('--output', type=str, help='输出到文件')
    parser.add_argument('--list-presets', action='store_true', help='列出所有预设关键词组合')

    args = parser.parse_args()

    # 列出预设
    if args.list_presets:
        print("可用的预设关键词组合:")
        for name, keywords in PRESET_KEYWORDS.items():
            print(f"\n  {name}:")
            for kw in keywords:
                print(f"    - {kw}")
        return

    # 确定使用的关键词列表
    if args.preset:
        keywords_list = PRESET_KEYWORDS[args.preset]
        print(f"使用预设组合: {args.preset} ({len(keywords_list)} 个关键词)")
    elif args.keywords:
        keywords_list = args.keywords
    else:
        parser.print_help()
        print("\n错误: 必须指定关键词或使用 --preset")
        sys.exit(1)

    # 默认输出文件名
    if args.output is None:
        args.output = f"journal-papers-{datetime.now().strftime('%Y-%m-%d')}.md"

    try:
        # 获取配置
        miniflux_config = get_miniflux_config()
        llm_config = get_llm_config()

        # 获取分类 ID
        print(f"正在获取分类 '{args.category}' 的 ID...")
        category_id = get_category_id(miniflux_config, args.category)
        if not category_id:
            print(f"未找到分类: {args.category}")
            sys.exit(1)
        print(f"分类 ID: {category_id}")

        # 搜索文章
        print(f"正在搜索 {len(keywords_list)} 个关键词 (最近 {args.days} 天)...")
        entries = search_articles(miniflux_config, category_id, keywords_list, days=args.days, limit=args.limit)

        if not entries:
            print("未找到匹配的文章。")
            return

        print(f"找到 {len(entries)} 篇文章")

        # 打开文件开始写入
        with open(args.output, 'w', encoding='utf-8') as f:
            # 写入头部
            write_header(f, keywords_list, args.days, len(entries))

            # 一篇一篇翻译并写入
            for i, entry in enumerate(entries, 1):
                title = entry.get('title', 'No Title')
                content = entry.get('content', '')
                abstract = strip_html(content)

                # 提取摘要（前2000字）
                if len(abstract) > 2000:
                    abstract = abstract[:2000] + '...'

                print(f"[{i}/{len(entries)}] 正在翻译: {title[:50]}...", end='\r')

                # 使用 LLM 翻译
                translated_title, translated_abstract = translate_with_llm(title, abstract, llm_config)

                # 写入文件
                write_entry(f, i, entry, translated_title, translated_abstract)

        print(f"\n结果已保存到: {args.output}")

    except Exception as e:
        print(f"\n错误: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
