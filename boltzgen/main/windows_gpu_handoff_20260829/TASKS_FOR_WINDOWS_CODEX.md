# Windows Codex 固定任务顺序

> **所有者模式更新（2026-08-31）**：如果工作区父目录存在状态为 `ACTIVE` 的
> `WINDOWS_OWNER_MODE.json`，本文件从 T7 起只作为历史审计兼容方案；请改读
> `boltzgen/main/windows_single_owner_20260831/WINDOWS_OWNER_TASKS_ZH.md`。所有者模式不再
> 要求 Mac 签发环境合同、复核补丁或接受经验事件。

本文件是 Windows 端的任务合同。任务必须按顺序执行；前一项没有形成闭合收据时，
后一项不得启动。所有失败都要保留，重试必须新建 `attempt_NNN`，不能覆盖旧目录。

## 已知状态

| 项目 | 当前状态 |
|---|---|
| AIV0 数据身份门 | `PASS`，权威 `attempt_007` |
| AIV1 静态合同测试 | 通过；不等于 AIV1 PASS |
| G1 GPU 环境门 | `NOT_RUN` |
| G2 单 GPU 端到端门 | `NOT_RUN` |
| 正式 G2 anchors（锚定候选） | 0 |
| 正式 AIV1 logical tasks（逻辑任务） | 0 |
| 正式 AIV1 sample rows（样本结果行） | 0 |
| 模型训练 | 禁止；本项目只做冻结权重推理 |
| lockbox 访问次数 | 0；包内只有 AIV0 身份元数据，无 25 个结构文件字节和候选×锁箱结果 |

## T-1：人工准备 Windows 与 WSL2

推荐安装当前 WSL 的 `Ubuntu-24.04`，因为其系统 Python 是 3.12，符合 BoltzGen
`>=3.11` 的要求。管理员 PowerShell 执行 `wsl --update`，需要新装时再执行
`wsl --install -d Ubuntu-24.04`。进入 Ubuntu 后安装：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip build-essential git zstd rsync
python3 --version
ps -p 1 -o comm=
```

Python 必须是 3.11 或 3.12，PID 1（Linux 启动后的第一个管理进程）必须是
`systemd`。Ubuntu 24.04 / Python 3.12 只用于 CUDA 12.8 工程候选，不是原
Ubuntu 22.04 / CUDA 12.6 正式环境；正式身份仍须 T7 由 Mac 发布版本化合同。
NVIDIA 显示驱动只安装在
Windows；禁止在 WSL 内安装 `nvidia-driver-*`。运行时笔记本要插电、关闭自动睡眠，
并保证散热。CUDA 12.8 工程候选要求 `nvidia-smi` 报告的驱动版本至少为 `570.65`。
工程阶段至少预留 80 GiB，申请正式 G1 前至少预留 250 GiB。

## T0：交接包校验和解包

1. 不要用 Windows 资源管理器解压。
2. 在 WSL2 中安装 `zstd`、`git`、`rsync`、`python3` 和 `tar`。
3. 把压缩包放在任意传输盘，但将内容解压到 WSL2 的 `$HOME/creator`。
4. 执行：

   ```bash
   cd /mnt/c/TRANSFER/WINDOWS_CODEX_GPU_HANDOFF_20260829_V1
   EXPECTED_TRANSFER_SHA256="请粘贴Mac_Codex单独给出的64位摘要"
   printf '%s  TRANSFER.SHA256SUMS\n' "$EXPECTED_TRANSFER_SHA256" | sha256sum -c -
   (cd .. && sha256sum -c WINDOWS_CODEX_GPU_HANDOFF_20260829_V1.TRANSFER.SHA256)
   sha256sum -c TRANSFER.SHA256SUMS
   sha256sum -c PAYLOAD.SHA256SUMS
   bash ./scripts/wsl/verify_and_extract_in_wsl.sh \
     "$PWD" \
     "$HOME/creator" \
     "$HOME/gpu_handoff_evidence" \
     attempt_001 \
     "$EXPECTED_TRANSFER_SHA256"
   ```

5. 校验失败立即停止，状态写 `BLOCKED_TRANSFER_INTEGRITY`。

无论成功或失败，T0 都把命令、标准输出、标准错误、退出码、文件清单和收据保存在
`$HOME/gpu_handoff_evidence/t0_transfer/attempt_001/`；失败重试必须改用新 attempt。

同级 `.TRANSFER.SHA256` 是包外传输校验文件，必须与目录一起复制；它只能检测拷贝
损坏，不能单独抵抗“包和校验文件一起被替换”。所以还必须从 Mac Codex 的独立回复
取得 64 位摘要。上面四条 `sha256sum` 命令必须在执行包内脚本之前先通过，以确认脚本
本身也未被替换；T0 随后会重复比较并拒绝任何清单外文件。
权威成功状态只有：

```text
TRANSFER_AND_SOURCE_VALIDATION_PASS
```

不得设置 `git config core.autocrlf true`。所有 Git 操作必须在 WSL2 中完成，
避免换行、权限或符号链接被 Windows 改写。

## T1：Windows 与 WSL2 只读探针

先在 Windows PowerShell 执行 `scripts/windows/collect_windows_host.ps1`，再在 WSL2
执行 `scripts/wsl/probe_wsl_gpu.sh`。必须记录：

```powershell
New-Item -ItemType Directory -Force C:\GPU_HANDOFF_EVIDENCE\windows_host | Out-Null
powershell -ExecutionPolicy Bypass -File `
  C:\TRANSFER\WINDOWS_CODEX_GPU_HANDOFF_20260829_V1\scripts\windows\collect_windows_host.ps1 `
  -OutputDirectory C:\GPU_HANDOFF_EVIDENCE\windows_host\attempt_001
