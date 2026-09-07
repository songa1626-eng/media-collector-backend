# 部署

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[browser]"
python -m playwright install chromium
Copy-Item config.example.yml config.yml
media-collector-api --config config.yml
```

默认监听 `127.0.0.1:8000`。只有在明确配置鉴权、HTTPS 和防火墙后，才应使用 `--host 0.0.0.0`。

## Docker

```powershell
docker build -t media-collector-backend .
docker run --rm `
  -p 127.0.0.1:8000:8000 `
  -e MEDIA_COLLECTOR_API_KEY=YOUR_API_KEY_HERE `
  -v C:\downloads:/app/Downloaded `
  media-collector-backend `
  media-collector-api --host 0.0.0.0
```

不要把 Cookie 写入镜像。生产环境使用容器密钥、环境变量注入或受控的外部密钥管理服务。

## 公网部署最低要求

1. HTTPS；
2. `MEDIA_COLLECTOR_API_KEY`；
3. IP 白名单或用户鉴权；
4. 请求速率限制；
5. 独立下载目录和最小文件权限；
6. 日志脱敏与定期清理；
7. 保持 `MEDIA_COLLECTOR_ALLOW_CUSTOM_SAVE_DIRS=0`，不允许任务写入配置根目录以外。
8. 设置 `MEDIA_COLLECTOR_SOURCE_URL`，向网络用户提供当前部署版本的对应源码。

当前 API 面向可信用户和本地集成，不应直接作为匿名公共下载服务。
