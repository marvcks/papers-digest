# Miniflux API 参考

> 完整的 Miniflux v2 REST API 使用指南
> 官方文档: https://miniflux.app/docs/api.html

## 目录

- [认证](#认证)
- [状态码](#状态码)
- [订阅源管理](#订阅源管理)
- [文章管理](#文章管理)
- [分类管理](#分类管理)
- [OPML 导入/导出](#opml-导入导出)
- [用户管理](#用户管理)
- [API 密钥管理](#api-密钥管理)
- [其他端点](#其他端点)

---

## 认证

API 支持两种认证方式：

### 1. HTTP Basic 认证

```bash
curl -u admin:password http://localhost:8080/v1/me
```

### 2. API Token 认证（推荐）

在 Settings > API Keys 创建令牌后使用：

```bash
curl -H "X-Auth-Token: your-token" http://localhost:8080/v1/me
```

---

## 状态码

| 状态码 | 说明 |
|--------|------|
| 200 | 成功 |
| 201 | 资源已创建/修改 |
| 204 | 资源已删除/修改（无返回内容） |
| 400 | 错误的请求 |
| 401 | 未授权（用户名/密码错误） |
| 403 | 禁止访问 |
| 500 | 服务器内部错误 |

### 错误响应格式

```json
{
    "error_message": "Some error"
}
```

---

## 订阅源管理

### 获取所有订阅源

```bash
GET /v1/feeds
```

**响应示例：**
```json
[
    {
        "id": 42,
        "user_id": 123,
        "title": "Example Feed",
        "site_url": "http://example.org",
        "feed_url": "http://example.org/feed.atom",
        "checked_at": "2017-12-22T21:06:03.133839-05:00",
        "etag_header": "...",
        "last_modified_header": "Sat, 23 Dec 2017 01:04:21 GMT",
        "parsing_error_message": "",
        "parsing_error_count": 0,
        "scraper_rules": "",
        "rewrite_rules": "",
        "crawler": false,
        "blocklist_rules": "",
        "keeplist_rules": "",
        "user_agent": "",
        "username": "",
        "password": "",
        "disabled": false,
        "ignore_http_cache": false,
        "fetch_via_proxy": false,
        "category": {
            "id": 793,
            "user_id": 123,
            "title": "Some category"
        },
        "icon": {
            "feed_id": 42,
            "icon_id": 84
        }
    }
]
```

### 获取单个订阅源

```bash
GET /v1/feeds/{feed_id}
```

### 获取分类下的订阅源

```bash
GET /v1/categories/{category_id}/feeds
```

### 创建订阅源

```bash
POST /v1/feeds
Content-Type: application/json

{
    "feed_url": "http://example.org/feed.atom",
    "category_id": 22
}
```

**必填字段：**
- `feed_url`: 订阅源 URL（字符串）

**可选字段：**
- `category_id`: 分类 ID（整数，2.0.49+ 版本可选）
- `username`: 订阅源用户名
- `password`: 订阅源密码
- `crawler`: 启用/禁用抓取器（布尔值）
- `user_agent`: 自定义 User Agent
- `scraper_rules`: 抓取规则
- `rewrite_rules`: 重写规则
- `blocklist_rules`: 屏蔽规则
- `keeplist_rules`: 保留规则
- `disabled`: 是否禁用
- `ignore_http_cache`: 是否忽略 HTTP 缓存
- `fetch_via_proxy`: 是否通过代理获取

**响应：**
```json
{
    "feed_id": 262
}
```

### 更新订阅源

```bash
PUT /v1/feeds/{feed_id}
Content-Type: application/json

{
    "title": "New Feed Title",
    "category_id": 22,
    "crawler": true
}
```

### 刷新订阅源

```bash
# 刷新单个订阅源（同步）
PUT /v1/feeds/{feed_id}/refresh

# 刷新所有订阅源（后台异步）
PUT /v1/feeds/refresh
```

### 删除订阅源

```bash
DELETE /v1/feeds/{feed_id}
```

### 标记订阅源文章为已读

```bash
PUT /v1/feeds/{feed_id}/mark-all-as-read
```

### 获取订阅源图标

```bash
# 通过订阅源 ID
GET /v1/feeds/{feed_id}/icon

# 通过图标 ID
GET /v1/icons/{icon_id}
```

**响应：**
```json
{
    "id": 262,
    "data": "image/png;base64,iVBORw0KGgoAAA....",
    "mime_type": "image/png"
}
```

### 获取订阅源统计

```bash
GET /v1/feeds/counters
```

**响应：**
```json
{
    "reads": {
        "1": 12,
        "3": 1
    },
    "unreads": {
        "1": 7,
        "3": 99
    }
}
```

---

## 文章管理

### 获取所有文章

```bash
GET /v1/entries?status=unread&direction=desc
```

**查询参数：**
| 参数 | 说明 |
|------|------|
| `status` | 文章状态：`read`、`unread`、`removed`（可重复使用） |
| `offset` | 偏移量 |
| `limit` | 返回数量 |
| `order` | 排序：`id`、`status`、`published_at`、`category_title`、`category_id` |
| `direction` | 方向：`asc` 或 `desc` |
| `before` | Unix 时间戳（之前） |
| `after` | Unix 时间戳（之后） |
| `published_before` | 发布时间之前（Unix 时间戳） |
| `published_after` | 发布时间之后（Unix 时间戳） |
| `changed_before` | 修改时间之前（Unix 时间戳） |
| `changed_after` | 修改时间之后（Unix 时间戳） |
| `before_entry_id` | 文章 ID 之前 |
| `after_entry_id` | 文章 ID 之后 |
| `starred` | 是否收藏（布尔值） |
| `search` | 搜索关键词 |
| `category_id` | 按分类筛选 |

**响应：**
```json
{
    "total": 10,
    "entries": [
        {
            "id": 888,
            "user_id": 123,
            "feed_id": 42,
            "title": "Entry Title",
            "url": "http://example.org/article.html",
            "comments_url": "",
            "author": "Foobar",
            "content": "<p>HTML contents</p>",
            "hash": "...",
            "published_at": "2016-12-12T16:15:19Z",
            "created_at": "2016-12-27T16:15:19Z",
            "status": "unread",
            "share_code": "",
            "starred": false,
            "reading_time": 1,
            "enclosures": null,
            "feed": { ... }
        }
    ]
}
```

### 获取单篇文章

```bash
GET /v1/entries/{entry_id}
```

### 获取订阅源的文章

```bash
GET /v1/feeds/{feed_id}/entries
```

支持与 `/v1/entries` 相同的查询参数。

### 获取分类的文章

```bash
GET /v1/categories/{category_id}/entries
```

支持与 `/v1/entries` 相同的查询参数。

### 批量更新文章状态

```bash
PUT /v1/entries
Content-Type: application/json

{
    "entry_ids": [1234, 4567],
    "status": "read"
}
```

### 切换收藏状态

```bash
PUT /v1/entries/{entry_id}/bookmark
```

### 更新文章

```bash
PUT /v1/entries/{entry_id}
Content-Type: application/json

{
    "title": "New title",
    "content": "Some text"
}
```

### 获取原文内容

```bash
GET /v1/entries/{entry_id}/fetch-content?update_content=true
```

**参数：**
- `update_content`: 是否更新数据库中的标题和内容（默认 `false`）

### 保存到第三方服务

```bash
POST /v1/entries/{entry_id}/save
```

### 获取附件

```bash
GET /v1/enclosures/{enclosure_id}
```

**响应：**
```json
{
    "id": 278,
    "user_id": 1,
    "entry_id": 195,
    "url": "https://example.org/file",
    "mime_type": "application/octet-stream",
    "size": 0,
    "media_progression": 0
}
```

### 更新附件

```bash
PUT /v1/enclosures/{enclosure_id}
Content-Type: application/json

{
    "media_progression": 42
}
```

---

## 分类管理

### 获取所有分类

```bash
GET /v1/categories
```

**包含统计信息：**
```bash
GET /v1/categories?counts=true
```

**响应：**
```json
[
    {
        "id": 1,
        "title": "All",
        "user_id": 1,
        "hide_globally": false,
        "feed_count": 7,
        "total_unread": 268
    }
]
```

### 创建分类

```bash
POST /v1/categories
Content-Type: application/json

{
    "title": "My category",
    "hide_globally": true
}
```

### 更新分类

```bash
PUT /v1/categories/{category_id}
Content-Type: application/json

{
    "title": "My new title",
    "hide_globally": false
}
```

### 删除分类

```bash
DELETE /v1/categories/{category_id}
```

### 刷新分类的所有订阅源

```bash
PUT /v1/categories/{category_id}/refresh
```

### 标记分类文章为已读

```bash
PUT /v1/categories/{category_id}/mark-all-as-read
```

---

## OPML 导入/导出

### 导出 OPML

```bash
GET /v1/export
```

返回 XML 格式的 OPML 文档。

### 导入 OPML

```bash
POST /v1/import

<opml version="1.0">...</opml>
```

请求体为 OPML 文件（XML 格式）。

**响应：**
```json
{
    "message": "Feeds imported successfully"
}
```

---

## 用户管理

### 获取当前用户

```bash
GET /v1/me
```

**响应：**
```json
{
    "id": 1,
    "username": "admin",
    "is_admin": true,
    "theme": "dark_serif",
    "language": "en_US",
    "timezone": "America/Vancouver",
    "entry_sorting_direction": "desc",
    "stylesheet": "",
    "google_id": "",
    "openid_connect_id": "",
    "entries_per_page": 100,
    "keyboard_shortcuts": true,
    "show_reading_time": true,
    "entry_swipe": true,
    "last_login_at": "2021-01-05T04:51:45Z"
}
```

### 获取所有用户（管理员）

```bash
GET /v1/users
```

### 获取单个用户（管理员）

```bash
# 通过用户 ID
GET /v1/users/{user_id}

# 通过用户名
GET /v1/users/{username}
```

### 创建用户（管理员）

```bash
POST /v1/users
Content-Type: application/json

{
    "username": "bob",
    "password": "test123",
    "is_admin": false
}
```

**可用字段：**
| 字段 | 类型 |
|------|------|
| `username` | string |
| `password` | string |
| `google_id` | string |
| `openid_connect_id` | string |
| `is_admin` | boolean |

### 更新用户（管理员）

```bash
PUT /v1/users/{user_id}
Content-Type: application/json

{
    "username": "joe",
    "theme": "dark_serif",
    "language": "fr_FR",
    "timezone": "Europe/Paris",
    "entry_sorting_direction": "asc",
    "entries_per_page": 50
}
```

### 删除用户（管理员）

```bash
DELETE /v1/users/{user_id}
```

### 标记用户所有文章为已读

```bash
PUT /v1/users/{user_id}/mark-all-as-read
```

---

## API 密钥管理

### 获取所有 API 密钥

```bash
GET /v1/api-keys
```

**响应：**
```json
[
    {
        "id": 1,
        "user_id": 1,
        "description": "My API Key",
        "token": "1234567890abcdef...",
        "created_at": "2023-10-07T03:52:50Z",
        "last_used_at": "2023-10-07T03:52:50Z"
    }
]
```

### 创建 API 密钥

```bash
POST /v1/api-keys
Content-Type: application/json

{
    "description": "My API Key"
}
```

### 删除 API 密钥

```bash
DELETE /v1/api-keys/{api_key_id}
```

---

## 其他端点

### 发现订阅源

```bash
POST /v1/discover
Content-Type: application/json

{
    "url": "http://example.org"
}
```

**响应：**
```json
[
    {
        "url": "http://example.org/feed.atom",
        "title": "Atom Feed",
        "type": "atom"
    },
    {
        "url": "http://example.org/feed.rss",
        "title": "RSS Feed",
        "type": "rss"
    }
]
```

**可选字段：**
- `username`: 订阅源用户名
- `password`: 订阅源密码
- `user_agent`: 自定义 User Agent
- `fetch_via_proxy`: 是否通过代理获取

### 获取版本信息

```bash
GET /v1/version
```

**响应：**
```json
{
    "version": "2.0.49",
    "commit": "69779e795",
    "build_date": "2023-10-14T20:12:04-0700",
    "go_version": "go1.21.1",
    "compiler": "gc",
    "arch": "amd64",
    "os": "linux"
}
```

### 清空历史

```bash
PUT /v1/flush-history
```

或

```bash
DELETE /v1/flush-history
```

返回 `202 Accepted` 状态码。

### 健康检查

```bash
# 检查服务和数据库（考虑 base path）
GET /healthcheck

# 仅检查服务是否运行
GET /liveness
# 或
GET /healthz

# 检查是否就绪（含数据库）
GET /readiness
# 或
GET /readyz
```

### 集成状态

```bash
GET /integrations/status
```

**响应：**
```json
{
    "has_integrations": true
}
```

---

## 使用示例

### Python 示例

```python
import requests

# 基本认证
auth = ('admin', 'password')

# 获取所有订阅源
response = requests.get('http://localhost:8080/v1/feeds', auth=auth)
feeds = response.json()

# 创建新订阅源
data = {
    'feed_url': 'https://example.com/feed.xml',
    'category_id': 1
}
response = requests.post('http://localhost:8080/v1/feeds', json=data, auth=auth)

# 获取未读文章
params = {'status': 'unread', 'limit': 10}
response = requests.get('http://localhost:8080/v1/entries', params=params, auth=auth)
entries = response.json()
```

### cURL 示例

```bash
# 获取所有订阅源
curl -u admin:password http://localhost:8080/v1/feeds

# 获取未读文章
curl -u admin:password "http://localhost:8080/v1/entries?status=unread"

# 创建新订阅源
curl -u admin:password -X POST http://localhost:8080/v1/feeds \
  -H "Content-Type: application/json" \
  -d '{"feed_url": "https://example.com/feed.xml", "category_id": 1}'

# 刷新所有订阅源
curl -u admin:password -X PUT http://localhost:8080/v1/feeds/refresh

# 导出 OPML
curl -u admin:password http://localhost:8080/v1/export > subscriptions.opml

# 导入 OPML
curl -u admin:-password -X POST http://localhost:8080/v1/import \
  --data-binary @subscriptions.opml
```

---

## 官方客户端库

### Go 客户端

```bash
go get -u miniflux.app/v2/client
```

### Python 客户端

```bash
pip install miniflux
```

```python
import miniflux

client = miniflux.Client("http://localhost:8080", "admin", "password")
feeds = client.get_feeds()
```
