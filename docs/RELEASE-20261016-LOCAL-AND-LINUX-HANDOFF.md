# 2026-10-16：本地启动与 Linux 部署交接

更新时间：2026-10-19（Asia/Shanghai）。本文件随 Media 的 `release/2026-10-16` 交接，供 Audit Agent 配套 MediaCrawler 使用。10 月 16 日的真实联网验证与后续离线修复记录分别保留，不将旧测试扩大为新版本或 Linux 的完整验收。

**已核实的远端起点是 Audit `709c392` + Media `32729e8`。本轮 Audit 修复已提交为 `f918dd6`（发布入口 dotenv 隔离）及 `be5ca8f`（历史分析记录链接），均已通过离线测试及两路独立 review；候选业务源码固定 `be5ca8ffab13975b41080dae5e33060d4690bd23`，后续仅文档提交须另记录完整 SHA。Media 业务代码仍为 `7bd9d11`。2026-10-19 已开始部署准备，推送及云端盘点结果以执行收据为准；尚未切换生产。**

## 1. 精确基线与取码

| 组件/记录 | 精确提交 | 状态与用途 |
| --- | --- | --- |
| Audit 历史已推送起点 | `709c3929c5c584d3b596b83b813f7555e14c913d` | 包含 `9a0ded4` 累积修复、原帖发布时间修复及本地验收记录；发布入口 `.env` 补丁须另行提交后重新冻结 |
| Audit 远端 release（补丁推送前核对） | `709c3929c5c584d3b596b83b813f7555e14c913d` | 已核实 `release/2026-10-16` 指向；`db5d57f` 保留为历史版本，不再作为当前取码目标 |
| Audit 本轮候选业务源码 | `be5ca8ffab13975b41080dae5e33060d4690bd23` | 包含 dotenv 隔离 `f918dd6`、前端历史轮次补丁 `be5ca8f`；本次源码离线测试、类型检查、构建及独立 review 已通过，Linux 验收待执行 |
| Media 已推送文档交付 | `32729e8b06cbf11f6aa153085852e3a44c1d05d8` | 远端 release 本次核对指向；相对 `7bd9d11` 没有业务源码改动 |
| Media 业务代码基线 | `7bd9d117cd1b0753990ba754c58b8c12de8f4ca5` | 固定 tag `release-baseline-20261016-media`；后续 Audit 修复没有修改本仓库业务代码 |
| Media 首次文档交付 | `0e00cfd2cf3e4c814e2761a928af4daecb2591cd` | 比 `7bd9d11` 仅增加 README、本文及冒烟 JSON；本次更新在此基础上只修改本文。实际部署记录本次文档提交的完整 SHA |
| Audit 早期联网冒烟基线 | `1dadd4757b478b716ca0c0f4b7f649545d82a94f` | 历史 tag `release-baseline-20261016-audit`，仅用于还原早期验证；不作为当前候选取码目标 |

`709c392` 包含 `9a0ded4`，相对 `db5d57f` 有 13 个后续提交；源码关系已检查，**不等于已经使用该完整版本重新完成真实登录、采集和报告验收**。本文保留历史 tag 指向，不移动 tag、不重写提交，也不把后续文档提交算作 Media 业务修复。

Media 基线合并的两个父提交分别是：

- 本地媒体重试基线：`4c70ac42ef3b8e71259fd9342032bed5fec75a34`。
- 原远端 release：`1f168f90e35d367bfac18e0bcb6ffaa7df51d404`。

Media release 已从第二个父提交 fast-forward 更新为“已测合并 + 文档”，无需再从 `4c70ac4` 重新合并。分支名会移动，部署前分别查询两仓库的 release 指向，并将实际完整 SHA、依赖及资产摘要冻结到部署清单。不得 force push 或仅凭分支名判断代码版本。

本次测试 Mac 的源码位置如下；这些路径只用于找回本机现场，远端不能照搬：

