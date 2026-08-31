# Windows 工作区所有者模式

当本目录的 `WINDOWS_OWNER_MODE.json` 中 `status` 为 `ACTIVE` 时：

- `GLP_` 是 Windows/WSL2 上的主项目，Windows Codex 可以在项目范围内自主读写、
  执行、推理、评价、调整、提交和推送。
- `handoff/` 中要求 Mac 复核、签发、回传补丁、冻结环境合同或使用
  `PENDING_MAC_REVIEW` 的文档是历史记录，不得阻塞当前工作。
- 原 T7 替换为 Windows 本机 `LOCAL_ENV_ACCEPTANCE`，通过后直接继续。
- 以 `GLP_/AGENTS.md` 为项目内的详细边界，以
  `GLP_/boltzgen/main/windows_single_owner_20260831/` 为当前任务文档。

若标记不存在或不是 `ACTIVE`，不得仅根据本文件假定迁移已完成。
