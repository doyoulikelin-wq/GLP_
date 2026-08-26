# 第三方来源与许可提示

本文件是来源索引，不替代各上游许可证或法律审查。项目整体尚未声明许可证。

## BoltzGen

- 源码：<https://github.com/HannesStark/boltzgen>
- 固定版本：`v0.3.2`，commit `31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0`
- 上游源码许可证：MIT；本仓库不复制 vendor 源码树。
- Mac 历史尝试使用实验性 MPS commit `592317f0f5582730b28c144267a15631c07fcb94`，仅作为历史运行身份。
- 发布模型和 inference-data 的许可按固定 Hugging Face revision 元数据记录；不得仅由源码 MIT 推定任意第三方资产可再分发。

## BindCraft

- 源码：<https://github.com/martinpacesa/BindCraft>
- 当前参考 release：tag `v.1.5.3`，commit `a234a8d3af9fe3d2724209aa91d930280b72048b`
- 上游仓库源码标示 MIT。
- 现有项目原型没有固定上游 revision，不能倒填成已使用该 release。
- AlphaFold 参数、ProteinMPNN、PyRosetta 与其他依赖有独立条款；尤其 PyRosetta 的商业使用需要单独核验。

## 结构与生物数据库

- RCSB Protein Data Bank archive/API：依据其使用政策记录为 CC0-1.0；仍应引用原结构作者与 PDB ID。<https://www.rcsb.org/pages/usage-policy>
- SAbDab2 快照：项目快照声明 CC BY 4.0；公开衍生表保留 SAbDab2 attribution、快照日期和 API 版本。<https://sabdab.opig.stats.ox.ac.uk/>
- UniProt：可版权数据库内容按 CC BY 4.0 及当前官方许可处理。<https://www.uniprot.org/help/license>
- PubChem：聚合内容的来源和许可可能依内容元素而异；保留 contributor/provenance，不笼统推定所有字段可无条件再分发。<https://pubchem.ncbi.nlm.nih.gov/docs/downloads>

## 报告内容

项目 HTML、Markdown、Python 和 Notebook 为项目生成内容，但其中引用的论文、图示概念、软件名称、数据条目和源码链接仍归相应权利人。本仓库不声明对第三方内容的额外权利。
