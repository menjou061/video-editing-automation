# Doubao/Jianying orchestrator v1.4.0

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
