# BindCraft 路线会话进展

## 结论

BindCraft 当前是“设计原型已写、输入已审计、尚未运行”的探索线。现有文件不能支持候选质量、命中率或选择性结论；修复输入与评价合同后，才适合启动小规模轨迹。

## 原型做什么

原型保留 BindCraft 的 de novo 小型结合蛋白流程：AlphaFold2 反向传播产生候选骨架与序列倾向，ProteinMPNN 在骨架上重设计序列，AlphaFold2 重新预测复合物，PyRosetta 计算界面和结构过滤。项目额外加入：

- 三个 GLP-1(7–36) 正靶几何；
- 两个坐标删除得到的 GLP-1(9–36) 派生对照；
- GIP、glucagon 和 oxyntomodulin 同源肽反靶；
- His7/Ala8 的 N 端接触计数；
- 正靶最小 interface predicted template modeling score、负靶最大同类分数与差值门。

## 审计发现

- 12 个代码单元均无执行计数或输出。
- 没有生成候选、结果 CSV、日志、AlphaFold2 复预测产物、PyRosetta 结果或实验数据。
- 主正靶按两个模型平均，其他正/负靶只用一个模型，分数不可直接比较。
- 默认 `positive ≥ 0.50` 与 `negative ≤ 0.35` 已蕴含差值至少 0.15，额外 margin 门在默认值下冗余。
- N 端接触数最多为 2，通过门要求至少 2，且加分为 `0.025 × contacts`；所以所有通过者都固定加 0.05，无法排序。
- GIP 当前只有 30/42 个残基，oxyntomodulin 只有 26/37 个残基。
- 两个 9–36 文件来自对应 7–36 坐标删除，不是独立实验结构。
- 1D0R 多模型属于同一 NMR deposition，不能当成独立生物学重复。
- Notebook 动态获取未固定的上游 `main` 和外部资产，尚未形成可复现生产环境。

## 下一步

1. 补全或替换不完整同源肽结构，明确每个末端化学状态。
2. 固定 BindCraft release/tag、AlphaFold2、ProteinMPNN、PyRosetta、输入文件和哈希。
3. 所有正靶和负靶使用相同模型集合、循环次数、随机性状态和汇总规则。
4. 重构选择性打分：把通过门与排序分开，增加负靶预测置信度和重复候选处理。
5. 先按每个正构象 30–50 条轨迹试跑，完整保存日志、CSV、PDB 和环境；小试通过前不扩大规模、不下单。

## 资源入口

- [原型 Notebook](../main/active_glp1_selectivity_20260823/bindcraft_active_glp1_selectivity_prototype_20260823.ipynb)
- [输入审计 Notebook](../notebooks/bindcraft_input_audit_20260826.ipynb)
- [靶标面板清单](../resources/manifests/bindcraft_glp1_target_panel_20260825.csv)
- [路线资源索引](../resources/manifests/bindcraft_resources_20260826.csv)
