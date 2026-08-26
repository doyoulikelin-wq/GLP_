# BoltzGen 一次性代码

此目录的脚本只用于特定日期的报告构建、Notebook 构建、资产剖析或交付封存，不得被 `boltzgen/main/` 正式流程导入。

- `report_builders/`：从冻结表构建报告 artifact。
- `notebook_builders/`：生成或执行复盘 Notebook。
- `profiling/`：一次性统计资产与 checkpoint 元数据。
- `release/`：封存历史交付和校验清单。

文件名包含用途和日期；原始实现中的相对路径按历史运行包解释，迁移后的代码主要承担审计和溯源角色。
