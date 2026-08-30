# Windows Codex GPU 交接包

本目录定义 `WINDOWS_CODEX_GPU_HANDOFF_20260829_V1` 的构建、校验和接手边界。
成品用于把 Mac 上已经冻结的项目代码、公开开发数据、BoltzGen `v0.3.2`
源码和 GPU 运行资产交给 Windows 5070 Ti 笔记本中的 WSL2 Codex。

如果只想先在自己的 Windows 电脑上加载现成权重，完成一次 VHH 推理、候选生成和
筛选，请阅读源码中的 `QUICK_START_PERSONAL_INFERENCE_ZH.md`；在交付成品中，它会成为
同级个人入口附加包的 `README_FIRST_ZH.md`。这个小包不复制大权重，用户只运行一条
PowerShell 命令；原有 T0/T1/T2 审计链仍保留给后续正式 AI 验证，不需要在第一次个人
冒烟时手工操作。

## 当前能力边界

- AIV0（AI 验证第 0 阶段，数据身份检查）已通过，权威运行是
  `aiv0_asset_validation/attempt_007`。
- AIV1（AI 验证第 1 阶段）输入合同测试已通过；正式推理尚未开始。
- G1（GPU 环境门）和 G2（单 GPU 端到端验收门）均未运行。
- 当前仓库还没有正式的单 GPU 执行器、Linux wheelhouse、环境 lock、G2 anchor
  发布器和六个正式 AIV1 执行单元。
- 因此本包的状态只能是 `ENGINEERING_HANDOFF_READY`，不能写成 G1、G2 或
  AIV1 `PASS`。白话说，它只表示“材料已校验，可以交给 Windows”，不表示
  Windows GPU 或 BoltzGen 已经跑通。

即使将来形成 G1 PASS、G2 PASS 或 `AIV1_HANDOFF_PASS`，它们也只是计算技术门，
不能证明真实结合、亲和力、选择性或实验成功。

## 成品包含

1. 只含 BoltzGen 路径的脱敏单根提交、离线 Git bundle 和精确源码快照；
2. 上游 BoltzGen `v0.3.2`、提交
   `31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0` 的源码快照；
3. 五个 GPU 运行资产、来源材料、运行清单和 SHA-256 校验；
4. 已准入的 12 个基线 VHH 骨架、6X18 计算正向参考结构和 AIV1 精确 16 个公开
   开发态；这些输入本身不证明结合、亲和力或选择性；
5. AIV0 最终收据、最终派生登记册和 AIV1 预检证据；
6. Windows/WSL2 只读探针、解包、兼容路径和 CUDA 12.8 候选环境脚本；
7. Windows Codex 的固定任务顺序和术语表；
8. 只追加的工程经验事件格式与写入器。Windows 只形成等待 Mac 复核的原始事件，
   不能直接修改权威经验库。

AIV0 审计证据中保留 25 个锁箱结构的身份元数据（相对名称、大小、SHA-256、来源和
隔离状态），用于证明它们被排除。包内没有这 25 个结构文件的字节，也没有任何
候选×锁箱推理结果；锁箱访问次数仍为 0。

为保持历史收据可复算，部分旧 attempt 日志会原样保留当时的 Mac 或 Windows 本机
路径字符串；它们是来源记录，不是凭据，也不会被 Windows 当成可执行路径。归档文件
的拥有者字段统一写成 `root/root`，不携带 Mac 用户名。

## 明确排除

- GIP、glucagon/6LMK 等 lockbox（一次性锁箱）结构，以及可能包含它们的完整
  项目 Git 历史；
- Mac ARM/MPS 虚拟环境、第三方源码副本和完整运行输出；脱敏源码中仍保留 Git 已跟踪的
  小型 Mac/MPS 参考代码与审计摘要，只可阅读借鉴，不能作为 Windows 正式证据；
- SAbDab 原始压缩快照、数据库主库和未准入的新 17 骨架；
- `private/`、GitHub Token、SSH 私钥、`.env` 和任何凭据；
- 训练数据和模型训练任务。

