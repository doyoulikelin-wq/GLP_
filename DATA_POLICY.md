# 数据与运行产物政策

## 目的

Git 是代码、合同、摘要和可复现索引，不是模型权重、原始结构仓库或运行对象存储。每个外置资源必须能从 manifest 判断“它是什么、从哪里来、何时获取、为何使用、如何校验、是否能公开”。

## 可提交

- 项目自编代码与 Notebook；
- 自包含 HTML 和生成它的规范 artifact；
- YAML/JSON 配置、小型登记表和机器可读摘要；
- 经许可审查、来源明确、体积受控的少量公开输入；
- URL、revision、字节数、SHA-256、记录粒度、限制和恢复说明；
- 失败结果的汇总与证据索引。

## 只索引、不提交

- BoltzGen checkpoint、AlphaFold 参数、`mols.zip` 和其他模型缓存；
- SAbDab2 原始下载包、完整 mmCIF 集合和 SQLite 数据库；
- 虚拟环境、Conda 环境、Python site-packages、vendor 源码树；
- 完整 `runs/`、`snapshots/`、NPZ、PDF、逐阶段日志、资源采样；
- 大型候选结构集合或未授权候选序列；
- 含姓名、邮箱、赛事报名信息、盲法映射或实验批次标识的文件。

Git LFS 也不是隐私或许可边界；公开仓库里的 LFS 对象仍然公开且进入历史。

## 资源索引字段

主索引位于 [`shared/resources/manifests/all_resources_20260826.csv`](shared/resources/manifests/all_resources_20260826.csv)，至少包含：

```text
resource_id, route, purpose, asset_class, local_workspace_path,
source_name, source_uri, source_revision, created_at, format,
record_count, file_count, size_bytes, sha256, sha256_manifest,
license, git_policy, repository_path, validation_status, limitations
```

`local_workspace_path` 是相对项目工作区的逻辑位置，不包含用户名或机器绝对路径。未来迁移到对象存储时，新增内容寻址 URI，不覆盖历史来源。

## 数据角色

- `input`：进入模型或分析的冻结数据。
- `output`：模型或程序直接生成的结果。
- `evaluation`：只用于阈值、排序、反筛或盲测的数据。
- `provenance`：只支持来源、化学身份和审计，不直接进入模型。
- `checkpoint`：预训练过程的输出、当前推理的输入；绝不称为训练数据。

结构条目的原抗原、数据库目录名或计算分数都不能自动成为本项目结合标签。

## 保留失败

运行失败、零幸存者和被安全停止的尝试不能被覆盖。新 attempt 写新目录；摘要同时保留分母、失败项、退出状态和限制。报告必须区分工程完成、计算通过和实验验证。
