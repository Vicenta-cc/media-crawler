# 审核项目使用的 MediaCrawler 源码版本

本版本保存 2026-09-10 本机 MediaCrawler 的源码修改，起点为 `ec56ebfe638dcfc8680f41711215ee9618a843e2`。包含账号状态注入、失效标记、主内容限速、并发调度、流式采集及 API 参数支持。原项目 LICENSE 与声明保留。

仓库不包含登录 cookies、浏览器用户目录、采集结果、数据库、模型密钥或虚拟环境。使用者需自行安装依赖并登录账号。此仓库是采集组件，不包含审核项目的报告、前台、K RuleSet 或 Hermes。

## 安装

```bash
git clone https://github.com/Vicenta-cc/media-crawler.git
cd media-crawler
uv sync --frozen
uv run playwright install chromium
```

使用发布时记录的提交固定版本，避免自动跟随 main 改变行为。需要 Node.js 支持签名 JavaScript 执行；Linux 浏览器系统依赖按 Playwright 安装提示配置。保留采集器自己的环境，不与审核应用混装 Python 依赖。具体平台使用方式参见根 README。

审核应用配置示例（替换绝对路径）：

```dotenv
MEDIACRAWLER_DIR=/ABS/media-crawler
CRAWLER_LOGIN_PYTHON=/ABS/media-crawler/.venv/bin/python
```

主项目通过 CLI 调用，无需为此单独运行本仓库的 API 服务。不要把个人 cookies 写进源码提交。

## 单条与批量

同一源码通过 `--crawler_max_notes_count`、`--max_comments_count_singlenotes` 控制数量；`--max_concurrency_num` 控制并发，`--crawler_max_items_per_minute` 控制每分钟主内容启动数（1至5）。评论分页等待由 `--crawler_sleep_sec` 控制。

审核应用负责每词配额、总任务上限、去重、任务停止、报告和失败重试。只修改采集器参数不等于已配置审核项目的完整任务。单帖演示通常由主项目同时限制采集与分析各1帖。

## 本次验证

在独立源码目录使用既有采集器 Python 环境验证：`test_account_auth_state.py`、`test_crawl_rate_limiter.py`、`test_api_limits.py`，共23项通过。没有发起新采集或登录，没有修改正在运行的原目录。新机器全新依赖安装及各平台在线可用性不属于这次验证结果。
