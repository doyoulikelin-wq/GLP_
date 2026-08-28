# AIV1 技术门静态合同（2026-08-28）

本目录冻结 AIV1（AI 验证第 1 阶段）的输入身份和最小数据库结构。它只包含可审查的小型合同，不包含候选结构、推理结果或实验标签。

## 文件

| 文件 | 作用 |
|---|---|
| `aiv1_input_contract.json` | 冻结 10 个候选、16 个开发态、160 个逻辑任务、800 个样本结果及 G2 来源约束 |
| `development_state_contract.tsv` | 精确列出 6X18×1、1D0R×4、9IVM×1、2L63×10；不通过目录扫描选结构 |
| `aiv1_experience_registry_schema.sql` | `AIV1_BOOTSTRAP_SCHEMA_PARTIAL`：把不可变身份、状态事件、800 行样本事实和多指标长表分离，并禁止更新/删除历史事实；它不是 AIV2–AIV4 完整经验库 |
| `AIV1预检摘要_20260828.json` | 绑定最终预检 attempt、合同/代码哈希、已验证项目、当前阻塞和科学边界；明确不是 AIV1 PASS |

## 关键口径

- 10 个 anchor（锚定候选）只能来自正式 Linux/NVIDIA G2 的 `7xl0_adherence__attempt_001`；现有 Mac 结果和 6XYM 工程探针都不能替代。
- 该 cell 的来源分片字段固定为 `shard_id=acceptance`；config/code/environment 分别必须机械等于 7XL0 resolved-config 清单、实际 GPU runtime scripts 清单和 G1 environment provenance 清单，不能填写自报哈希。
- 16 个 state（目标结构状态）等于 `6X18×1 + 1D0R×4 + 9IVM×1 + 2L63×10`。
- 160 个 logical task（逻辑任务）等于 `10 candidates × 16 states`。
- 800 个 sample-result row（样本结果行）等于 `160 tasks × 5 folding samples`。
- task matrix（任务矩阵）逐行保存 `panel_role`（状态角色）和 `compact_cluster_weight`（紧凑构象簇权重）；1D0R model 12/19/20 固定为 6/10/4，禁止把三个代表构象简单等权平均。
- `metric_sample` 是逐指标的长表，行数会是 `800 × 指标数`，不得用它冒充 800 样本分母。
- 首版矩阵统一写 `REFOLD_REQUIRED`（必须重新折叠）。只有未来证明 candidate/config/target/code/schema/五样本产物哈希逐项相同，才能在新合同中使用 `REUSED_VERIFIED`（已验证复用）。
- GIP/2B4N 和 glucagon/6LMK 是 lockbox（一次性锁箱）分区；AIV1 任务数必须为 0。
- 16 态中的 6X18、1D0R 和 `data/not_binding` 逻辑路径沿用 AIV0 `compatibility_aliases.tsv` 已登记的三个兼容软链接；validator 必须逐字核对链接路径、目标 URI 和链接文本。除此以外，目标路径中的任何软链接都阻断。
- 当前 SQL 只是 AIV1 可执行骨架；进入 AIV2 前必须发布有版本号的 migration（数据库迁移），补齐 config、candidate、metric ensemble、failure、Codex decision 和 artifact 等完整实体并重新验证。

## 科学边界

AIV1 即使最终通过，也只说明输入、任务、原子映射、指标、聚合、失败处理和经验库链条可以重放。它不证明 VHH 结合、不结合、亲和力、选择性、表达、稳定性或实验成功；当前也不训练 BoltzGen 权重。
