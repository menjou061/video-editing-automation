# Doubao/Jianying orchestrator v1.5.0

For new production tasks, freeze task-level Eval inputs and runtime identity
before generation. Use the reviewed category/SKU material index when configured;
keep visual rules separate from the generic Diaodu/RDM contract. Retain real
QC, Gold-quality and Mac-review evidence. Observation mode never fabricates a
PASS and does not replace existing delivery gates.

这是视频自动化执行引擎，不包含业务账号、机器路径或生产日志。

部署时由宿主环境提供：

- `JY_SKILL_ROOT`、`JY_DRAFT_ROOT`、`JY_INSTALL_ROOT`
- `JY_NAS_SHARE`、`JY_NAS_USER`、`NAS_PASSWORD`
- `JY_MAC_DRAFT_ROOT`
- `LARK_BASE_TOKEN`

引擎职责：素材理解、语义分段、证据匹配、稳定切片、字幕写入、草稿结构校验和可回放证据生成。它不负责把视觉业务规则升级为通用 Diaodu/RDM 规则。

执行边界：

1. 先生成素材理解和匹配证据，再写入草稿。
2. 任何视觉模型失败或证据不足都必须阻断正式 `PASS`。
3. 只重做失败语义段，保留已完成稿和 deferred indices。
4. 分发和表格回写必须经过独立人工检查凭证。
5. 版本号从运行时实际文件回读，禁止在提示词中手写版本。

安全继续与恢复：

- 编排器默认单任务运行；上一个任务没有完整结束收据，或结果不是
  `SUCCESS`，就停在当前任务，不自动进入下一条。
- `result.json` / `last_result.json` 使用固定的 terminal_state、failure_class、
  retryable、safe_to_continue、next_action 和 parent_run_id 合同；失败后的新
  attempt 必须关联父 run。
- 环境失败只重做预检；语义失败只消费已有分析做匹配或人工确认；写稿和桌面验收
  分别从对应阶段恢复，不重新做视觉分析。
- 视觉服务调用最多进行一次有限重试；凭据、路径、编码和余额错误直接停机。
- 视觉子进程默认使用渲染机 `.codex/volc.config.toml` 的 `volc` profile 和火山
  CodingPlan OpenAI-compatible 接口；CC Switch Anthropic env 文件只作为显式回滚路径。
- `draft_write` 只接受 `semantic_match` 产生的成功嵌套结果；原始 manifest 或损坏缓存
  不得作为恢复输入。桌面诊断失败保持 `DESKTOP_IMPORT_BLOCKED` / `PACKAGE_NOT_ACTIVE`。
- 批量跳过必须由操作者显式设置 `JY_TASK_SKIP_CURRENT=1`，并先写入
  `DEFERRED` 收据。
- 批量 `done.json` 必须同时带有 `task_id`、`manifest_sha256`、`runtime` 和
  `task_log`；`task_log` 指向的 report 根目录下必须存在带 `task_finished` 事件的
  `result.json`/`events.jsonl`，否则继续队列会停在当前任务。
- 队列选择后的生成前预检会验证包版本、素材目录与视频池、草稿根目录父路径和脚本；
  预检失败直接写 `ENVIRONMENT_PRECHECK_FAILED`，不得触发 TTS、视觉分析或建稿。

具体命令和收据字段见仓库 `docs/video-task-control.md`。