大模型和数据不会提交到公开 GitHub。GitHub 只保存本目录中的代码、文档和
小型合同。实际交接包由 `scripts/build_handoff_bundle.sh` 生成在 Git 仓库外。

## Mac 端构建交接包（Windows 用户不要执行）

必须在干净且已提交的项目分支上执行：

```bash
PROJECT_REPO="$(git rev-parse --show-toplevel)"
WORKSPACE_ROOT="$(dirname "$PROJECT_REPO")"
bash "$PROJECT_REPO/boltzgen/main/windows_gpu_handoff_20260829/scripts/build_handoff_bundle.sh" \
  "$WORKSPACE_ROOT" \
  "$WORKSPACE_ROOT/transfer"
```

构建器拒绝覆盖同名目录，复验五个运行资产、精确 16 个开发态和 12 个骨架，
生成不超过 1.9 GiB 的大资产分卷，并在临时目录完整解包复核后才发布成品。

基础包完成后，Mac 端再生成不复制权重的个人推理入口附加包：

```bash
BASE_BUNDLE="$WORKSPACE_ROOT/transfer/WINDOWS_CODEX_GPU_HANDOFF_20260829_V1"
BASE_TRANSFER_SHA256="$(shasum -a 256 "$BASE_BUNDLE/TRANSFER.SHA256SUMS" | awk '{print $1}')"
bash "$PROJECT_REPO/boltzgen/main/windows_gpu_handoff_20260829/scripts/build_personal_inference_overlay.sh" \
  "$BASE_BUNDLE" \
  "$WORKSPACE_ROOT/transfer" \
  "$BASE_TRANSFER_SHA256"
```

附加包必须与基础包放在同一目录；构建器拒绝把它写进基础包或 Git 仓库。

成品是一个“目录型压缩交接包”，不是单个 ZIP：请把整个
`WINDOWS_CODEX_GPU_HANDOFF_20260829_V1` 目录及同级的
`.TRANSFER.SHA256` 文件原样复制到 Windows，不要只复制其中一个分卷，也不要用
网盘在线解压。Mac Codex 还会把其中的 64 位摘要通过本任务回复单独告诉用户；T0
必须同时匹配这个独立摘要和包外校验文件，再从离线 bundle 恢复一个不含远程地址的
脱敏 Git 工作树。它只有一个基线提交，不含完整公开仓历史。

Windows 端禁止克隆或抓取完整公开仓库。它把代码修改提交到本地分支后，用
`export_code_patches_for_mac.sh` 导出补丁；Mac Codex 复核补丁并集成到原项目后，
才由 Mac 推送 GitHub。导出器只生成“脱敏基线 → 最终文件树”的单一压平差异，扫描
实际补丁字节并试应用，不携带 Windows 的中间提交元数据。这样 Windows GPU 代理不会
接触仓库其他路径中的保留结构。

Windows 端必须先阅读 `WINDOWS_CODEX_START_PROMPT.md` 和
`TASKS_FOR_WINDOWS_CODEX.md`，并且只在 WSL2 的 `/home/...` Linux 文件系统中
解包和运行。

## 工程交接仍需现场验证的事项

- Mac 无法代替真实 Windows 执行 PowerShell、WSL2 和 5070 Ti 探针；第一次 T1
  是现场验证，不得提前标记通过。
- Windows 探针当前只能证明“至少一个固定卷”还有 80 GiB，不能机械证明它就是 WSL
  虚拟磁盘所在卷。开始 T2 前要人工确认实际承载卷；正式 G1 前须按 WSL 登记的
  `Lxss BasePath`（WSL 发行版存储路径）检查至少 250 GiB。
- T2 只建立可复算的工程候选环境，不是只读封存的正式环境。进入 T3/T7 或引用它作为
  正式身份前，必须重新核对 wheelhouse、依赖 lock、两个环境和收据，并按新版正式
  合同封存。
- 参数、路径或基础命令在 attempt 目录建立前就错误时，脚本只能在终端报错；Windows
  Codex 仍须把终端记录另存，再用新的 `attempt_NNN` 重试。
