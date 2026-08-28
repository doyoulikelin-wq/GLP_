# AI 结构资产验证登记册 V2

这里保存可进入 Git 的小型静态合同，不保存原始结构库、完整派生清单或运行日志。
完整派生登记册位于
`workspace://boltzgen/data/ai_structure_asset_validation_registry_20260828_211504/`，阶段日志
位于
`workspace://boltzgen/runs/glp1_vhh_formal_campaign_20260828/logs/stages/aiv0_asset_validation/`。

## 资源治理

| 字段 | 值 |
|---|---|
| 用途 | M0/AIV0 数据身份、迁移可重复性、结构解析、分区语义和 scaffold 准入审计 |
| 来源 | 项目生成的登记合同；结构内容分别继承 RCSB PDB、SAbDab2 和各上游来源登记 |
| revision | `AI_VALIDATION_ASSET_REGISTRY_V2`，2026-08-28 |
| 创建时间 | 2026-08-28（Asia/Shanghai） |
| 格式 | TSV、JSON、JSONL、Markdown |
| 粒度 | 源逻辑路径、结构逻辑路径、cohort、override、compatibility alias、attempt 事件 |
| 记录数 | 177 条历史源逻辑记录；112 条结构逻辑路径；13 个 cohort；18 个 override |
| 文件数/字节数 | Git 包和外置登记册分别由仓库树与最终 receipt 动态闭合，禁止用一个总数混为同一资源 |
| SHA-256 | 五个迁移前非摘要 TSV 见 `historical_output_hashes.tsv`；当前 V2 见最终阶段 receipt |
| 许可 | 项目自编合同/代码按仓库政策；结构与元数据继续继承各来源许可，不由本登记册重新授权 |
| 敏感级别 | `public_or_restricted_research_metadata`；不含候选序列、实验盲法映射、凭据或个人信息 |
| Git 策略 | 只提交代码、静态合同、小型摘要和脱敏经验事件；完整派生清单与 logs 外置 |
| 消费者 | V2 validator、AIV0 runner、AIV1 输入构造器、审阅者和未来 production handoff |
| 验证状态 | 以 `AIV0验证摘要_20260828.json` 指向的最终只读 receipt 为准 |
| 限制 | PASS 不证明结合、非结合、`K_D`、选择性、表达、稳定性或实验成功 |

## 文件

- `asset_mounts.tsv`：把稳定逻辑路径映射到迁移后的规范 `workspace://` 资产；同时
  冻结每个源挂载的文件数与字节数。
- `compatibility_aliases.tsv`：登记仍需保留的历史软链接、预期相对链接文本和目标；
  篡改、绝对、悬空或逃逸链接均阻断。
- `cohort_registry.tsv`：定义 13 个结构群、统计粒度、角色与默认状态。
- `file_overrides.tsv`：冻结 compact 正对照、不完整挑战结构和新 scaffold 的隔离或
  准入状态。
- `historical_output_hashes.tsv`：冻结迁移前 5 个非摘要 TSV 的 SHA-256；V2 必须逐
  字节复现它们，`validation_summary.json` 因 schema 与 validator 哈希升级而受控更新。
- `AIV0验证摘要_20260828.json`：只含可公开进 Git 的 M0 结果和规范 URI，不含完整
  本机日志或绝对路径。
- `aiv0_experience_events_20260828.jsonl`：脱敏的成功、失败和 supersede 经验事件；
  完整运行证据仍以外置 attempt 为准。

## 计数口径

- 177：历史逻辑清单行数，合计 14,884,156 字节；用于迁移可重复性。
- 175：去掉两个 Finder `.DS_Store` 后的逻辑文件数，仍包含文档、归档和镜像，不
  应称为 175 个科学样本。
- 24：旧正向镜像树的逻辑文件数；其 23 个 mmCIF 与主集合逐对相同，但验证读取
  独立归档副本，避免软链接“自己和自己比较”。
- 112：结构逻辑路径数，112/112 可解析。
- 72：新 17 scaffold 包中被 `checksums.sha256` 冻结的文件数，72/72 验证通过；
  checksum 文件自身不自校验。
- 1：1D0R 正向 ensemble 的独立 biological deposition 数；20 个构象相关，不能按
  20 个独立正样本统计。
- 36：challenge prepared mmCIF；其中 32 个可作计算挑战、4 个不完整而隔离，32 个
  构象只属于 4 个 target/source groups。
- 0：实验负标签数。目录名 `no_binding` 不是 binder/nonbinder 真值。

详细门槛和名词解释见
[`../../../plans/glp1_vhh_aiv0_m0_20260828.md`](../../../plans/glp1_vhh_aiv0_m0_20260828.md)。
