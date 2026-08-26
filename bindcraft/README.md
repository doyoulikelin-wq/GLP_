# BindCraft / de novo 小型结合蛋白探索线

## 状态

当前只有一个研究原型 Notebook、8 个小型 PDB 输入和一次静态审计。Notebook 的 12 个代码单元均未执行，没有候选结构、统计 CSV、日志或实验结果，因此现在不能评价设计命中率或选择性。

## 已有内容

- [GLP-1 选择性原型 Notebook](main/active_glp1_selectivity_20260823/bindcraft_active_glp1_selectivity_prototype_20260823.ipynb)
- [输入审计 Notebook](notebooks/bindcraft_input_audit_20260826.ipynb)
- [输入审计 HTML](reports/html/bindcraft_glp1_selectivity_input_audit_20260826.html)
- [8 个 PDB 输入](resources/data/GLP1选择性靶标面板_20260825/)
- [靶标逐文件 manifest](resources/manifests/bindcraft_glp1_target_panel_20260825.csv)
- [路线资源索引](resources/manifests/bindcraft_resources_20260826.csv)

## 原型算法

BindCraft 以目标结构为条件，通过 AlphaFold2 反向传播优化 de novo binder，再用 ProteinMPNN 重设计序列、AlphaFold2 复预测和 PyRosetta 界面过滤。项目原型在此基础上加入多正靶、GLP-1(9–36) 与同源肽反靶，以及 His7/Ala8 接触要求。

这条线不是 VHH 设计：它搜索全新小型蛋白骨架，空间更大、表达与可开发性风险也更高。

## 当前必须修复

- 所有正、负靶使用完全相同的模型集合、recycle、随机性状态和汇总规则。
- 补全或替换 GIP 30/42 和 oxyntomodulin 26/37 的不完整坐标。
- 明确 9–36 是坐标删除派生对照，不冒充独立实验结构。
- 分开“通过门”和“排序分”；删除默认参数下冗余的 margin 门或改变阈值合同。
- 重构 N 端接触排序；当前通过者都固定得到 `+0.05`，没有区分力。
- 固定 BindCraft、AlphaFold2、ProteinMPNN、PyRosetta、模型资产、seed 状态与文件哈希。
- 对 GLP-1 C 端酰胺和其他末端化学给出一致、可审计的表达。

## 下一次尝试

修复后先对每个正构象运行 30–50 条轨迹，完整保存：输入 manifest、固定代码 revision、环境、stdout/stderr、trajectory CSV、设计 PDB、跨靶复预测和筛选原因。小试门未通过前，不启动大规模 campaign，也不下单合成。

建议生产环境遵循上游要求使用 Linux、CUDA 兼容的 NVIDIA GPU；上游 README 推荐至少 32 GB 显存。当前原型没有在本机运行。
