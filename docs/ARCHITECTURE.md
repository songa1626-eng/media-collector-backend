# 架构

## 目标

公开版只提供可独立运行的 Python 后端，不依赖 Electron，不包含桌面 UI、IPC、前端资产或产品授权逻辑。

## 组件

```text
HTTP client
    |
FastAPI (server/app.py)
    |
JobManager ---- concurrency / TTL / result model
    |
    +-- Douyin executor
    |      +-- URL parser
    |      +-- API client
    |      +-- downloader strategies
    |      +-- file/database helpers
    |
    +-- Weibo executor
           +-- Weibo provider
           +-- page/date iteration
           +-- image/HLS downloader
```

## 关键边界

- HTTP 层只负责验证、鉴权、调度和状态返回。
- 平台执行器分别持有登录态和分页规则，避免互相耦合。
- 每个抖音任务使用请求级配置副本，避免并发任务修改共享配置。
- 浏览器 CORS 默认关闭；API 默认只监听回环地址。
- 任务状态保存在内存，媒体文件保存在用户配置的目录。

## 扩展方式

新增平台时应实现独立执行器，并在 `server/app.py` 的调度入口注册。执行器返回统一统计字段：`total`、`success`、`failed`、`skipped`，附加信息放入任务的 `result` 字段。

## 当前限制

- 平台接口、反自动化规则和登录方式可能随时变化。
- 单元测试不等于真实账号、真实网络下的全历史端到端验证。
- 任务状态暂未持久化；服务重启后需要重新提交任务。
- 非 Windows 系统不会使用 Windows DPAPI 保存微博 Cookie，应优先使用环境变量或外部密钥管理。