```

```bash
bash "$HOME/creator/handoff/scripts/wsl/probe_wsl_gpu.sh" \
  "$HOME/creator/gpu_work" \
  attempt_001 \
  "$HOME/gpu_handoff_evidence/t0_transfer/attempt_001/receipt.json" \
  "/mnt/c/GPU_HANDOFF_EVIDENCE/windows_host/attempt_001/receipt.json"
```

- Windows、WSL2、Ubuntu 和 Linux 内核版本；
- GPU 精确型号、驱动、显存、温度和功率信息；
- `nvidia-smi` 是否正常；
- CPU、内存、磁盘空闲量和系统时间；
- WSL2 是否能看到 NVIDIA GPU。

只允许形成 `ENGINEERING_GPU_PROBE_PASS`，不能形成 G1 PASS。

## T2：CUDA 12.8 / Blackwell 候选环境

RTX 5070 Ti 属于 NVIDIA Blackwell 架构。当前正式方案锁定的
`PyTorch 2.7.0+cu126` 不能静默沿用。先运行
`scripts/wsl/bootstrap_cu128_engineering_env.sh`，在 Windows 本机建立：

```bash
bash "$HOME/creator/handoff/scripts/wsl/bootstrap_cu128_engineering_env.sh" \
  "$HOME/creator" \
  "$HOME/creator/gpu_work" \
  attempt_001 \
  "$HOME/gpu_handoff_evidence/t0_transfer/attempt_001/receipt.json" \
  "/mnt/c/GPU_HANDOFF_EVIDENCE/windows_host/attempt_001/receipt.json" \
  "$HOME/creator/gpu_work/runs/wsl_gpu_probe/attempt_001/receipt.json"
