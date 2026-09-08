# MediaCollector Backend

[![CI](https://github.com/songa1626-eng/media-collector-backend/actions/workflows/ci.yml/badge.svg)](https://github.com/songa1626-eng/media-collector-backend/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-only-blue)](LICENSE)

中文 | [English](README.en.md)

一个面向**个人备份、已获授权内容迁移和合法归档**的统一媒体归档后端。

MediaCollector Backend 将抖音和微博的内容获取、分页、下载、任务调度、元数据保存和本地媒体存储统一到一个 FastAPI 服务中。它可以独立运行，也可以作为桌面客户端、内部工具或其他应用的后端。

> 本项目只适用于下载你本人发布的内容、已取得权利人明确授权的内容，或法律允许归档的内容。代码可用不等于平台授权。使用前请阅读[安全与合规说明](docs/SECURITY_AND_COMPLIANCE.md)。

## 项目定位

很多平台下载项目只解决“解析一个链接并下载文件”。本项目更关注长期归档场景：

- 用同一个 REST API 调度抖音和微博任务；
- 支持指定用户主页的全历史、日期范围和增量归档；
- 统一处理任务状态、重试、并发、去重、媒体文件和元数据；
- 让后端能力可以被 CLI、桌面端或自定义前端复用；
- 将平台适配器与通用任务管理分开，便于后续扩展其他内容源。

## 能力

- 抖音：单视频、图集、用户主页作品、点赞、合集、音乐和直播入口
- 微博：用户主页图片和视频、完整多图展开、HLS 视频转封装
- 范围：默认全历史，可指定页码或日期范围
- 任务：异步提交、状态查询、并发限制、任务结果统计
- 安全：默认仅监听 `127.0.0.1`，可选 API Key，CORS 默认关闭
- 部署：本地 Python、Docker、OpenAPI 文档

### 关于“全量下载”

`full_history` 表示程序会持续翻页，直到达到平台返回的边界或任务配置限制。它不代表能够获取平台隐藏、删除、私密、登录权限不足或接口未返回的内容，也不保证第三方平台接口长期稳定。

## 功能矩阵

| 模块 | 能力 |
| --- | --- |
| 抖音 | 单视频、图文、用户主页作品、点赞、合集、音乐和直播入口 |
| 微博 | 用户主页图片和视频、多图展开、视频资源下载和 HLS 转封装 |
| 归档范围 | 全历史、页码范围、日期范围和增量任务 |
| 任务系统 | 异步提交、状态查询、结果统计、并发控制和失败重试 |
| 存储 | 本地媒体文件、元数据和 SQLite 下载记录 |
| API | FastAPI、OpenAPI、Swagger UI、可选 API Key |
| 安全默认值 | 默认监听 `127.0.0.1`，CORS 默认关闭 |
| 部署 | Windows、Linux、macOS 本地运行，支持 Docker |

## 快速开始

要求 Python 3.10+。

```powershell
git clone https://github.com/songa1626-eng/media-collector-backend.git
Set-Location media-collector-backend

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e ".[browser]"
python -m playwright install chromium
Copy-Item config.example.yml config.yml
Copy-Item .env.example .env
media-collector-api --config config.yml
```

启动后：

- 健康检查：`http://127.0.0.1:8000/api/v1/health`
- Swagger：`http://127.0.0.1:8000/docs`
- OpenAPI：`http://127.0.0.1:8000/openapi.json`

Linux/macOS 使用 `python3 -m venv .venv`、`source .venv/bin/activate` 和 `cp` 完成同样的步骤。

### 下载抖音主页全部作品

```powershell
$body = @{
  provider = "douyin"
  url = "https://www.douyin.com/user/SEC_UID"
  modes = @("post")
  full_history = $true
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/downloads" `
  -ContentType "application/json" `
  -Body $body
```

### 下载微博主页全部媒体

```powershell
$body = @{
  provider = "weibo"
  url = "https://weibo.com/u/USER_ID"
  full_history = $true
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/downloads" `
  -ContentType "application/json" `
  -Body $body
```

查询结果：

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/jobs/JOB_ID"
```

更多字段与返回格式见 [API 文档](docs/API.md)，部署方式见[部署文档](docs/DEPLOYMENT.md)。

## 工作方式

```text
桌面端 / CLI / 自定义前端
              |
              | REST API
              v
        FastAPI 服务层
              |
          JobManager
       任务、并发、状态
          /          \\
         /            \\
   抖音执行器       微博执行器
       |                |
   分页与下载       分页与下载
         \\            /
          \\          /
          本地媒体与元数据
```

服务层只负责请求校验、鉴权、任务调度和状态返回；平台执行器分别处理登录态、分页规则和媒体资源。这样可以减少平台逻辑之间的耦合，也方便后续增加新的适配器。

## Cookie

- 抖音：优先使用 `DOUYIN_COOKIE` 环境变量，或运行 `python tools/cookie_fetcher.py`
- 微博：使用 `WEIBO_COOKIE` 环境变量；触发登录时需要 Playwright 浏览器
- 不要把 Cookie 写入 Issue、日志、截图或提交到 Git

`.gitignore` 已排除 `.env`、`config.yml`、Cookie、数据库、浏览器配置和下载目录。

## 仓库边界

本仓库只公开后端。Electron UI、桌面端资产、产品授权逻辑和商业分发代码不在本仓库中。

## 官方桌面端

本项目另有独立的桌面端 UI，用于提供更完整的可视化工作流和数据管理体验，包括：

- 抖音与微博的可视化下载入口；
- 指定人物主页归档；
- 多任务队列与进度查看；
- 本地媒体库和元数据浏览；
- 历史记录、筛选、搜索和重新下载；
- 更适合普通用户的配置与错误提示。

桌面端源代码、桌面端资产和产品授权逻辑不包含在本仓库中。开发者可以直接使用本后端 API，也可以基于本 API 开发自己的客户端。

官方桌面端属于独立的商业产品，收费内容可以包括安装包、持续更新、私有部署、技术支持和定制开发。后端本身仍按本仓库许可证发布；商业合作请先阅读[商业授权说明](COMMERCIAL_LICENSE.md)。

## 许可证与商业使用

本项目整体按 AGPL-3.0-only 发布，详见 [LICENSE](LICENSE)。复用的 MIT/Apache-2.0 代码保留在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 `LICENSES/` 中。

AGPL 允许商业使用，但修改后通过网络提供服务时通常需要向对应用户提供后端源代码。需要闭源后端授权、品牌化部署或技术支持，可参考 [商业授权说明](COMMERCIAL_LICENSE.md)。第三方组件仍受各自许可证约束。

商业 UI 不应复制本仓库的后端源码或第三方受限组件；如果将后端修改后与其他程序组合分发，请先复核 AGPL 和第三方许可证义务。

## 安全与合规边界

本项目仅适用于：

- 备份你本人发布的内容；
- 归档已经取得权利人明确授权的内容；
- 法律和平台规则允许的研究、迁移或内部使用场景。

不得用于绕过登录、付费、地域限制、访问控制或技术保护措施，不得批量收集或出售无权处理的个人信息，也不得将本项目部署成匿名开放的代下载服务。

使用者需要自行确认内容权利、平台协议、隐私保护、保存期限和再传播边界。详细说明见[安全与合规说明](docs/SECURITY_AND_COMPLIANCE.md)。

## 社区交流

欢迎通过 GitHub Issue 讨论 Bug、功能建议和部署问题。QQ 群入口将在群组建立并完成审核后补充到本节。

> 请勿在 Issue、QQ 群或截图中发送 Cookie、Token、密码、数据库文件或其他敏感信息。

二维码计划放在 `docs/assets/qq-group-qr.png`。未提供有效群号或二维码前，本仓库不会放置占位二维码。

## 文档

- [English README](README.en.md)
- [API](docs/API.md)
- [部署](docs/DEPLOYMENT.md)
- [安全与合规](docs/SECURITY_AND_COMPLIANCE.md)
- [架构](docs/ARCHITECTURE.md)
- [发布到 GitHub](docs/PUBLISHING.md)
- [贡献指南](CONTRIBUTING.md)
- [安全漏洞报告](SECURITY.md)
- [版本记录](CHANGELOG.md)

## 开发验证

```powershell
pip install -e ".[dev,browser]"
ruff check .
python -m pytest
python -m build
```
