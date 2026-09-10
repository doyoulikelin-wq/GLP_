# VHH 姿态方法 V2：同源目标锚定与逐结构适用域

这是新的方法开发，不修改 `summarize_vhh_pilot.py`、旧 `POSE_RULE` 或历史结果。
旧 pilot 只能标为 `RETROSPECTIVE_METHOD_DEVELOPMENT`，不能据此解锁旧阶段门。
未来新计划可在运行前冻结此规则及源码哈希，标为 `PROSPECTIVE_EXPLORATORY`。

## 为什么不是把旧 0.001 Å 门槛简单放宽

V2 不再把首个生成候选当共同参考，改用所有候选共享的原始 `target.cif`。
采用显式 GLP-1 生物编号 7–36 与完整30残基序列身份检查，每残基用 N/CA/C/O
四个骨架原子的中心，避免把作者编号或数组位置误当生物编号。

先仅用中央13–33段求不含镜像的 Kabsch 刚体对齐，再分别检查中央段、全目标和
识别端部7–12的偏移。这样，端部一个残基严重偏移不能被整体平均掩盖。
目标参考与候选的对齐点还必须通过非退化／条件数检查。

初始工程适用域为：中央段 RMSD ≤0.5 Å、全目标 RMSD ≤1.0 Å、端部7–12任一
残基偏移 ≤1.0 Å；中央点云第二／第一奇异值比 ≥0.02。
这些数值是此次方法开发的明确工程选择，不是实验验证的生物学阈值，也不声称它们
在查看旧试点之前已经存在。通过合成测试只能说明实现符合定义，不能证明阈值生物学有效。

## 输出与进度边界

- 适用域内：输出 CDR 骨架中心位置及 framework→CDR 单位方向。
- 适用域外：只将该结构的姿态描述设为 `null`，注明原因；仍独立报告有效原子接触。
- 无效残基映射、非有限坐标或不明确原子结构：输入错误，拒绝计算，不伪装成可用结果。
- 8 Å位置／45°方向的完整连接几何分组只作描述；不再要求至少两类才继续。
- 没有可比较姿态时类别数是 `null`，不是零；不是候选失败，更不是不结合。
- `old_gate_unlock_allowed=false`、`biological_pass=false` 始终保留。

## API 与依赖

`scripts/evaluate_vhh_pose_v2.py` 使用 NumPy、Gemmi，以及已有
`evaluate_vhh_epitope.py` 的标准重原子／映射验证和独立接触指标。

```python
reference = load_reference(original_target_cif, chain_id="P")
result = evaluate_fold(design_arrays, fold_arrays, reference,
                       usage="PROSPECTIVE_EXPLORATORY",
                       geometry_source="generated_input")
# geometry_source="free_fold_samples" 可描述每次自由复折叠。
```

调用方应在计划中冻结 `POSE_V2_RULE`、`rule_digest()`、原始参考文件 SHA256 和
实现源码 SHA256，并保证所有候选使用同一个原始参考。模块不写文件、不运行 GPU。
当前项目原始 `target.cif` 的链名是 `P`，不是生成后复合物中的 `A`；必须明确传入。
返回逐残基和逐样本结果属于私有分析；公开报告只应允许列表式聚合输出。

## 限制与验证

仅支持完整活性 GLP-1 目标；不为截短态静默补残基。两中心方向丢失绕此轴的转动，
分组也会受 CDR长度与构象影响，因此不能称为完整结合模式分类。
中央参考并非天然恒定结构；当它严重变化时姿态不可比，但距离接触仍可独立描述。
实际亲和力、选择性、末端酰胺／质子化、表面埋藏或实验稳定性均未得到验证。

专属合成测试覆盖刚体不变性、轻微／大范围形变、整体均方偏差掩盖端部严重形变、
退化与近共线对齐、无效编号、NaN、不可定义方向，以及姿态分组不作为科学必过门。