```

- 在线解析器环境；
- 带哈希的 requirements lock；
- Linux x86_64 wheelhouse（离线安装轮子目录）；
- 两个从同一 wheelhouse 独立重建的干净环境；
- PyTorch `2.7.0+cu128`；
- cuEquivariance `0.5.1` 四个组件；
- CUDA、BF16 和原生 kernel 前向/反向 smoke test（最小冒烟测试）。

候选环境只能标记 `ENGINEERING_COMPATIBILITY_ONLY`。必须记录实际 Ubuntu、
Python、驱动、GPU、PyTorch 和 CUDA 版本，之后发布有版本号的环境合同修订，才有
资格进入正式 G1。

## T3：补齐 GPU 生产代码

当前以下文件尚未物化，Windows Codex 需要实现并逐个测试：

- `build_input_manifest.py`：只从允许清单生成输入清单；
- `build_design_specs.py`：生成 12 个确定性设计规格；
- `verify_specs.py`：验证 12/12 `boltzgen check`；
- `verify_gpu_env_stage.sh`：每次业务阶段复验环境；
- `validate_cell_output.py`：验证候选、NPZ、mmCIF、五个折叠样本和有限数值；
- `submit_local_once.sh`：本地一次性提交状态机；
- `run_local_cell.sh`：单 GPU 本地执行器；
- `status_local_cell.sh`：只读查询；
- `finalize_local_attempt.py`：停止监控后生成输出清单和收据；
- `release_g2_anchors.py`：G2 通过后机械发布 10 个 anchor 及 release receipt。

笔记本没有 Slurm（集群作业调度器），不得伪造 `SLURM_JOB_ID`。本地执行收据使用：

```text
executor_kind=WSL2_SYSTEMD_SINGLE_GPU
```

使用 `flock`（Linux 文件锁）保证同时最多一个 GPU 任务；systemd 设置
`Restart=no`。禁止使用 BoltzGen `--reuse`，避免旧结果混入新 attempt。

## T4：规格检查

在模型推理前必须对旧 12 骨架生成 12 个设计规格，运行 12/12
`boltzgen check`，保存检查输出、人工复核表和 `spec_gate_bundle.tar`。任一规格
缺失、漂移或引用错误都写 `BLOCKED_SPEC_GATE`。

## T5：7XL0 工程 smoke

先运行：

```text
7XL0 × adherence checkpoint × 1 candidate × diffusion_batch_size=1
```

这项任务只用于确认 5070 Ti 可以跑通设计、逆折叠、复折叠、分析和过滤。成功状态
必须写：

```text
ENGINEERING_SMOKE_PASS_NOT_G2
```

不能把它改名为 `7xl0_adherence__attempt_001`，也不能冻结为 AIV1 anchor。

## T6：6XYM 工程显存探针

在 T5 通过后，可先运行 6XYM、单 checkpoint、`batch=1`。它只估计最长 CDR
骨架的显存风险。12 GB 显存发生 OOM（显存不足）时保留完整失败证据，写
`BLOCKED_GPU_MEMORY`。

## T7：环境合同修订与正式 G1

T2、T5、T6 的工程证据交回 Mac Codex 后，先由 Mac 冻结 CUDA 12.8 环境合同修订。
Windows 再实现并运行正式 G1 runner，机械复验系统、驱动、Python、两个离线重建
环境、完整依赖清单、native kernel、250 GiB scratch 和前序收据。只有版本化的
`G1 PASS` receipt 才能进入 T8；T2 的工程状态绝不能代替它。

## T8：正式 G2 的原始硬条件

只有环境合同、GPU runtime、12/12 规格门和本地执行收据全部闭合后，才能尝试：

1. `7xl0_adherence__attempt_001`：10 candidates，batch 1；
2. `6xym_diverse_batch5__attempt_001`：10 candidates，batch 5；
3. `6xym_adherence_batch5__attempt_001`：10 candidates，batch 5。

每个候选必须有 5 个折叠样本；两个 6XYM 探针的显存峰值均不超过总显存 90%；
不得有 CUDA OOM、NaN（非数值）、截断日志或缺失文件。

三项都必须机械匹配主方案 Step 8：design（设计）500 sampling steps、3 recycling；
inverse folding（逆折叠）200 sampling steps、3 recycling；folding（复折叠）200
sampling steps、3 recycling、5 diffusion samples。还要逐项复验 design checkpoint、
inverse-fold checkpoint、folding checkpoint、mols.zip、设计规格和 resolved config
（解析后配置）的 SHA-256。工程缩短参数不得使用正式 cell 名称。

如果 5070 Ti 的 batch 5 失败，batch 1 成功也不能宣布原 G2 PASS。若正式采用
batch 1，必须先发布新版合同并重建所有关联矩阵和哈希。

## T9：G2 anchor 发布

G2 真正通过后，`release_g2_anchors.py` 从原始 G2 gate 和三个 cell 机械生成：

```text
07_analysis/ai_validation/anchor_candidate_set_v1.tsv
04_pilot/g2/G2_anchor_release.receipt.json
```

只允许从 7XL0 × adherence 的 10 个候选发布 anchors。两个 6XYM cell 的 20 个
候选只是显存探针，明确禁止进入 anchor 集合。

## T10：补齐 AIV1 实现并做最终预检

以下六组代码和测试当前仍不存在：

- `compute_project_metrics.py`；
- `run_multistate_ai_validation.py`；
- `update_ai_experience_registry.py`；
- `freeze_ai_eval_spec.py`；
- `validate_aiv1_campaign.py`；
- `run_aiv1_stage.py`。

先完成六组实现及测试，再使用新的 `attempt_005` 运行最终 preflight；禁止向冻结的
AIV0 formal campaign 根追加任何文件。预检必须验证 10 anchors × 16 states = 160
logical tasks，每 task 5 samples，共 800 sample rows。唯一允许的就绪状态是：

```text
READY_FOR_FORMAL_AIV1_INPUT_VALIDATION
```

## T11：正式 AIV1 与经验库交接

只有 T10 就绪后才能运行 160/800 的正式推理、评价和只追加经验事件。完整分母、
失败分类、输入/代码/环境/输出哈希和最终 handoff 都闭合后，终态才可以是
`AIV1_HANDOFF_PASS`；否则保留可重放失败证据并停止。

每个 T0–T11 attempt 都要先生成不可覆盖的 receipt、manifest 和失败码。随后可按
`ENGINEERING_EXPERIENCE_EVENT_SCHEMA.json` 准备一个事件 JSON，并写入 Windows
工程暂存库：

```bash
mkdir -p "$HOME/creator/gpu_work/experience"
EVENT_JSON="$HOME/creator/gpu_work/experience/event_to_append.json"
SOURCE_RECEIPT="$HOME/creator/gpu_work/path/to/attempt_NNN/receipt.json"
python3 "$HOME/creator/handoff/scripts/append_engineering_experience.py" \
  --registry "$HOME/creator/gpu_work/experience/windows_engineering_events.jsonl" \
  --event "$EVENT_JSON" \
  --source-receipt "$SOURCE_RECEIPT"
