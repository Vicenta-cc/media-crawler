# 抖音账号浏览器接入

抖音采集统一使用 CloakBrowser，不再通过旧 Playwright/CDP 启动分支。SDK 固定为 `cloakbrowser==0.5.10`，已加入 pyproject.toml、requirements.txt 和 uv.lock。`uv sync --frozen` 可安装锁定环境；登录服务应使用同一个 MediaCrawler Python 环境。Node 优先使用 PATH 中的程序，否则使用 Playwright 随包 Node，签名模块不依赖启动目录。

## 账号绑定与启动

调用方必须传入 `MEDIACRAWLER_ACCOUNT_ID`（账号库内部稳定 ID，不能用昵称或 Cookie），以及绝对路径 `MEDIACRAWLER_CLOAK_PROFILE_ROOT`。没有账号 ID 时启动失败；显式指定旧引擎时启动失败，不回退。

每个 ID 在根目录 profiles.sqlite3 中对应固定种子、首次登记的内核版本和独立目录。重新登录、重启或改昵称都不重新分配。整个注册表和账号目录须一起持久保存；它们含登录数据，不能提交 Git。各账号之间的种子不同不意味着所有浏览器采样字段都不同。

采集默认无头；CLI 显式 `--headless false` 才显示窗口。后台账号失效时报告 ACCOUNT_AUTH_INVALID，由账号管理完成重新登录，不在后台自动打开另一浏览器。任务成功、异常均释放浏览器目录锁。

## 配套账号管理

审计应用通过共享的本文件所在项目 `tools/cloak_browser.py` 启动登录窗口。应用的 `CRAWLER_BROWSER_PROFILE_ROOT` 会传给登录与采集两个子进程。管理页新登录使用可见 CloakBrowser，二维码仍可显示在原页面，验证码仍由用户本人处理。

重新登录先获取账号锁，通知管理器将账号标为 login_required，收到确认后才清理当前账号 Cookie 和登录来源的 localStorage。取消或失败保留该状态；成功关闭并落盘后才把加密备份写回账号库并标为 active。保持固定目录、种子和版本。

`auth-imported` 是首次导入边界：旧账号首次使用空目录可从数据库导入一次快照；已有目录永不覆盖回旧数据库快照。显式重新登录也写入该标记，取消之后不会复活旧凭据。数据库密文是备份，账号目录是运行时的最新状态。

同一账号登录/采集并发会被文件锁拒绝。当前锁实现在 macOS/Linux 使用 flock；本轮实机验证为 macOS，不宣称 Windows 已验收。

## 本地验证与边界

爬虫修复回归命令：

```sh
.venv/bin/python -m pytest -q tests/test_account_auth_state.py tests/test_douyin_response_detection.py tests/test_douyin_pagination_dedupe.py tests/test_cloak_browser.py tests/test_douyin_media_download.py tests/test_crawl_rate_limiter.py tests/test_douyin_js_runtime.py
```

2026-09-15：50 passed、2 skipped（可见/无头旧指纹抽样测试需 CLOAK_BROWSER_SMOKE=1）。配套应用另有真实 CloakBrowser 的本地页面测试，覆盖重新登录后爬虫读取新状态、目录/种子/版本不变、账号隔离、互斥锁和取消后不恢复旧快照。

本轮仅改代码和本地验证，未恢复“盘口”采集，未部署正式 runtime。此前 68 帖/127 评论属于之前可见模式的隔离实测，不等于本次完整管理页流程已实网验收。下一步需验证真实登录后的无头采集、重启恢复，再继续 100/300。

原先 scripts 下的实网诊断工具和早期报告是历史实验入口，不是本次生产启动接口；其中“失败保留窗口”等行为可能与现在任务结束关闭窗口的规则不同。
