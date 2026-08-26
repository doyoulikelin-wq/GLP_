# GLP-1 活性型态选择性捕获蛋白设计

本仓库汇总“选择性识别活性型 `GLP-1(7–36)NH₂`、尽量排斥 `GLP-1(9–36)NH₂`”项目的会话结论、项目代码、输入索引、运行摘要和自包含 HTML 报告。项目分成两条互相独立、共享实验终点的路线：

- [`boltzgen/`](boltzgen/)：以可变重链抗体结构域（VHH）骨架为约束的工程主线。
- [`bindcraft/`](bindcraft/)：全新骨架小型结合蛋白的探索线。
- [`shared/`](shared/)：共同知识、会话总览、资源索引和仓库治理。

## 当前结论

| 路线 | 已完成 | 最新结果 | 当前状态 |
|---|---|---|---|
| BoltzGen | 数据资产核验、VHH 骨架筛选、Mac 冒烟、旧 12 骨架第一轮和增强轮、失败归因、正式执行合同 | 冒烟 `0/2`、第一轮 `0/24`、增强轮 `0/48`、深度探针 `0/4` 严格通过 | 工程链路完成；Linux/NVIDIA 正式 campaign 尚未启动；无实验命中 |
| BindCraft | 算法说明、选择性原型 Notebook、8 个 PDB 输入、静态审计 | 12 个代码单元均未执行；无候选 PDB、CSV、日志或实验结果 | 先修输入与评价合同，再做每个正构象 30–50 条轨迹的小试 |

这些 `0/N` 是完整保留的负结果，不是失败数据被删除。它们只描述当前计算批次，不能证明模型无效，也不能保证扩大样本会成功。

## 先看这些文件

- [项目会话总览](shared/sessions/project_session_overview_20260826.md)
- [项目总览 HTML](shared/reports/html/glp1_project_session_resource_overview_20260826.html)
- [全部资源索引](shared/resources/manifests/all_resources_20260826.csv)
- [仓库逐文件来源与 SHA-256 映射](shared/resources/manifests/repository_file_map_20260826.csv)
- [BoltzGen 最新进展](boltzgen/sessions/boltzgen_progress_20260826.md)
- [BoltzGen 无上下文执行方案](boltzgen/plans/boltzgen_glp1_vhh_execution_plan_20260826.md)
- [BindCraft 最新进展](bindcraft/sessions/bindcraft_progress_20260826.md)
- [BindCraft 输入审计 HTML](bindcraft/reports/html/bindcraft_glp1_selectivity_input_audit_20260826.html)
- [GLP-1 AI 设计知识图谱](shared/reports/html/glp1_ai_design_knowledge_graph_20260819.html)

## 目录语义

```text
boltzgen/
  main/                 可复用的路线代码；按尝试和日期分包
  plans/                正式实施合同
  reports/html/         自包含 HTML 报告
  reports/manifests/    生成 HTML 的规范 artifact
  notebooks/            可审计复盘 Notebook
  resources/            小型清理数据、运行摘要与外部资源索引
  sessions/             会话结论与最新进展
  tools/one_off/        报告/Notebook 构建、剖析、封存等一次性代码
bindcraft/
  main/                 当前研究原型
  reports/              审计报告及其规范 artifact
  notebooks/            输入审计 Notebook
  resources/            8 个小型公开 PDB 输入与逐文件 manifest
  sessions/             会话结论与最新进展
  tools/one_off/        一次性工具专用目录
shared/
  main/                 仓库级可复用检查
  reports/              共同知识和项目总览
  resources/            全部会话/资源机器可读索引
  sessions/             跨路线总结
  tools/one_off/        仓库迁移和索引生成脚本
```

`main/` 在这里是“正式或可复用代码目录”，与 Git 默认分支 `main` 不是一回事。

## 命名规范

用户给出的 `*` 作为分隔意图处理；实际文件名不用操作系统保留字符，而用下划线：

- 执行入口或尝试包：`<尝试内容>_<YYYYMMDD>`。
- 数据：`<用处>_<YYYYMMDD>` 或 `<用处>_<YYYYMMDD_HHMMSS>`。
- 一次性脚本：放入对应路线的 `tools/one_off/`，文件名必须包含目的和日期。
- 不使用 `latest`、`final_v2`、`new` 等会漂移的名称；最新状态由 README 和 manifest 指向。

详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 数据与 Git 边界

Git 中只保存：项目代码、Notebook、HTML、报告 artifact、配置、小型清理表、精选公开输入和资源 manifest。以下内容只索引，不上传：

- BoltzGen checkpoint、`mols.zip` 和模型缓存；
- SAbDab2 原始 TGZ、完整结构库和 SQLite 主库；
- Python/Conda 环境、vendor 源码快照、缓存；
- 完整 `runs/`、CIF/NPZ/PDF/日志/资源采样和中间结果；
- 含姓名、邮箱或赛事信息的工作簿和未审核文档；
- 未授权候选序列、盲法映射或实验批次信息。

资源索引保留用途、来源 URL、revision、格式、文件数、字节数、校验清单、许可、验证状态和限制。详见 [DATA_POLICY.md](DATA_POLICY.md)。

## 上游固定点

- BoltzGen 正式基线：[`v0.3.2`](https://github.com/HannesStark/boltzgen/releases/tag/v0.3.2)，commit `31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0`。Mac 历史运行使用未合并的实验性 MPS commit `592317f0f5582730b28c144267a15631c07fcb94`，不等同官方 CUDA 复现。
- BindCraft 当前可参考 release tag [`v.1.5.3`](https://github.com/martinpacesa/BindCraft/releases/tag/v.1.5.3)，commit `a234a8d3af9fe3d2724209aa91d930280b72048b`。现有本地 Notebook 当时动态获取未固定 `main`，因此仍标记为不可复现原型，不能倒填成已使用该 release。

第三方软件、模型和数据的许可各自独立，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本仓库当前未声明项目整体许可；公开可读不等于授权复用。

## 科学边界

预测界面模板建模分数、预测对齐误差、结构均方根偏差、接触数、溶剂可及表面积和 PyRosetta 能量都只是计算代理，不是解离常数、亲和力或临床结论。只有配对的表面等离子体共振或生物层干涉、表达质量控制、交叉反应和捕获液相色谱—串联质谱实验，才能支持项目目标。
