# 安全与合规

## 使用边界

本项目用于个人备份、已获授权的内容迁移和合法归档。不得用于：

- 绕过登录、付费、地域、访问控制或技术保护措施；
- 未经授权批量收集、监控、画像或出售个人信息；
- 下载、传播或商业利用无权使用的图片、视频、音乐和肖像；
- 对平台发起高频请求、干扰服务或规避风控；
- 共享账号、Cookie、Token 或其他登录凭证；
- 构建匿名开放的“代下载”服务。

抖音当前用户协议明确限制未经书面许可的爬虫、模拟下载以及内容商业使用。微博规则和适用法律也可能随时更新。使用者必须自行取得必要授权，并承担数据来源、处理目的、保存期限和再传播的合规责任。

参考：

- [抖音用户服务协议](https://www.douyin.com/agreements/?id=6773906068725565448)
- [微博服务使用协议](https://weibo.com/signup/v5/protocol)
- [GitHub Acceptable Use Policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies)

## 凭证

- 只通过环境变量、本地受保护文件或密钥管理服务提供凭证。
- 不在 API 响应、日志、Issue、截图和测试样例中输出真实凭证。
- 泄露后立即在平台退出全部会话并重新登录。
- 公开部署时禁用交互式浏览器登录，改用受控运维流程更新凭证。

## 网络

- 默认监听 `127.0.0.1`。
- CORS 默认关闭；只为明确的可信来源设置 `MEDIA_COLLECTOR_CORS_ORIGINS`。
- 非本机访问必须设置 API Key，并在反向代理启用 HTTPS 和速率限制。

## 发布前检查

```powershell
git status --short
git grep -n -I -E "(Cookie:|sessionid=|WEIBO_COOKIE=.+|DOUYIN_COOKIE=.+|API_KEY=.+)"
git ls-files
```

确认不存在 `.env`、真实配置、Cookie、数据库、浏览器用户目录、模型权重、下载文件和 UI 资产。

本文不是法律意见。商业上线前建议由熟悉数据合规、版权和平台协议的律师复核。
