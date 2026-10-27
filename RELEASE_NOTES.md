# PAL v0.2.0 — Soft Daylight

QuestFlow 更名为 **PAL（Personal Activity Level）**，并更新为更轻盈的浅色界面。原有任务、成就、活跃度宝箱和奖励商店保持不变。

### 更新
- 应用显示名称、浏览器标题和 PWA 元信息统一改为 PAL。
- 深紫背景改为柔和的浅灰紫，面板改为白色；保留紫色作为交互强调色。
- fnOS Docker 常见问题修复：可配置 Python 镜像源、Alpine 软件源，修复 NAS 解压文件权限导致的启动失败。
- 附带首次安装、故障处理、安全说明、SQLite 数据备份说明及用户提供的历史 UI 截图（已移除内网地址和账户信息）。
- 出于安全考虑，未修改的默认安装密钥示例会被拒绝。

### 兼容性
不迁移 SQLite schema；仍使用 `questflow.db` 与 `questflow_questflow_data` 命名卷。升级前请备份数据，**不要执行 `docker compose down -v`**。

### 测试
Python 标准库端到端回归测试覆盖账号初始化、登录、任务计数、周期、金币、宝箱、商店、成就、历史排行榜、设置、数据库备份与归档。**未针对公网环境进行独立安全审计**。
