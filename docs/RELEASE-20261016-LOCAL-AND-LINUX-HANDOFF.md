# 2026-10-16：本地启动与 Linux 部署交接

更新时间：2026-10-16（Asia/Shanghai）。本文件随 `release/2026-10-16` 交接，供 Audit Agent 配套 MediaCrawler 使用。

**已确认：在 macOS 隔离测试环境中完成抖音登录、身份核对、登录态复用、关键词搜索和 1 条内容落盘。Linux 正式切换、完整调查业务和其他平台尚未因此获得验收。** 今晚应先完成目标 Linux 环境的部署门槛及小样本验收，再逐步恢复采集。

## 1. 精确基线与取码

| 组件 | 实际测试的源码提交 | 基线 tag |
| --- | --- | --- |
| [audit-agent-demo](https://github.com/Vicenta-cc/audit-agent-demo) | `1dadd4757b478b716ca0c0f4b7f649545d82a94f` | `release-baseline-20261016-audit` |
| [media-crawler](https://github.com/Vicenta-cc/media-crawler) | `7bd9d117cd1b0753990ba754c58b8c12de8f4ca5` | `release-baseline-20261016-media` |

Media 基线合并的两个父提交分别是：

- 本地媒体重试基线：`4c70ac42ef3b8e71259fd9342032bed5fec75a34`。
- 原远端 release：`1f168f90e35d367bfac18e0bcb6ffaa7df51d404`。

本次交付将原远端 media release 从第二个父提交 fast-forward 更新为“已测合并 + 本文”，禁止 force push。包含本文的纯文档提交不改变上表中的已测源码；tag 保持固定，部署清单还应记录实际交付提交、依赖及资产摘要。分支名会移动，部署前用 `git ls-remote origin refs/heads/release/2026-10-16` 核对交付提交，不能只凭分支名判断部署的是哪份代码。

本次测试 Mac 的源码位置如下；这些路径只用于找回本机现场，远端不能照搬：

| 位置（相对当前用户主目录） | 分支与用途 |
| --- | --- |
| `~/.codex/worktrees/geographic-integration/xhs-audit-agent-demo` | audit `codex/geographic-integration`，测试时 HEAD 为上述 audit 基线 |
| `~/.codex/worktrees/media-release-integration/media-crawler` | media `codex/media-release-integration-20261016`，测试时 HEAD 为上述 media 基线；随后仅追加交接文档 |
| `~/Documents/Codex/projects/xhs-audit-agent-demo` | 原开发副本 `codex/backend-development`，存在未提交开发改动，未作为本次运行源码；本次仅借用它的 audit venv 和私有环境配置 |

不要将“原开发目录在本地”理解为“该目录就是已测基线”，也不要为对齐基线而 reset/覆盖原开发修改。

以下命令用于**新目录准备精确源码**，不在正在运行的生产 checkout 中执行切换：

```bash
git clone --branch release/2026-10-16 git@github.com:Vicenta-cc/audit-agent-demo.git audit-agent-demo
git -C audit-agent-demo checkout --detach 1dadd4757b478b716ca0c0f4b7f649545d82a94f
git clone --branch release/2026-10-16 git@github.com:Vicenta-cc/media-crawler.git media-crawler
git -C media-crawler checkout --detach 7bd9d117cd1b0753990ba754c58b8c12de8f4ca5
git -C audit-agent-demo rev-parse HEAD
git -C media-crawler rev-parse HEAD
```

HTTPS 地址为 `https://github.com/Vicenta-cc/media-crawler.git`。若本机网络阻断 GitHub SSH 22，可为单次 Git 命令使用 `-c core.sshCommand='ssh -p 443 -o Hostname=ssh.github.com -o BatchMode=yes'`；这只解决 Git 传输，不等于解决采集代理。无需因此更改服务器全局 SSH 或代理配置。

## 2. 本次实际验证了什么

| 项目 | 结果及边界 |
| --- | --- |
| 前端/API | 本机 `http://127.0.0.1:3499/crawler-accounts`，API `127.0.0.1:8499` |
| 登录 | 登录 V2 显示成功，核对平台身份，账号存储状态为 `active` |
| 网络 | 保留原 TUN；采集账号绑定独立香港 HTTP 代理 `127.0.0.1:17891`；浏览器和请求通道的出口观测一致 |
| 采集 | Audit 的 `MediaCrawlerAdapter.run_search` 调用配套 media，复用该账号 Profile，关键词“风景” |
| 范围 | 1 条内容、并发 1、每分钟最多 2 条；关闭评论与媒体下载 |
| 结果 | 进程退出码 0，adapter 返回 1 条，JSONL 实际有 1 行；作品 ID `7684858294619785134` |
| Redis | 本次路径未要求本机 Redis，已实际跑通 |
| 运行版本 | audit Python `3.12.10`；media Python `3.11.15`；Camoufox SDK `0.5.8`；Playwright `1.62.0`；浏览器 `156.0.1-beta.39` |

脱敏记录见 [本次冒烟结果](releases/2026-10-16-local-smoke.json)。原始证据在测试 Mac 的 `~/.codex/runtime/media-merge-login-smoke/`：`smoke-result.json`、`login-success.png`、`outputs/crawl-1792142847/` 下的 stdout/stderr 和 JSONL。这些文件没有随本文上传；不得将原始账号库、Cookie、Profile、代理凭据或完整私密日志提交到 Git。

本次未验证：评论、视频/图片下载、作者采集、其他平台、完整 worker 调查/审核/报告/问答、多用户权限、Linux 显示与进程隔离、重启恢复和长时间运行。先前单测/回归中的 fixture、Redis 或其他浏览器缺失，不能据此推导“这条登录链路无法本地运行”；本次 1 条实测也不能替代那些回归范围。

## 3. 本地启动前的准备

1. 固定上述两个源码提交；分别准备 audit/media 的依赖环境，安装对应操作系统的浏览器及资源。只安装 `camoufox` Python 包不代表浏览器已安装。
2. 固定绝对解释器路径；保留 `.venv/bin/python` 启动器本身，不将符号链接解析成系统 Python。`CRAWLER_LOGIN_PYTHON` 同时用于登录和 adapter 采集运行器选择。
3. 准备独立测试数据、outputs、账号库、调度库、Profile；不复用或清空其他环境的数据。本次临时借用了原开发目录的 audit venv，新环境应独立安装并保存依赖清单。
4. 确认前端/API/代理端口空闲。账号页面需要前端和 API；直接 adapter 冒烟不需要完整调查 worker。完整调查业务仍需要独立 worker 和模型、ffmpeg、ASR 等依赖。
5. 明确账号路由；先验证代理可达、浏览器与 HTTP 出口一致，再登录。登录和采集期间不切节点、不切账号绑定。

### 3.1 已验证的本地配置组合

下列是**本机隔离冒烟的配置模板**，不是生产环境文件。替换所有路径，存到仓库外受限文件（例如权限 `0600`），不要直接覆盖已有 `.env`。

```dotenv
# 仅用于本机 loopback 隔离冒烟；权限/额度测试仍须 required。
APP_AUTH_MODE=disabled
CORS_ALLOW_ORIGINS=http://127.0.0.1:3499
CRAWLER_LOGIN_V2_ENABLED=true
CRAWLER_LOGIN_V2_ORIGINS=http://127.0.0.1:3499
CRAWLER_PROXY_ENABLED=true
CRAWLER_SAFETY_ENABLED=true
CRAWLER_DIRECT_DEGRADE_ENABLED=false
CRAWLER_PROXY_ECHO_URL=https://ipv4.wtfismyip.com/text

MEDIACRAWLER_DIR=/ABS/media-crawler
CRAWLER_LOGIN_PYTHON=/ABS/media-crawler/.venv/bin/python
PYTHONPATH=/ABS/audit-agent-demo
XHS_AUDIT_DATA_DIR=/ABS/local-smoke/data
XHS_AUDIT_OUTPUTS_DIR=/ABS/local-smoke/outputs
REQUEST_SCHEDULER_DB=/ABS/local-smoke/data/request_scheduler.sqlite3
CRAWLER_CAMOUFOX_STATE_ROOT=/ABS/local-smoke/data/camoufox_state
CRAWLER_BROWSER_PROFILE_ROOT=/ABS/local-smoke/data/crawler_browser_profiles
CRAWLER_CAMOUFOX_BROWSER_VERSION=156.0.1-beta.39
CRAWLER_CAMOUFOX_EXECUTABLE=/ABS/OS-SPECIFIC/camoufox
```

`CRAWLER_CAMOUFOX_STATE_ROOT` 是新引擎状态根；`CRAWLER_BROWSER_PROFILE_ROOT` 保留逻辑 Profile/锁语义，两者应分开。账号代理地址通过账号代理资源绑定进入路由，不是把 `17891` 填入任意全局 `HTTP_PROXY` 就完成配置。保留 HTTP/HTTPS loopback 的可达性。

本次出口探测 URL 用于获得稳定 IPv4 观测；它是已测配置，不是必须使用的公共服务。远端应验证所选探测服务可达、响应格式符合契约，并使浏览器和 HTTP 通道观测同一个出口。

### 3.2 API、前端及最小验收顺序

本机已有运行配置位于 `~/.codex/runtime/media-merge-login-smoke/`，其中 `run_audit_api.py` 保存上述环境组合，`run_crawl_smoke.py` 是本次 adapter 冒烟脚本。该目录是本机操作记录，仓库 clone 不会生成它。已有服务运行时不要重复启动。

新机器准备好私有配置和依赖后，可以用以下**本地命令模板**启动；先将路径替换为实际值。配置文件必须是可信、可由 shell 加载的赋值文件；不要 source 外部未知文件。

```bash
# 终端 1：加载独立配置，再启动本机 API。
cd /ABS/audit-agent-demo
set -a
. /ABS/private/local-smoke.env
set +a
/ABS/audit-agent-demo/.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8499
```

```bash
# 终端 2：已经安装前端依赖的 Audit_assistant。
cd /ABS/audit-agent-demo/Audit_assistant
VITE_API_PROXY_TARGET=http://127.0.0.1:8499 ./node_modules/.bin/vite --host 127.0.0.1 --port 3499 --strictPort
```

1. 打开 `/crawler-accounts`，创建/选择测试采集账号，绑定实际代理资源。
2. 打开登录 V2；仅在出现二维码时扫码，平台要求验证时由账号持有人处理。
3. 验证页面登录成功、平台身份正确、存储状态 `active`。
4. 通过正常 Audit 任务或 adapter 使用相同账号、相同 Profile、相同路由搜索 1 条，关闭评论和媒体下载。
5. 同时检查退出码、返回内容数量、实际 JSONL、账号状态和路由日志；只有 UI 显示成功还不够。

若使用 adapter 独立脚本，须提供有效的 execution/owner 及 `route_context`，并在 `finally` 关闭 bundle。本次脚本用 `acquire_route_execution(..., owner_id='local-smoke', account_store=...)` 获取 bundle，再传给 `run_search(route_context=bundle, ...)`。该 owner 仅适用于此免应用鉴权的本机冒烟；正式任务必须沿用真实任务及用户授权上下文。不要伪造 FD/ACK、补一个任意 owner 来绕过生产授权，也不要用裸 `main.py` 代替应用集成链路验收。

### 3.3 TUN 和 Redis 的结论

- TUN 本身不能证明代码有 bug。此次保留 Codex 使用的原 TUN 网络，给采集建立独立、无 TUN 的 HTTP 代理进程，绑定 Mac 的 `en0`，固定香港节点；普通流量与采集流量分别测得不同出口。该方案只在专用代理进程运行时有效。
- 香港节点与 `en0` 都不是代码要求。换电脑或服务器时重新选择可达、稳定、符合账号路由策略的出口，禁止直接照搬网卡名和节点凭据。
- 本次账号绑定代理与 SQLite 调度/状态链路不依赖 Redis。media 的传统动态代理池在 `proxy/base_proxy.py` 中创建 Redis cache；启用那条功能或运行相关测试时另行配置。Redis 不是“本地必须、服务器不用”，也不是相反。

## 4. 本次故障与排查顺序

| 现象 | 已确认原因/优先检查 | 正确处理 |
| --- | --- | --- |
| 登录启动报 `PROXY_CONFIG_INVALID` | 本次曾开启账号代理但关闭 safety，生成的 snapshot 不满足 managed login bootstrap 契约 | 对本次代理模式同时开启 proxy/safety，重启隔离 API，创建新登录会话；保留授权校验 |
| 独立 adapter 冒烟报同一错误 | 本次脚本最初未处于任务上下文，owner 为空，`RouteOperation` 拒绝 | 使用有效执行上下文/route bundle；这不是浏览器网络故障 |
| `PROXY_EXIT_CHANGED` | 比较浏览器、HTTP、缓存观测；检查 IPv4/IPv6、节点切换、代理实际路由 | 统一出口后按账号流程重新校验；不能删除观测记录或关闭保护硬过 |
| API 重启后旧窗口会话不存在 | 旧登录会话已失效 | 关闭旧窗口，从账号重新发起登录 |
| API 可用但调查任务不执行 | 独立 worker、任务配置、准入或依赖未就绪 | 检查 worker 与 API 同一配置；不靠反复登录修复 |
| Profile 不兼容/账号身份不符 | 引擎、OS、版本、generation 或平台身份不匹配 | 走受控迁移/重新登录；不复制旧 Chromium Cookie 或删除锁试运气 |

错误码可能对应多种原因，需结合日志位置判断，不能将所有 `PROXY_CONFIG_INVALID` 或平台网络错误都归因于 TUN/合并。

## 5. 远端不能照搬的设置

| 本地配置/操作 | Linux 正式部署要求 |
| --- | --- |
| `APP_AUTH_MODE=disabled` | 多用户正式环境使用 `required`，保留原认证数据库与权限；不能用关闭鉴权解决测试失败 |
| HTTP 测试时可能放宽 Secure Cookie | 正式 HTTPS 使用 `APP_AUTH_COOKIE_SECURE=true`；不复制本地放宽配置 |
| `127.0.0.1:17891`、`en0`、Mac TUN/HK 节点 | 服务器的 loopback 指向服务器自身；使用服务器可用的路由。无需为 Codex 复制全机 TUN，也不能移植 Mac 网卡配置 |
| `3499/8499`、Vite 开发服务 | 使用正式构建和现有托管入口；保留经审核的端口/relay，不因本次验证改写线上拓扑 |
| 本地 Origin | `CRAWLER_LOGIN_V2_ORIGINS` 填真实前端 HTTPS Origin，精确协议/域名/端口，无路径、尾斜杠或通配符；正确转发登录 WebSocket |
| `/Users/...`、本机借用的 venv、Mac 浏览器 | 用 Linux 路径、独立环境、匹配平台的浏览器和资产；venv 应在最终绝对位置构建 |
| Mac/headless Profile | 不作为 Linux headed Profile 交付；按迁移记录保留原账号 ID、代理绑定、锁和安全状态，在目标环境重新核对身份 |
| 本地 `data`/outputs/测试账号库 | 不覆盖生产数据；不把生产 DB 复制给浏览器进程以使检查通过；升级回退不能用旧 DB 覆盖新业务数据 |
| 终端、调试 wrapper、临时环境变量 | 正式 systemd 服务固定环境与工作目录；API/worker 读取同一审核配置，浏览器子进程使用独立的更小白名单 |
| 为排障清空数据、删锁、换节点、关闭保护 | 正式环境先停准入、证明进程退出、备份及按流程恢复；不删除锁或绕过保护来消除错误 |

## 6. 今晚 Linux 部署步骤与停止条件

本次 Mac 实测不能批准生产切换。配套 audit 的 [Camoufox 发布契约](https://github.com/Vicenta-cc/audit-agent-demo/blob/1dadd4757b478b716ca0c0f4b7f649545d82a94f/deploy/camoufox/README.md)明确写有 **G0 安全审核尚未关闭**。部署者须核对实际候选资产对应的审核和收据；若仍未完成，保持采集维护状态，先在已批准的隔离 Linux 环境验收。不要将示例 manifest 当成已通过记录，也不要编造 `passed:true`。

1. **冻结交付。** 记录 App/Crawler 实际完整 SHA、前端 dist、解释器、依赖锁、浏览器及资源 SHA256。SDK/Playwright 固定为本基线要求。使用 Linux 匹配资产及系统库、字体、可信 CA；生产不执行 `camoufox fetch`，不临时升级依赖碰运气。
2. **预演运行边界。** 按配套 `deploy/camoufox` 执行 owner/child preflight 和 Linux 冒烟；使用专用非 root 浏览器用户、私有 headed Xvfb、正确目录权限和 FD/ACK 通道。账号/owner/scheduler DB 不对浏览器用户开放；不关闭 sandbox 或证书校验。
3. **准备维护窗口。** 保持 `CRAWLER_BROWSER_MAINTENANCE=true`，停止相关正式 API/worker 单元，并证明旧浏览器、node、Xvfb 和任务进程组已退出。旧版本不一定识别新维护开关，因此设置开关不能替代实际停写证明。
4. **备份并迁移。** 在一致停写窗口备份业务/账号/认证/调度库、必要密钥及关闭的 Profile，检查备份完整性。按迁移服务生成记录，保留账号 ID、用户归属、稳定锁、代理绑定和安全状态；不全库覆盖、不直接导入 Chromium Cookie。
5. **按正式入口恢复业务。** 使用已审核 manifest、真实资产 hash 和切换收据，保持现有 systemd/relay 拓扑。API/worker 读取同份业务配置；真实前端 Origin 和登录 WebSocket 均检查通过。采集先保持维护状态。
6. **目标环境验收。** 按迁移及维护流程完成授权账号重新登录和身份核对；先 1 条搜索，再小量评论与媒体下载，再测作者/其他平台。确认失败、取消、路由停机和 Profile 稳定性符合预期。不能为了测试绕过维护门槛。
7. **完整业务与恢复采集。** 验证真实用户登录 → worker 执行 → 图片/视频/评论审核 → 报告 → 问答 → 定向重启后再问答；模型与真实 ASR 需单独验证，health 200 不够。完成审核、迁移、健康及维护解除收据后，按契约恢复准入和实际重启验证。

任一资产/安全门槛、数据库备份、账号身份、出口一致性、Linux 浏览器边界或迁移步骤不通过，则停止扩大范围，保留证据并维持采集暂停。首次迁移失败按发布契约修复 Camoufox 或保持业务维护模式；不自动回退旧引擎。未切换时保留原运行环境，已产生新业务数据后不以旧库回滚。

## 7. 交接时应留下的记录

- 实际交付提交与上述已测源码的关系、App/Crawler 配对、前端构建及依赖/浏览器资产摘要。
- 服务名、实际工作目录、解释器、配置文件位置、数据路径、代理绑定策略；只记录密钥位置，不记录值。
- 本次 Mac 冒烟结果，以及目标 Linux 的安全审核、预检、备份/迁移、登录/采集、完整业务与重启验证结果；未运行项明确标注。
- 故障时保持采集暂停的方法、备份位置、负责的部署人和实际验收时间。

配套参考：[audit 当前启动交接](https://github.com/Vicenta-cc/audit-agent-demo/blob/1dadd4757b478b716ca0c0f4b7f649545d82a94f/docs/runtime/CURRENT-STARTUP.md)、[统一运行入口](https://github.com/Vicenta-cc/audit-agent-demo/blob/1dadd4757b478b716ca0c0f4b7f649545d82a94f/docs/runtime/UNIFIED-RUNTIME.md)。本仓库下方 README 中的 R2/Cloak/CDP 描述保留作历史或上游通用资料，不作为本次 Camoufox 集成部署依据。