| 位置（相对当前用户主目录） | 分支与用途 |
| --- | --- |
| `~/.codex/worktrees/geographic-integration/xhs-audit-agent-demo` | 早期 audit 冒烟 `codex/geographic-integration`，当时为 `1dadd47` |
| `~/.codex/worktrees/new-media-app-integration/xhs-audit-agent-demo` | audit 累积集成分支 `codex/new-media-app-integration-20261016`，本次核对已提交起点为 `709c392`；本轮修复已分别提交为 `f918dd6` / `be5ca8f`，后续文档提交另行记录 |
| `~/.codex/worktrees/exit-refresh-release/xhs-audit-agent-demo` | 出口修复发布副本，HEAD `db5d57f` |
| `~/.codex/worktrees/media-release-integration/media-crawler` | media `codex/media-release-integration-20261016`，业务代码 `7bd9d11`，随后仅追加交接文档 |
| `~/Documents/Codex/projects/MediaCrawler-camoufox-runtime` | 后续出口过期实测使用的 media 运行副本，源码 `7bd9d11`；浏览器/模型资源在其独立 venv 中 |
| `~/Documents/Codex/projects/xhs-audit-agent-demo` | 原开发副本 `codex/backend-development`，存在未提交开发改动，未作为本次运行源码；本次仅借用它的 audit venv 和私有环境配置 |

不要将“原开发目录在本地”理解为“该目录就是已测基线”，也不要为对齐基线而 reset/覆盖原开发修改。

以下命令固定**本轮候选业务源码**；先确认远端已包含该提交，再执行取码。不在运行中的生产 checkout 执行切换。Media 命令固定已推送的文档起点，实际候选须另记录包含本次交接更新的最终文档提交。两仓库均显式固定 SHA，`cat-file` 失败时停止，不静默改用旧 tag：

```bash
set -e
git clone --branch release/2026-10-16 git@github.com:Vicenta-cc/audit-agent-demo.git audit-agent-demo
git -C audit-agent-demo cat-file -e 'be5ca8ffab13975b41080dae5e33060d4690bd23^{commit}'
git -C audit-agent-demo checkout --detach be5ca8ffab13975b41080dae5e33060d4690bd23
git clone --branch release/2026-10-16 git@github.com:Vicenta-cc/media-crawler.git media-crawler
git -C media-crawler cat-file -e '32729e8b06cbf11f6aa153085852e3a44c1d05d8^{commit}'
git -C media-crawler checkout --detach 32729e8b06cbf11f6aa153085852e3a44c1d05d8
git -C audit-agent-demo rev-parse HEAD
git -C media-crawler rev-parse HEAD
```

已核对 Media `7bd9d11..32729e8` 仅涉及 README、本文和冒烟 JSON；`32729e8` 单独只修改本文。本轮文档修订尚未包含在 `32729e8`，若要随发布交付，提交后记录新的文档 SHA。若后续出现业务源码改动，重新核对配套关系并验收。生产构建从最终冻结的完整 SHA 取码，不在构建中追随浮动分支；历史 `db5d57f` 或 `1dadd47` 不能静默替代新起点。

HTTPS 地址为 `https://github.com/Vicenta-cc/media-crawler.git`。若本机网络阻断 GitHub SSH 22，可为单次 Git 命令使用 `-c core.sshCommand='ssh -p 443 -o Hostname=ssh.github.com -o BatchMode=yes'`；这只解决 Git 传输，不等于解决采集代理。无需因此更改服务器全局 SSH 或代理配置。

## 2. 本次实际验证了什么

### 2.1 早期真实联网冒烟（2026-10-16）

本节保留原始结果，配对为 Audit `1dadd47` + Media `7bd9d11`。

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

### 2.2 出口过期修复与真实闲置验证（2026-10-16）

代码纳入 Audit `db5d57f`，Media 仍为 `7bd9d11`。真实联网验证使用 `8ebdb25` 集成工作区中的出口修复补丁；之后提交时保留 `a8868b8` 状态换行并加入补丁、测试和记录。提交后的独立发布副本另跑针对性回归及前端构建，不将当时联网环境描述为已经运行 `9a0ded4`。

- 原问题：本地免鉴权的新 M3 调查先判账号可用性，过期出口记录导致提前失败，没有机会进入原有复查流程；“待校验”又与真实保护混在一起显示。
- 修复：在该本地入口的动态校验前调用现有授权线路复查；API 只读区分 `EXIT_OBSERVATION_STALE` 与真实保护，前端显示“采集网络待校验，任务启动时自动检查”。登录状态另支持换行。
- 未改变出口 TTL（300 秒），未关闭账号授权、账号/出口租约、风险暂停或出口变化保护，未启用直连降级。不是要求用户每五分钟扫码，也不是后台固定每五分钟轮询每个账号。
- 新账号实际扫码并确认；19:18:00 开始计时，19:23:31 启动验证，真实闲置 **331.59 秒**。代理与共享观测均自然过期；worker 入口复查成功、出口未变，复用登录态搜索“风景”，19:24:52 完成并落盘 1 条，无需重新扫码。
- 作品 ID `7693069731972813818`，JSONL SHA256 `23ecc519dd85b27ca3315af1793c0e6bffe2f8a8e39ddc978ad1abd2ed310246`。验证页为本地 `3501`、API `8501`，与早期 `3499/8499` 区分。
- 补齐了本机 Camoufox 固定模型资源；按当前 SDK pin 逐文件核对哈希后安装，`is_pinned()` 通过。这是运行环境修复，没有修改 Media 源码或绕过校验。
- 针对性 worker/API 回归 19 项通过；发布副本选定累积回归 383 项通过，前端构建及新增状态用例通过。各组范围不同，不相加为全量验收。既有 fixture、默认爬虫路径、前端 mock 问题及多进程重跑停滞的边界见原记录，不能宣称全量测试全通过。

