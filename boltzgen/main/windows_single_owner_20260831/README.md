# Windows 单机所有者迁移包

本目录把旧的“Mac 权威、Windows 只执行并回传”的交接方式切换为 Windows/WSL2 单机
所有者模式。它面向已经完成 T0–T6、当前停在“下一步 T7”的 Windows 工作区。

## 结论

- 不覆盖 Windows 当前工作区；迁移脚本先完整备份旧仓库，再把 Windows 已提交的代码差异
  应用到完整项目历史。
- 原 T7 的 Mac 环境合同签发取消，改为 Windows 本机 `LOCAL_ENV_ACCEPTANCE`。
- 现有约 6 GB 权重和运行资产继续使用，不在本迁移包中重复复制。
- Windows 获得完整 `GLP_` Git 历史、GitHub 远程地址和全部 `shared/data` 小型样本集合。
- Windows 可以直接提交和推送，不再导出补丁给 Mac 复核。
- SHA-256 只用于发现复制损坏，不是签发、审批或跨机器授权。

缩写和专业名词见 `GLOSSARY_ZH.md`。

## Windows Codex 权限

项目根目录的 `AGENTS.md` 负责授予项目内的工作权限；它不能代替 Windows
Codex 自身的本机沙箱设置。如果所有者明确要求项目工作期间不再弹出文件和
命令审批，在 Windows Codex 中打开迁移后的 `GLP_` 目录，运行 `/permissions`，
选择 `Danger Full Access`（不限制本地文件和命令的高权限模式），并将请求批准设为
`Never`（不询问）。这是一次 Windows 本机设置，不需要 Mac 签发。
如果使用 Codex CLI（命令行版），可以在项目目录用以下明确参数启动：

```bash
codex --sandbox danger-full-access --ask-for-approval never
```

高权限模式同样能读写项目外的本机文件，所以只在自己的可信机器上开启。

## Mac 构建命令

在源仓库已提交后运行：

```bash
bash boltzgen/main/windows_single_owner_20260831/scripts/build_windows_owner_handoff.sh \
  /path/to/creator \
  /path/to/creator/transfer
```

成品目录名为 `WINDOWS_SINGLE_OWNER_HANDOFF_20260831_V1`。它应与 Windows 已有的
`WINDOWS_CODEX_GPU_HANDOFF_20260829_V1` 放在同一传输目录，但不依赖 Mac 再发送单独摘要。

## Windows 接管命令

把成品目录复制到 Windows 后，在 WSL2 中执行：

```bash
bash /mnt/c/TRANSFER/WINDOWS_SINGLE_OWNER_HANDOFF_20260831_V1/ADOPT_IN_WSL.sh \
  "$HOME/creator"
```

脚本会：

1. 要求当前 Windows 仓库先提交全部 T3–T6 代码；
2. 把当前脱敏仓库完整移动到带时间戳的备份目录；
3. 从完整 Git bundle 建立新仓库；
4. 把旧脱敏基线到 Windows 当前 HEAD 的代码差异应用到新仓库；
5. 逐文件比较现有数据：相同文件跳过，缺失文件才复制，同路径内容不同则立即停止，
   绝不静默覆盖；
6. 创建 Windows 主分支、配置 GitHub `origin` 并建立样本数据兼容路径；
7. 在 `~/creator/AGENTS.md` 和 `~/creator/handoff/OWNER_MODE_OVERRIDE.md` 写入所有者
   模式跳转，防止外部旧交接文档继续要求 Mac 审批；
8. 保留旧仓库、迁移补丁和报告；任何中途失败会自动恢复旧仓库，只在全部成功后
   才最后写入 `WINDOWS_OWNER_MODE.json` 并发布 `ACTIVE` 标记。

迁移后请在 Windows Codex 中精确打开 `~/creator/GLP_`，而不是继续把旧交接包目录
当作项目根目录。

GitHub 首次推送仍需要 Windows 用自己的 GitHub 账户登录一次。这是 GitHub 对账户身份的
验证，不是 Mac 审批。不要复制 Mac 的 Token 或私钥；在 Windows 使用浏览器登录即可。

## T7 之后怎么走

详见 `WINDOWS_OWNER_TASKS_ZH.md`。最短路径是：

1. 本机环境快速检查；
2. 以实测可行的 batch 和采样预算继续候选生成；
3. 自动验证结构、数值和复折叠样本；
4. Windows 自己把成功/失败写入经验库并调整下一轮。

正式 G1/G2/AIV 的旧标签属于原审计兼容方案。单机所有者模式默认使用
`LOCAL_ENV_READY`、`EXPLORATORY_INFERENCE_COMPLETE` 和 `AI_EVALUATION_COMPLETE`，避免把
计算探索误称为预注册验证或实验结论。
