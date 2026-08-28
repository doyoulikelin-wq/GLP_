# BoltzGen 路线会话进展

## 结论

BoltzGen 路线已经完成“数据与工程可运行性”的闭环，但尚未得到计算严格通过或实验验证的候选。当前不做基础模型全量训练，正式路线是冻结 BoltzGen `v0.3.2` 推理，再用配对实验标签训练项目级重排序器。

## 已完成

- 版本锁定：BoltzGen `v0.3.2`，tag commit `31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0`。
- MVP 资产：5 个必需运行资产均完成哈希核验，约 6.35 GB；权重不进入 Git。
- VHH 骨架库：从 4,508 个 SD-H instances 经结构质量控制、去重与聚类，冻结 10 PRIMARY + 2 RESERVE；12/12 通过 BoltzGen 输入检查。
- Mac 冒烟：2/2 链路完成，0/2 严格通过。
- 旧 12 骨架第一轮：24/24 候选链路完成，0/24 严格通过。
- Mac 增强轮：48 个主候选、96 个真实复折叠样本，0/48 严格通过；独立深度探针 0/4。
- 数据治理：112 个结构路径均可解析；新 17 个 challenger scaffold 仍为准入待定，正式 production 数为 0。
- 实施合同：形成无上下文可执行方案，包含数据地址、源码固定点、环境、质量门、统计设计、GPU 估算、实验闭环和停止条件。

## 尚未完成

- Linux/NVIDIA 正式 `AIV1–AIV4` 尚未运行。
- 实施方案中的部分生产脚本仍标记为 `TO_IMPLEMENT`。
- GLP-1 C 端酰胺尚未在当前标准聚合物坐标中完成原子级证明。
- 当前没有实验负标签，不能建立可信的 7–36/9–36 选择性重排序器。
- `0/N` 只描述当前计算批次，不证明 BoltzGen 无效，也不保证扩样必然成功。

## 2026-08-28 AIV1 预检增量

- 已冻结精确 16-state（16 个目标结构状态）合同：合同坐标指纹值与 AIV0 inventory（资产清单）的冻结值 16/16 一致，当前 16 个源文件的字节级 SHA-256 也已重新计算并一致；本次没有重新运行 AIV0 坐标指纹算法。AIV1 lockbox state 数为 0。
- 已实现 fail-closed（条件不满足即阻断）的 10-anchor（10 个 G2 冻结候选）输入验证与 160-task/800-sample-slot 矩阵 builder（任务表构建脚本）。合成正/负测试的实际项数和通过数只以最终权威测试 receipt 的 `test_count/test_passed/test_failed` 及日志哈希为准，不在进展文档写死。
- 已建立 `sample_result` 与 `metric_sample` 分离、身份/状态事件分离且禁止历史 UPDATE/DELETE 的经验库 SQL 启动骨架，状态为 `AIV1_BOOTSTRAP_SCHEMA_PARTIAL`；它不是完整 schema，进入 AIV2 前必须完成版本化 SQL migration、迁移测试和完整 schema receipt。
- 已运行仓库外不可覆盖预检；AIV0 handoff 与 state 合同通过，但正式 anchor/task/sample/inference/training 都仍为 0。
- 当前阻塞码为 `BLOCKED_EXTERNAL_INFRASTRUCTURE`、`BLOCKED_MISSING_G2_ANCHORS`、`BLOCKED_MISSING_AIV1_IMPLEMENTATION`、`BLOCKED_INSUFFICIENT_SCRATCH`；不得表述为 AIV1 PASS。

本节术语的完整中文解释见 [AIV0/M0 补充方案第 10 节](../plans/glp1_vhh_aiv0_m0_20260828.md#10-专业名词表)，包括 G1/G2、PDB ID、mmCIF、GPU、CUDA、BF16、MPS、`nvidia-smi`、scratch、allowlist、runner、validator、schema、SQL、JSON、TSV、CSV、NPZ、GiB、SHA-256 和 handoff。

## 最新执行顺序

1. 在 Linux/NVIDIA 环境实现 AIV1，保持 BoltzGen 权重冻结。
2. 以同一 10 anchors×16 states 完成 160 tasks/800 sample results；AIV1 只冻结 schema、公式、方向、缺失处理和聚合规则，不从该单一 scaffold 冻结跨 scaffold 数值阈值。
3. provisional spec 与 final spec 的上述规范字段只要不同，就必须对同一 160/800 全量重跑并重新签发 `AIV1_DATA_COMPLETE`；final spec、最终 DATA_COMPLETE 和完整经验库 schema receipt 全部闭合后，才发布 `AIV1_HANDOFF_PASS` 并进入 240。
4. 依次推进 240、2,400、12,000；每层都保留不可变 baseline、失败证据和候选去重谱系。
5. 形成 96–192 条配对表面等离子体共振或生物层干涉实验面板。
6. 有足够正负、表达和交叉反应标签后，训练项目级 XGBoost 加速失效时间模型；不把结构代理冒充解离常数。
7. 以 score-blind 前瞻批次验证重排序器，再决定是否评估 adapter、低秩适配或小模型继续训练。

## 资源入口

- [零上下文执行方案](../plans/boltzgen_glp1_vhh_execution_plan_20260826.md)
- [报告目录](../reports/html/)
- [代码目录](../main/)
- [资源索引](../resources/manifests/boltzgen_resources_20260826.csv)
