# API 文档

基础地址：`http://127.0.0.1:8000`

## 提交任务

`POST /api/v1/downloads`

```json
{
  "provider": "douyin",
  "url": "https://www.douyin.com/user/SEC_UID",
  "modes": ["post"],
  "save_dir": "",
  "start_date": null,
  "end_date": null,
  "start_page": 1,
  "end_page": 0,
  "full_history": true,
  "download_pinned": false
}
```

| 字段 | 说明 |
|---|---|
| `provider` | `douyin` 或 `weibo` |
| `url` | 目标主页或该平台支持的媒体 URL |
| `modes` | 抖音模式：`post`、`like`、`mix`、`music`、`collect`、`collectmix` |
| `save_dir` | 可选输出目录；默认必须位于 `config.yml` 的 `path` 内 |
| `start_date` / `end_date` | ISO 日期，例如 `2026-01-01` |
| `start_page` / `end_page` | 微博分页范围；`end_page=0` 表示不设上限 |
| `full_history` | 默认为 `true`；微博为 `false` 且未指定结束页时只处理起始页 |
| `download_pinned` | 是否下载抖音置顶作品 |

响应：

```json
{
  "job_id": "6d69fbfd65fe",
  "provider": "douyin",
  "status": "pending",
  "url": "https://www.douyin.com/user/SEC_UID"
}
```

旧路径 `POST /api/v1/download` 暂时保留，但已标记为废弃。

## 查询任务

- `GET /api/v1/jobs/{job_id}`：查询一个任务
- `GET /api/v1/jobs`：列出当前进程保留的任务

状态为 `pending`、`running`、`success` 或 `failed`。服务重启后内存任务列表会清空，但已下载文件不会删除。

## 健康检查

`GET /api/v1/health` 不需要 API Key。

`GET /api/v1/about` 返回版本、许可证和部署者设置的源码地址，也不需要 API Key。

## API Key

设置 `MEDIA_COLLECTOR_API_KEY` 后，除健康检查外的所有 `/api/` 请求必须包含：

```text
X-API-Key: YOUR_API_KEY_HERE
```

API Key 只适合本地或受控网络的基础保护。公网部署仍应使用 HTTPS、反向代理、访问控制和速率限制。