验证脚本走真实 worker 配置校验、线路探测和 Media 搜索，但显式替换模型供应商校验为测试回调，并在校验后直接调用 adapter；**没有验收完整前端调查、真实模型分析或报告**。该新增预校验仅作用于免鉴权、准入关闭、启用抖音采集的新任务路径；正式鉴权仍走既有调度刷新，旧任务恢复分类不能套用此次结果。

完整记录：[Audit 出口修复与验证](https://github.com/Vicenta-cc/audit-agent-demo/blob/db5d57f8d4f2f88f6f7cb7ceb2bf4acaa85b6bb9/docs/EXIT-REFRESH-FIX-20261016.md)。本地原始证据目录为 `~/.codex/runtime/exit-refresh-verification-20261016/`，不上传账号库、Profile 或私密日志。

### 2.3 Audit 后续修复至 `9a0ded4`（离线证据）

这些改动均在 Audit，Media 业务基线不变。以下依据该提交内的记录和提交历史整理，本次文档更新没有重新执行这些测试：

| 变化 | 当前行为与部署关注点 |
| --- | --- |
| 地域采集/恢复及启动恢复边界 | API 启动不接管仍由 worker 持有的任务；线路刷新沿用授权和风险保护，恢复命令持久化，重复恢复/取消受执行权约束 |
| 地域模型格式与草案解析 | 格式纠错最多三次，耗尽保留技术性待判定并保留报告门禁；省市组合确定性解析，不靠模糊匹配扩大地域 |
| 必要媒体交接与崩溃恢复 | **先补齐并验证媒体、持久化交付，再释放采集线路并异步审核**；后续采集可并行。旧“等待上一条审核释放线路”方案已被替代，不能按旧方案部署或排障 |
| 审核与文件依赖 | 图片需完整解码，视频需本地 ffmpeg 完整解码；审核读取来源、策略和哈希绑定的本地文件。缺失/损坏时延期，不能跳过媒体验收或重置历史任务硬过 |
| `16fca47` / `9a0ded4` | 恢复阻断原因投影一致；正在正常地域采集的任务不误显示恢复警告 |
| 其他累积修复 | 报告时间本地化、按请求补全账号历史问询及提示一致性；不等于真实问询已在 Linux 验收 |

已记录的离线结果：媒体就绪、真实本地视频交接及 SQLite 崩溃恢复最终组合 **58 passed、0 skipped**；草案地域解析与工具链组合记录 **166 passed**；其他预筛、恢复和前端检查见各自文档。分组有重叠且验证发生在不同修复阶段，不累计为 `9a0ded4` 的全量回归数字。外部模型、爬虫和探测使用替身；本地真实 ffmpeg 解码不等于真实平台媒体下载通过。

配套记录位于 Audit `9a0ded4` 的 `docs/runtime/`：

- `GEOGRAPHIC-MEDIA-HANDOFF-20261017.md`：最终媒体交接、崩溃恢复和验收边界；优先于旧线路等待方案。
- `GEOGRAPHIC-RECOVERY-FIX-20261016.md`：恢复、出口刷新及早期修复记录。
- `GEOGRAPHIC-PREFILTER-FALLBACK-20261017.md`、`DRAFT-REGION-RESOLUTION-20261017.md`：预筛纠错、待判定和草案解析。

上述记录已随 Audit `709c392` 进入远端 release；本机 API/worker 配置仍固定 `16fca47`，不能把源码推送当作已加载或已完成 Linux 验收。`709c392` 的 `docs/runtime/LOCAL-3499-ACCEPTANCE-20261017.md` 补录了本地大火调查两条审核和已发布报告，但其完整问询、跨报告及全部修复重启验收仍有缺口。部署者应检查精确源码中的记录，并在目标环境重新验证 `APP_AUTH_MODE=required` 下的登录、采集、媒体交接、报告及重启恢复。

## 3. 本地启动前的准备

1. 固定上述两个源码提交；分别准备 audit/media 的依赖环境，安装对应操作系统的浏览器及资源。只安装 `camoufox` Python 包不代表浏览器已安装。
2. 固定绝对解释器路径；保留 `.venv/bin/python` 启动器本身，不将符号链接解析成系统 Python。`CRAWLER_LOGIN_PYTHON` 同时用于登录和 adapter 采集运行器选择。
3. 准备独立测试数据、outputs、账号库、调度库、Profile；不复用或清空其他环境的数据。本次临时借用了原开发目录的 audit venv，新环境应独立安装并保存依赖清单。
4. 确认前端/API/代理端口空闲。账号页面需要前端和 API；直接 adapter 冒烟不需要完整调查 worker。完整调查业务仍需要独立 worker 和模型、ffmpeg、ASR 等依赖。
5. 明确账号路由；先验证代理可达、浏览器与 HTTP 出口一致，再登录。登录和采集期间不切节点、不切账号绑定。

### 3.0 依赖与浏览器资产预检（本地和 Linux 都适用）

需要分别准备：**Python 依赖、匹配 OS 的完整浏览器发行包、固定指纹模型**。当前固定 SDK `0.5.8`、Playwright `1.62.0`、浏览器 `156.0.1-beta.39`；SDK pin 为 `model-2/2026`，包括 `fingerprint-network.json.zst`、`values.dat.zst`、`values.json.zst`，按 SDK 自带摘要校验。保留发行包布局和版本元数据，不能只复制一个可执行文件。

模型应安装到实际 Media Python 对应的 fpgen 数据目录，在构建环境校验并按 SDK 的固定模型安装流程保留标记和元数据；不要仅复制几个文件后假定完整初始化已完成。部署前使用与服务相同的解释器及可见权限做无网络检查（此检查不启动浏览器，也不替代 Linux 冒烟）：

```bash
/ABS/media-venv/bin/python - <<'PY'
import os
from importlib.metadata import version
from camoufox.fpgen_model import PIN, is_pinned
assert version("camoufox") == "0.5.8"
assert version("playwright") == "1.62.0"
assert not os.environ.get("FPGEN_MODEL_URL"), "custom fingerprint model is not allowed"
assert PIN["tag"] == "model-2/2026"
assert is_pinned(), "install the pinned model during artifact construction"
print("SDK, Playwright and fingerprint model checks passed")
PY
```

Media `tools/camoufox_browser.py` 在新建指纹时会拒绝模型缺失/哈希不符或自定义 `FPGEN_MODEL_URL`，因此这类资源问题可能表现为登录浏览器未启动、没有二维码。模型和浏览器在构建阶段准备并冻结摘要；生产不能靠首次请求联网下载、换模型或修改校验逻辑修复。

**当前依赖清单存在未修复差异：** `requirements.txt` 为 Pillow `12.1.0`，`pyproject.toml` / `uv.lock` 为 `9.5.0`，本机后续实测 venv 安装的是 `12.1.0`。两种安装入口不等价；本文没有修改依赖文件，也不声称较旧版本一定出错。部署前应统一选定并验证完整安装清单，生成 App/Crawler 各自传递依赖齐全的哈希锁及 Linux wheelhouse，执行 `pip check` 并记录实际版本。不要把普通 requirements 的范围依赖或现有 uv.lock 直接当成已验证的离线发布锁。Python `>=3.11` 是源码约束；原实测 Media `3.11.15`、Audit `3.12.10` 不代表其他解释器或 Linux 构建组合已通过验收。

补核发现两入口还存在 `aiosqlite`（固定 0.21.0 / 下界 0.21.0）、`pyhumps`（固定 3.8.0 / 下界 3.8.0）、`opencv-python`（无版本约束 / 下界 4.11.0.86）的差异；pyproject 还声明 pre-commit、websockets、asyncpg。不能只改 Pillow 就声称两入口等价。当前只完成核对，未擅自升级或删依赖。

Audit 的 `requirements-hermes.txt` 包含固定 Git 提交依赖，须在构建环境先制成 wheel，再与所有传递依赖锁定摘要。现有 `build.py` 用调用它的 Python 创建两套 venv；Linux 预演以明确的目标 Python 3.11 执行，不能误用系统默认解释器或搬运 Mac venv。正式构建仍要求两套独立完整哈希锁、匹配 Linux x86_64 的 wheelhouse 以及 `pip check`；这些产物本轮尚未制作。

本地测试覆盖、数据库与候选参数的核对表，以及发布入口补丁/验收边界见 Audit 本轮新增 `docs/runtime/DEPLOYMENT-READINESS-20261019.md`；该文件提交前不宣称已在远端可取。生产参数尚未从云端盘点，不将默认值或本地小样本值直接批准为正式值。

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
| `EXIT_OBSERVATION_STALE` / 采集网络待校验 | 过期的是线路出口观测，不等于账号退出或 IP 已改变 | 当前入口按授权流程自动复查；复查失败或真实风险仍拦截，不改 TTL、不靠反复扫码 |
| 无二维码、浏览器未启动，日志提示 pinned model / browser missing | 实际服务解释器的模型/浏览器资源不齐或摘要、版本不符 | 按 3.0 校验并补齐构建资产；不能只检查另一套 venv 是否可用 |
| Linux 报 DISPLAY 缺失 | Media Linux 运行要求 headed，由应用 owner 入口管理私有显示环境 | 检查私有 Xvfb 与真实子进程权限，不指望裸跑 Media 自动启动 Xvfb |
| 地域媒体交付延期 | 必需文件未完整解码、缺少来源/策略证据、ffmpeg 或磁盘条件不满足 | 检查媒体交接及资源配置，保留持久化证据；不略过审核、删清单或重置任务 |
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

## 6. Linux 部署步骤与停止条件

本次 Mac 实测不能批准生产切换。配套 Audit 已推送起点的 [Camoufox 发布契约](https://github.com/Vicenta-cc/audit-agent-demo/blob/709c3929c5c584d3b596b83b813f7555e14c913d/deploy/camoufox/README.md)明确写有 **G0 安全审核尚未关闭**；已核对截至 `709c392` 该门槛仍未在文档中关闭。本次没有连接服务器，不能确认是否另有匹配资产的审核和收据。部署者须核对；若仍未完成，保持采集维护状态，先在已批准的隔离 Linux 环境验收。不要将示例 manifest 当成已通过记录，也不要编造 `passed:true`。

1. **冻结交付。** 以 Audit 已推送 `709c392` 加本轮经审查发布入口补丁重新冻结候选，核对与 Media 已推送 `32729e8`（业务 `7bd9d11`）的配套关系；记录 App/Crawler 实际完整 SHA、前端 dist、解释器、统一后的完整依赖锁、浏览器及模型资源 SHA256。SDK/Playwright 固定为本基线要求。使用 Linux 匹配资产及系统库、字体、可信 CA；生产不执行 `camoufox fetch`，不临时升级依赖碰运气。更新部署 manifest 的源码、前端与资源摘要，不能沿用旧版本的成功收据。
2. **预演运行边界。** 当前 `deploy/camoufox/control.py` 只接受 **Linux x86_64**。执行 owner/child preflight 和 Linux 冒烟；使用专用非 root 浏览器用户、私有 headed Xvfb、正确目录权限和真实 FD/ACK 通道。Media Linux 路径没有 DISPLAY 会拒绝启动，不自动创建 Xvfb；不要裸跑 `main.py` 替代 Audit 集成。账号/owner/scheduler DB 不对浏览器用户开放；不关闭 sandbox 或证书校验。现有预检还要求文件描述符软限制至少 4096、`/dev/shm` 总容量至少 1 GiB、状态目录可用磁盘至少 3 GiB，以及浏览器共享库完整；这些是最低检查，不是业务容量测算。
3. **准备维护窗口。** 保持 `CRAWLER_BROWSER_MAINTENANCE=true`，停止相关正式 API/worker 单元，并证明旧浏览器、node、Xvfb 和任务进程组已退出。旧版本不一定识别新维护开关，因此设置开关不能替代实际停写证明。
4. **备份并迁移。** 在一致停写窗口备份业务/账号/认证/调度库、必要密钥及关闭的 Profile，检查备份完整性。按迁移服务生成记录，保留账号 ID、用户归属、稳定锁、代理绑定和安全状态；不全库覆盖、不直接导入 Chromium Cookie。
5. **按正式入口恢复业务。** 使用已审核 manifest、真实资产 hash 和切换收据，保持现有 systemd/relay 拓扑。API/worker 读取同份业务配置及数据路径，`MEDIACRAWLER_DIR` 指向冻结源码，`CRAWLER_LOGIN_PYTHON` 指向 Media 专用解释器。`APP_AUTH_MODE=required`，登录 V2 开启、真实前端 Origin 和登录 WebSocket 均检查通过。新入口清理继承环境；本轮 Audit 补丁还在 Settings 导入处禁止 managed release 从源码 `.env` 补值，不依赖 dotenv 的版本开关。模型、ASR、ffmpeg 等业务必需项必须通过审核的私有配置逐项纳入；确认最终源码已包含该补丁，不能假定旧 shell/.env 会被继承。采集先保持维护状态。
6. **候选受控放行与目标环境验收。** 候选与正式入口、数据、Profile、账号和线路隔离；完成真实迁移/基础健康记录后，按维护工具的收据契约只解除候选维护，重启候选 API/worker 并验证准入，再完成授权账号重新登录和身份核对。`maintenance.py` 只改 manifest，不热更新进程；普通登录/采集验收须在候选受控解除维护后进行，不能为测试解除旧生产保护；既有受控迁移登录仍可使用匹配账号、owner、migration、operation 的正式授权例外，禁止伪造身份。正式切换必须等候选验收及回退演练完成；按迁移及维护流程先 1 条搜索，再真实闲置超过当前出口 TTL 后发起下一次授权采集，检查自动复查、失败仍拦截且不要求无故重扫。随后小量评论与媒体下载，再测作者/其他平台。对最终 Audit 候选专门验证必要图片/视频补齐与完整解码、清单交付后释放采集线路、审核和后续采集交接，以及进程中断后的幂等恢复。固定并验证 `FFMPEG_PATH`、读写权限、磁盘和解码超时；视频完整校验可能耗时，不把正常执行误判为卡死。确认失败、取消、路由停机和 Profile 稳定性符合预期。不能为了测试绕过维护门槛。
7. **完整业务与恢复采集。** 验证真实用户登录 → worker 执行 → 图片/视频/评论审核 → 报告 → 问答 → 定向重启后再问答；模型与真实 ASR 需单独验证，health 200 不够。完成审核、迁移、健康及维护解除收据后，按契约恢复准入和实际重启验证。

任一资产/安全门槛、数据库备份、账号身份、出口一致性、Linux 浏览器边界或迁移步骤不通过，则停止扩大范围，保留证据并维持采集暂停。首次迁移失败按发布契约修复 Camoufox 或保持业务维护模式；不自动回退旧引擎。未切换时保留原运行环境，已产生新业务数据后不以旧库回滚。

## 7. 交接时应留下的记录

- 实际交付提交与上述已测源码的关系、App/Crawler 配对、前端构建及依赖/浏览器资产摘要。
- 服务名、实际工作目录、解释器、配置文件位置、数据路径、代理绑定策略；只记录密钥位置，不记录值。
- 本次 Mac 冒烟结果，以及目标 Linux 的安全审核、预检、备份/迁移、登录/采集、完整业务与重启验证结果；未运行项明确标注。
- 故障时保持采集暂停的方法、备份位置、负责的部署人和实际验收时间。

配套已推送参考：[Audit 当前启动交接](https://github.com/Vicenta-cc/audit-agent-demo/blob/709c3929c5c584d3b596b83b813f7555e14c913d/docs/runtime/CURRENT-STARTUP.md)、[统一运行入口](https://github.com/Vicenta-cc/audit-agent-demo/blob/709c3929c5c584d3b596b83b813f7555e14c913d/docs/runtime/UNIFIED-RUNTIME.md)。它们用于解释入口，不能替代最终 Audit 候选的源码与配置核对。本仓库下方 README 中的 R2/Cloak/CDP 描述保留作历史或上游通用资料，不作为本次 Camoufox 集成部署依据。

## 7. 2026-10-19 发布准备补充

Audit 的发布入口修复通过发布工具 58 项（零跳过）及运行边界 19 项回归；前端历史轮次补丁通过逻辑 50 项、TypeScript 与生产构建，两项均获独立代码及架构审查通过。前端仍依赖 `INVESTIGATION_MULTI_ROUND_ENABLED` 和历史轮次成员数据，权限或成员缺失时保持拒绝，不退到最新轮次或其他端点。

这些结果不代替 Linux 浏览器、账号登录、真实调查和回退演练。Linux 完整哈希锁、wheelhouse、匹配浏览器归档及 G0 资产审查仍须完成。部署先盘点存储及生产版本，不因空间不足删除生产数据，不关闭原有账号/线路保护。
