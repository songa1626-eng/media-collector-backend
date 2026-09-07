# 贡献指南

## 开始

1. Fork 仓库并从 `main` 创建分支。
2. 安装开发依赖：`pip install -e ".[dev,browser]"`。
3. 保持平台抓取逻辑、下载逻辑和 HTTP 层职责分离。
4. 新行为先补测试，不提交真实网络响应中的个人信息。

## 代码要求

- Python 3.10+，异步 I/O 路径不得引入阻塞请求。
- `ruff check .` 无错误。
- `python -m pytest` 全量通过。
- 不提交 Cookie、Token、下载内容、数据库、浏览器配置和模型权重。
- 平台接口样例必须脱敏，并尽量缩减为测试所需字段。

## 提交信息

使用简洁英文，例如：

- `feat: add weibo date-range downloads`
- `test: cover HLS playlist validation`
- `docs: explain API key deployment`

## Pull Request

PR 应说明行为变化、兼容性影响、验证命令和真实平台验证是否执行。单元测试通过不能表述为“真实平台全量下载已验证”。

提交即表示你有权贡献代码，并同意贡献按本仓库适用许可证发布。
