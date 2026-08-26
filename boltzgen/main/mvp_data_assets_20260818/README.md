# BoltzGen MVP 数据资产准备（2026-08-18/19）

公开入口：`准备MVP数据资产_20260818.py`。实现位于 `scripts/`；两份实现均有模块文档字符串和关键步骤注释。

## 输入

- 固定公开来源快照：RCSB PDB、UniProt P01275、PubChem CID 16133831；
- BoltzGen `v0.3.2` 示例 YAML；
- 外置运行资产：4 个 checkpoint 与 `mols.zip`。

## 输出

- 只允许项目使用的 GLP-1/VHH 清理输入；
- 文件统计、SHA-256、质量检查与数据角色 manifest；
- 运行资产保持外置，绝不复制到 Git。

仓库只保留代码和小型清理表。原数据包位置、体积和验证状态见 [`../../resources/manifests/boltzgen_resources_20260826.csv`](../../resources/manifests/boltzgen_resources_20260826.csv)。
