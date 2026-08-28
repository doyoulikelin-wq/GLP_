# AI 结构资产验证

`验证AI结构资产_20260820.py` 是带日期的公开入口，`validate_assets.py` 是可复用
实现。2026-08-28 起使用 V2 合同：逻辑身份用于规则、排序和历史对照，迁移后的
规范物理路径只用于读取与哈希；验证器禁止从软链接解析结果反推资产身份。

静态合同位于
[`../../resources/data/AI结构资产验证登记册_20260828/`](../../resources/data/AI结构资产验证登记册_20260828/)，
派生登记册外置于
`workspace://boltzgen/data/ai_structure_asset_validation_registry_20260828_211504/`。
历史 20260826 登记册保持只读。

正式调用由
[`../aiv0_campaign_20260828/run_aiv0_stage.py`](../aiv0_campaign_20260828/run_aiv0_stage.py)
完成，并为每次 `write` 或 `check` 生成不可覆盖的外置 attempt 收据。直接调用时应
显式提供 `--workspace-root`、`--contract-root` 和 `--output-root`；参数值由操作者在
运行时提供，仓库不保存本机绝对路径。

V2 冻结门包括：177 条历史逻辑源记录、14,884,156 字节、112 个结构路径且
112/112 可解析、9 个兼容别名、5 个迁移前派生 TSV 逐字节复现，以及新 scaffold
checksum 72/72。177 不是独立
科学样本数：其中包括 24 条正向镜像记录和 2 个 Finder 元数据文件；1D0R 的 20
个构象仍只来自 1 个独立 NMR deposition。

`PASS` 只表示登记、解析、迁移和语义合同通过，不表示候选结合、亲和力、选择性
或实验有效性通过。
