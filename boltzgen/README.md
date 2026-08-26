# BoltzGen / VHH 工程主线

## 状态

本路线已完成数据准备、VHH 骨架筛选和三批 Mac 推理验证，尚未启动 Linux/NVIDIA 正式 campaign，也没有计算严格通过或实验验证的候选。现阶段使用冻结预训练权重做推理，不训练 BoltzGen 基础模型。

## 关键结果

| 尝试 | 输入 | 产出 | 严格通过 | 解释 |
|---|---|---:|---:|---|
| Mac 冒烟 2026-08-19 | GLP-1(7–36) 单几何 + 7XL0 示例骨架 | 2 候选 | 0/2 | 证明实验性 MPS 链路可跑通 |
| 旧 12 骨架第一轮 2026-08-19 | 12 VHH × 单一 GLP-1 正靶 | 24 候选 | 0/24 | 主要暴露复合物 RMSD 与热点覆盖问题 |
| Mac 增强 2026-08-20 | 12 VHH × diverse/adherence | 48 主候选、96 复折叠样本 | 0/48 | 两个 checkpoint 均无幸存者；深度探针另为 0/4 |

## 主代码

- [`main/mvp_data_assets_20260818/`](main/mvp_data_assets_20260818/)：清理并封装 MVP 数据资产。
- [`main/mvp_mac_20260818/`](main/mvp_mac_20260818/)：Mac 冒烟入口与只读分析。
- [`main/sabdab2_scaffold_curation_20260819/`](main/sabdab2_scaffold_curation_20260819/)：下载 SAbDab2 SD-H 快照、构建 VHH 数据库并验证 BoltzGen 输入。
- [`main/round1_old12_mac_20260819/`](main/round1_old12_mac_20260819/)：旧 12 骨架第一轮准备、运行和分析。
- [`main/enhanced_old12_mac_20260820/`](main/enhanced_old12_mac_20260820/)：Mac 增强轮的独立 checkpoint 运行、日志和分析。
- [`main/asset_validation_20260820/`](main/asset_validation_20260820/)：结构资产登记与非破坏式验证。

每个尝试包都提供带日期的公开执行入口；`scripts/` 内保留原实现文件名，避免破坏其已有相对路径和 manifest 合同。

## 正式路线

正式执行以 [无上下文实施方案](plans/boltzgen_glp1_vhh_execution_plan_20260826.md) 为权威合同：

1. 固定 BoltzGen `v0.3.2` 和 Linux/NVIDIA 环境。
2. 旧 12 骨架始终作为 baseline；新 17 骨架单独走准入 probe。
3. 以 `10 → 240 → 2,400 → 12,000` 分层门控推进，而不是直接全量运行。
4. 对正靶、9–36 反靶和挑战面板采用冻结且一致的复折叠评价。
5. AIV4 通过后才建立 96–192 条配对实验面板。
6. 有实验标签后训练项目级重排序器；当前不更新 BoltzGen 权重。

建议正式生成使用 4–8 张 80 GB 级 A100/H100，并用 pilot 实测吞吐后再排期。完整 GPU、环境和统计合同见实施方案；不能把 Mac 用时直接外推到集群。

## 数据与报告

- [资源索引](resources/manifests/boltzgen_resources_20260826.csv)
- [VHH 基线骨架登记表](resources/data/SAbDab2_VHH骨架登记表_20260819/旧12骨架登记表_20260819.tsv)
- [第一轮候选指标](resources/data/BoltzGen旧12骨架第一轮摘要_20260819/候选指标_20260819.csv)
- [Mac 增强运行摘要](resources/data/BoltzGen旧12骨架Mac增强摘要_20260820/运行摘要_20260820.json)
- [HTML 报告目录](reports/html/)

大体积运行资产与原始结果不进入 Git；恢复路径和校验清单写入资源索引。
