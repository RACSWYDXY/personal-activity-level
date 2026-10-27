# 在 GitHub 发布 PAL

## 1. 建立仓库（账号持有者操作）

打开 <https://github.com/new>，仓库名称推荐 `personal-activity-level`，可见性选择 **Public**（若还没检查好，请先选 Private）。**不要勾选自动生成 README、.gitignore 或 LICENSE**，因为源码包中已经包含这些文件。

## 2. 上传源码（Windows/Linux 有 Git CLI 时）

解压源码包，进入包含 `app.py` 和 `docker-compose.yml` 的项目目录。在 PowerShell / Shell 内执行：

```bash
git init
git add .
git commit -m "Release PAL v0.2.0: light theme and docs"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/personal-activity-level.git
git push -u origin main
```

将 `YOUR_USERNAME` 换成你的 GitHub 用户名。推送时通过 GitHub 官方的浏览器登录或凭据管理器完成授权，**不要将密码或 Token 复制到代码和公开聊天**。

## 3. 发布版本

代码推送成功后，在仓库的 GitHub **Releases → Draft a new release** 中创建 `v0.2.0` 标签，标题 `PAL v0.2.0 — Soft Daylight`，将 `RELEASE_NOTES.md` 内容作为发布说明。可以选择附加源码 ZIP。

## 4. 检查

- GitHub Actions `Python regression tests` 是否通过。
- README 截图、LICENSE、`INSTALL-FNOS.md`、`DATABASE.md`、`SECURITY.md` 是否展示正常。
- **确认仓库中没有 `.env`、`questflow.db`、SQLite 备份、内网IP、密码或私人数据**。
- 对外宣传时说明项目为个人自托管原型，不适合直接公网开放。
