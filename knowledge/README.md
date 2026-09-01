# 知识

本目录是“活性型 `GLP-1(7–36)NH₂` 选择性捕获蛋白”项目的统一知识入口。
项目原有的 10 份 HTML 仍保存在 `shared/`、`boltzgen/` 和 `bindcraft/` 的规范报告目录中；
这里不复制或移动报告，只提供分类导航、完整目录、哈希和来源链，避免出现两个内容相同却可能漂移的权威副本。

## 快速入口

- [浏览器知识门户](reports/html/glp1_knowledge_portal_20260901.html)
- [全部 HTML 目录](resources/manifests/html_reports_20260901.csv)
- [HTML—来源映射](resources/manifests/html_report_sources_20260901.csv)
- [当前项目状态](../README.md)
- [BoltzGen 最新进展](../boltzgen/sessions/boltzgen_progress_20260826.md)
- [BindCraft 最新进展](../bindcraft/sessions/bindcraft_progress_20260826.md)

> HTML 是生成日期当天的只读快照，不会自动随项目状态更新。当前状态以根 README、路线 README、
> 正式执行合同和最新运行回执为准。结构置信度、界面分数、接触数与均方根偏差都是计算代理，
> 不等于结合、解离常数、选择性或实验成功。

## 共同知识

| 内容 | 类型 | 日期 | 说明 |
|---|---|---:|---|
| [AI 实施架构](../shared/reports/html/glp1_ai_implementation_blueprint_20260818.html) | 概念说明 | 2026-08-18 | 数据、模型、推理、训练边界与评价的总体设计 |
| [AI 设计知识图谱](../shared/reports/html/glp1_ai_design_knowledge_graph_20260819.html) | 概念说明 | 2026-08-19 | 术语、两条设计路线、数据与实验闭环 |
| [项目会话与资源总览](../shared/reports/html/glp1_project_session_resource_overview_20260826.html) | 历史状态快照 | 2026-08-26 | 截至生成日的会话、进展与资源索引 |

## BoltzGen / VHH

| 内容 | 类型 | 日期 | 可支持的结论 |
|---|---|---:|---|
| [MVP 数据资产](../boltzgen/reports/html/boltzgen_mvp_data_assets_20260818.html) | 数据与质量控制 | 2026-08-18 | 数据来源、格式、输入输出角色、清理与样例 |
| [数据流与算法原理](../boltzgen/reports/html/boltzgen_vhh_glp1_algorithm_20260819.html) | 算法说明 | 2026-08-19 | VHH 骨架、GLP-1、生成、逆折叠、复折叠与过滤逻辑 |
| [SAbDab2 VHH 骨架筛选](../boltzgen/reports/html/sabdab2_vhh_scaffold_screening_20260819.html) | 数据与质量控制 | 2026-08-19 | SD-H/VHH 筛选漏斗、结构 QC 与旧 12 骨架来源 |
| [Mac MPS 冒烟](../boltzgen/reports/html/boltzgen_nanobody_mps_smoke_20260819.html) | 历史运行快照 | 2026-08-19 | 工程链路跑通；严格过滤 `0/2`，不是官方 CUDA 等价复现 |
| [旧 12 骨架第一轮](../boltzgen/reports/html/boltzgen_old12_glp1_round1_20260819.html) | 历史运行快照 | 2026-08-19 | 24 条候选完成，严格过滤 `0/24` |
| [旧 12 骨架 Mac 增强轮](../boltzgen/reports/html/boltzgen_old12_glp1_mac_enhanced_20260820.html) | 历史运行快照 | 2026-08-20 | 48 条主候选严格过滤 `0/48`；独立深度探针 `0/4` |

## BindCraft / de novo 小型结合蛋白

| 内容 | 类型 | 日期 | 可支持的结论 |
|---|---|---:|---|
| [GLP-1 选择性输入与原型审计](../bindcraft/reports/html/bindcraft_glp1_selectivity_input_audit_20260826.html) | 输入审计 | 2026-08-26 | Notebook 未执行；输入与评价合同仍需修订，不能评价候选质量 |

## 目录范围

本模块登记 10 份项目生成 HTML，共 `8,455,193` bytes：Shared 3 份、BoltzGen 6 份、
BindCraft 1 份。仓库外发现的同名页面均与上述规范文件哈希相同，因此不重复提交。
SAbDab2 网站抓取空壳、Python 环境帮助页和第三方软件文档不属于项目生成报告，也不进入目录。

## 来源与复现

报告来源链按以下顺序阅读：

```text
HTML → artifact manifest → 报告构建脚本 → 数据/资源 manifest → 外部来源 revision
```

早期两份 Shared 页面没有专属 artifact，目录中明确标记为 `provenance_partial`，不会倒填不存在的
生成参数。其余报告可从 [HTML—来源映射](resources/manifests/html_report_sources_20260901.csv)
定位 artifact 与一次性构建脚本。

GitHub 默认展示 HTML 源码；需要交互查看时，请下载仓库或单个 HTML 后在浏览器中打开。