```

运行前必须把上面两个变量改成实际的绝对路径；事件 JSON 的字段以 schema 为准。

暂存事件的状态固定为 `PENDING_MAC_REVIEW`。Windows 不得改写旧行，也不得直接修改
Mac 上既有的权威经验库；Mac Codex 验证源收据和哈希后，才决定接受、拒绝或以新
事件补充解释。此工程暂存库不代替 T10 尚待实现的正式 AIV1 经验库 writer。

## Git 与 GitHub 规则

- 在线仓库：`https://github.com/doyoulikelin-wq/GLP_.git`；
- `HANDOFF_STATUS.json` 同时记录原项目提交和脱敏基线提交；二者不是同一个哈希；
- T0 从离线 bundle 建立 `$HOME/creator/GLP_`；它只有脱敏单根历史且没有
  `origin`，不得给它添加公开远程地址；
- Windows 不执行 `gh auth login`，不克隆、不抓取、不推送公开仓库；
- T0 已自动从不可变脱敏提交创建 `codex/windows-gpu-<YYYYMMDD>` 本地工作分支；
  只在该分支提交代码，不得切回或移动脱敏基线分支；
- 完成后运行：

  ```bash
  bash "$HOME/creator/handoff/scripts/wsl/export_code_patches_for_mac.sh" \
    "$HOME/creator/GLP_" \
    "$HOME/creator/gpu_work" \
    attempt_001
  ```

- 把补丁目录和 GPU 收据交回 Mac；只有 Mac Codex 有权复核、应用并推送 GitHub；
- 导出器只保留脱敏基线到最终文件树的单一压平差异；中间提交、作者和提交正文不会
  进入交接补丁，补丁还必须通过字节扫描、大小门和临时基线试应用；
- 补丁只允许代码、合同、测试和小型清单；
- checkpoint、mols.zip、环境、wheelhouse、原始输出和数据不得推到公开 GitHub。
