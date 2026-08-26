# BoltzGen Mac MVP（结果日期 2026-08-19）

公开入口：`运行MVP_Mac_20260819.py`；实现为 `scripts/run_mvp.py`，只读分析为 `scripts/analyze_results.py`。

## 已有结果

- 输入检查、扩散设计、逆折叠、复合物重折叠、分析和过滤均完成；
- 生成 2 条候选，严格过滤 `0/2`；
- 两条候选均未满足 2.5 Å 重折叠均方根偏差门，也未覆盖 His7/Ala8 热点。

## 恢复要求

本仓库不含约 1.65 GB 环境、上游 vendor、副本 checkpoint、`mols.zip`、目标/骨架 CIF 或完整输出。按资源索引恢复这些文件后，再运行：

```bash
python3 运行MVP_Mac_20260819.py --through execute
```

这是实验性 Apple Metal Performance Shaders 路径，不是官方 Linux/NVIDIA 生产基线。最终报告见 [`../../reports/html/boltzgen_nanobody_mps_smoke_20260819.html`](../../reports/html/boltzgen_nanobody_mps_smoke_20260819.html)。
