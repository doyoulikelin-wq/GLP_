# AIV1 技术门：合同构建与只读预检

本目录实现 AIV1 正式推理前可在 Mac 完成的部分：严格验证 AIV0 交接、16 个开发态、G2 的 10 个正式候选来源，构建 160 个任务的确定性矩阵，并生成不可覆盖的预检证据。它不会运行 BoltzGen 推理，也不能发布 AIV1 PASS。

## 当前实现

- `build_ai_validation_matrix.py`：只有 AIV0 PASS、正式 G2 Linux/NVIDIA 收据和 10 个候选全部闭合后，才在临时目录写 160-task matrix，并以原子重命名一次发布；缺失或漂移时不生成占位矩阵。
- `run_aiv1_preflight.py`：只能在独立的 AIV1 预检根新建 `attempt_NNN`；保存命令、Python 环境、自测日志和所有输入哈希，最终原子发布 `receipt.json`，禁止写入已冻结的 AIV0 根。
- `test_build_ai_validation_matrix.py`：用合成候选验证 160/800 正向合同，以及候选数、来源、G2 全证据链、CSV/mmCIF 序列、Mac/MPS、lockbox、路径逃逸、跨 campaign 编号、重复和哈希漂移等负向合同；权威测试数只读取最终 receipt 的 `contract_test_count`。

候选验证不是“文件存在即可”：正式 G2 gate 必须同时绑定 7XL0 验收成功文件、两项 6XYM 资源探针、各自输出清单、配置清单和资源汇总；validator 直接核对三个 cell 的 design CIF/NPZ、inverse-fold CIF/NPZ、fold NPZ 和 refold CIF 各 10 份，并检查每份 fold NPZ 确有 5 个坐标样本及 6 组有限评分。两项资源探针的显存峰值还会从原始 `nvidia_smi.csv` 重新计算，而不是相信 SUCCESS 或 gate 的自报数值。每个 anchor 还必须与 `aggregate_metrics_analyze.csv` 的完整序列、`refold_cif/<file_name>` 的 mmCIF 蛋白序列及输出清单逐一一致。不同 candidate ID 可以具有相同完整序列，但不能共用同一候选路径；不同路径若文件字节恰好相同，不擅自删减 10 个实例，而是在输入快照中记录重复内容计数。

正式 AIV1 仍需实现 `compute_project_metrics.py`、`run_multistate_ai_validation.py`、`update_ai_experience_registry.py`、`freeze_ai_eval_spec.py`、最终 validator 和正式 stage runner，并为每个文件提供可解析测试；预检会把这些缺口写成 `BLOCKED_MISSING_AIV1_IMPLEMENTATION`。

## 状态解释

| 状态 | 含义 |
|---|---|
| `FAIL_INPUT_CONTRACT` | AIV0 交接或 16-state 输入本身不一致，必须先修正合同/资产 |
| `BLOCKED_PREREQUISITES` | 当前可验证部分通过，但外部环境、G2 anchors 或正式执行代码尚缺 |
| `READY_FOR_FORMAL_AIV1_INPUT_VALIDATION` | 只表示可进入正式 AIV1 输入验证，仍不是 AIV1 PASS |

`MPS` 是 Apple 芯片上的 Metal Performance Shaders 加速后端；它不是合同要求的 CUDA/NVIDIA 环境。`G2` 是正式单 GPU 端到端验收门。`receipt` 是绑定输入、输出、状态和 SHA-256 哈希的机器凭证。
