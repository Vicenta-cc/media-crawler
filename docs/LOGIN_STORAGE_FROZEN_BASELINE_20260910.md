# 登录态修复冻结基线（2026-09-10）

## 固定版本

- 采集器分支：`codex/login-storage-frozen-20260910`。
- 采集器固定标签：`crawler-login-storage-frozen-20260910`。
- 父提交：`a77d8f4ad99b692641711c8e170c73dc7ebdf627`（已有账号注入、限速及流式接口基线）。
- 当前应用配对提交：`cad3eabd583543fac27a45f211122e2ca924ed2b`。
- M3 资源生命周期实验提交：`fcdd5bc`，单独保留；不是当前应用运行版本。

后续开发从上述固定版本另建分支；保留此标签，不移动、不覆盖。应用和采集器是两个 Git 仓库，须分别记录版本。

## 本次变更

账号状态注入脚本只在当前页面 origin 有对应快照时清空并恢复 localStorage。about:blank/srcdoc 子页面可能共享父页面存储但报告 null origin，必须跳过，以免清空父页面登录标记和实时会话更新。

本次只纳入此防护、两个实际 Chromium 离线回归用例及本文档。未合并 account-pool 或 crawl-v2 的换号与续抓实现。

## 核对及验证

- 本次修复前保存的 `base/base_crawler.py` 和 `tests/test_account_auth_state.py` 与父提交逐字节一致。
- 冻结时，运行目录中的抖音 core、基础配置、命令行入口及限速器与父提交逐字节一致。
- `python -m pytest tests/test_account_auth_state.py tests/test_crawl_rate_limiter.py -q`：10 passed。
- 既有连续三页验证曾成功；不以此承诺平台验证不会再次发生。

## 运行范围

此次提交在独立 worktree 完成，没有切换、重启或改写正在运行的采集目录。运行目录已有同一登录态修复。

标签冻结的是采集器源码。当前主采集目录另有 API 和其他平台的本地改动，并未整体快照到此版本；临时去重采集脚本、任务输出、账号登录态、模型密钥及本地环境也不包含在本源码标签内。因此该标签不是完整运行环境或历史任务的备份。
