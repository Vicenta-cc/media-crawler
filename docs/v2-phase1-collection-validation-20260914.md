# V2 第一阶段采集层验证

基线为正式运行 crawler 的 `d9aa0c10acdd6de8701303afe34a1c0f9cf2d42e`，并包含已验证的 CDN 修复提交 `60ccef1`。本阶段提交只修改 MediaCrawler 采集层。

## 修改

- `DY_SEARCH_PAGE_SIZE=15`，搜索 offset 改为 `page * page_size`。旧代码第一页传 `-15`，并以 10 作为步长请求 15 条，导致页间重叠和错位。
- 同一关键词搜索过程中维护 `seen_aweme_ids`，重复帖子不再消耗详情、媒体、评论和帖子额度。
- 顶层评论和二级评论在 callback 前按原始 `cid` 去重。
- 评论接口返回空数据且 cursor 不前进时退出，避免无界循环。
- 保留 CDN 媒体请求头、备用播放地址、有限刷新和媒体请求限速改动。

## 验证

定向测试：

```text
29 passed, 1 warning
```

覆盖第一页 offset、跨页重叠帖子、唯一帖子额度、评论重复 ID、cursor 卡住、CDN 请求头/备用地址/刷新/429/取消和既有登录态与限速测试。

全量测试：`96 passed, 8 skipped, 7 failed`。失败均为既有环境或既有测试问题，与本阶段修改无关：5 项需要本机 Redis 127.0.0.1:6379，1 项需要 Redis 的代理池，1 项是已有 Excel store 类型断言。没有把这些外部依赖失败算作分页、去重或 CDN 回归失败。

本阶段没有修改正式 runtime、正式数据库、应用代码或前端。
