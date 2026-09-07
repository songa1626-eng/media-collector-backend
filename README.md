# MediaCollector Backend

一个不包含 Electron UI 的独立 Python 后端，用统一 REST API 调度抖音和微博媒体归档任务。

> 本项目只适用于下载你本人发布的内容、已取得权利人明确授权的内容，或法律允许归档的内容。代码可用不等于平台授权。使用前请阅读[安全与合规说明](docs/SECURITY_AND_COMPLIANCE.md)。

## 能力

- 抖音：单视频、图集、用户主页作品、点赞、合集、音乐和直播入口
- 微博：用户主页图片和视频、完整多图展开、HLS 视频转封装
- 范围：默认全历史，可指定页码或日期范围
- 任务：异步提交、状态查询、并发限制、任务结果统计
- 安全：默认仅监听 `127.0.0.1`，可选 API Key，CORS 默认关闭
- 部署：本地 Python、Docker、OpenAPI 文档

## 快速开始

要求 Python 3.10+。

```powershell
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

## Cookie

- 抖音：优先使用 `DOUYIN_COOKIE` 环境变量，或运行 `python tools/cookie_fetcher.py`
- 微博：使用 `WEIBO_COOKIE` 环境变量；触发登录时需要 Playwright 浏览器
- 不要把 Cookie 写入 Issue、日志、截图或提交到 Git

`.gitignore` 已排除 `.env`、`config.yml`、Cookie、数据库、浏览器配置和下载目录。

## 仓库边界

本仓库只公开后端。Electron UI、桌面端资产、产品授权逻辑和商业分发代码不在本仓库中。

## 许可证与商业使用

本项目整体按 AGPL-3.0-only 发布，详见 [LICENSE](LICENSE)。复用的 MIT/Apache-2.0 代码保留在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 `LICENSES/` 中。

AGPL 允许商业使用，但修改后通过网络提供服务时通常需要向对应用户提供后端源代码。需要闭源后端授权、品牌化部署或技术支持，可参考 [商业授权说明](COMMERCIAL_LICENSE.md)。第三方组件仍受各自许可证约束。

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
