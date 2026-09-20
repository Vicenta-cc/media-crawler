# Audit Agent R2 配套爬虫（2026-09-20）

## 冻结版本

- 本仓库：`https://github.com/Vicenta-cc/media-crawler`。
- 当前运行代码：`efafe3186400b1020955c6acfdc201235b84a2d8`。
- 冻结分支：`codex/douyin-v2-pagination-dedupe-20260914`。
- 冻结标签：`douyin-pagination-fix-20260916`。
- 配套应用：`509d922af4d23dec9d488e7f1e3b95042b0c4bf1`，分支 `codex/r2-interactive-login-20260918`。
- 历史 8027 爬虫快照：`6d2c85bcb02fd7dd1e30822e0cb2103bb9b42f39`，标签 `frozen/douyin-8027-crawler-20260916`；仅供追溯。
- 默认入口：`main`，内容对齐冻结代码与文档，保留原 main 提交历史；精确运行仍检出冻结提交。
- 本文档分支：`codex/r2-frozen-handoff-20260920`，从当前冻结代码派生，不是新业务 release。

`efafe318` 包含分页边界和评论回复保留修复，以及此前账号绑定持久化 CloakBrowser 集成。原本地目录有未跟踪的实验脚本、测试与锁文件；这些不是冻结版本的一部分，也没有随本次同步上传。

## 获取运行代码

在新目录执行：

```bash
git clone --branch codex/douyin-v2-pagination-dedupe-20260914 https://github.com/Vicenta-cc/media-crawler.git media-crawler-r2
cd media-crawler-r2
git switch --detach efafe3186400b1020955c6acfdc201235b84a2d8
git status --short
```

## 与应用的启动关系

R2 由应用 runtime 统一调用爬虫，不需要另外开启本仓库 WebUI。应用通过固定爬虫 Python、源码路径、账号身份和 Profile 路径启动采集进程。

- 云端源码：`/mnt/datadisk0/xhs-audit-agent-r2-abc/releases/r2-abc-20260916/crawler`。
- 云端爬虫解释器：`/mnt/datadisk0/xhs-audit-agent-r2-abc/runtime/venv-crawler/bin/python`，Python 3.11.6。
- 配套 Node：`/usr/bin/node`，v24.15.0。
- CloakBrowser：0.5.10；新登录 Profile 的 Chromium 为 145.0.7632.109.2，已有 Profile 保留其版本。
- Profile：`/mnt/datadisk0/xhs-audit-agent-r2-abc/runtime/browser-profiles`，按采集账号持久化，不能在任务之间任意共用或覆盖。

应用通过 `MEDIACRAWLER_DIR`、`CRAWLER_LOGIN_PYTHON`、`CRAWLER_BROWSER_PROFILE_ROOT` 等配置接入；调用子进程还需稳定账号 ID 与对应认证状态，不能只运行通用 `main.py --platform dy` 就假定完成应用集成。

账号交互登录由配套应用提供，Linux 上依赖 Xvfb、libX11/libXtst、浏览器与中文字体。短信输入成功后还要验证后台无头浏览器复用。不要把登录态、Cookie、加密密钥和 Profile 提交到仓库。

完整运行文件与操作说明在 [应用 main 的启动归档](https://github.com/Vicenta-cc/audit-agent-demo/tree/main/deploy/r2-frozen-20260918)。当前 Git 仓库不是包含全部依赖和账号数据的容器镜像；pyproject.toml/requirements.txt 用于依赖声明，重建后仍需重新验收。

## 更新与验证

从精确冻结提交派生功能分支，在独立环境安装依赖并验证分页、评论、暂停恢复、登录态复用和账号锁。不要直接把本地实验目录覆盖到服务器。更新后由应用 runtime 校验提交与源码指纹；锁文件是运行状态，不是发布代码。

旧 8027 的 71 passed 是 2026-09-16 历史验收结果，不代表本次重新运行。9 月 18 日配套应用发布曾验证真实扫码短信登录后的无头采集，返回 15 条结果。本次仅同步版本、文档和启动说明，没有执行真实采集。
