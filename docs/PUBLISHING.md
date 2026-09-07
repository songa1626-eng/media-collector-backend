# 发布到 GitHub

## 首次发布

1. 在 GitHub 新建空仓库，建议名称 `media-collector-backend`。
2. 不要自动生成 README、许可证或 .gitignore。
3. 复制仓库 HTTPS 地址。
4. 在本地仓库执行：

```powershell
git remote add origin REPOSITORY_URL
git push -u origin main
```

本地仓库已包含首个提交，不需要 `git add -A` 重新收集文件。

## 仓库设置

建议配置：

- Description：`Backend-only API for authorized Douyin and Weibo media archiving`
- Topics：`python`、`fastapi`、`douyin`、`weibo`、`media-archiver`
- 启用 Issues、Discussions、Private vulnerability reporting
- 将 `main` 设为保护分支，要求 CI 通过
- 不启用 GitHub Pages 托管下载文件

## v1.0.0 Release

1. 确认 CI 通过。
2. 创建标签：`git tag -a v1.0.0 -m "Release backend-only public API"`。
3. 推送标签：`git push origin v1.0.0`。
4. GitHub Release 附加 `dist/` 中的 wheel 和 sdist。
5. Release 说明引用 [CHANGELOG.md](../CHANGELOG.md) 和[安全合规说明](SECURITY_AND_COMPLIANCE.md)。

## 每次发布前

```powershell
ruff check .
python -m pytest
python -m build
git status --short
```

同时确认发布包中不存在 Cookie、`.env`、数据库、浏览器用户目录、下载内容、模型权重和 Electron UI。
