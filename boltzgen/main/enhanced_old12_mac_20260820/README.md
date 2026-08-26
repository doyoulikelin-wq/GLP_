# 旧 12 VHH 骨架 Mac 增强推理（2026-08-20）

公开入口：`运行旧12骨架Mac增强_20260820.py`。

## 已有结果

- diverse 与 adherence 两个独立进程支路共 24/24 任务完成；
- 48 个主候选、96 个复折叠样本、0/48 严格通过；
- 7XL0 adherence 深度探针 4 条、0/4；
- 同进程双 checkpoint 压力档位因 swap 增加超过 4 GiB 被安全停止，失败证据保留。

推荐历史重放命令：

```bash
python3 运行旧12骨架Mac增强_20260820.py prepare
python3 运行旧12骨架Mac增强_20260820.py run -- --profile balanced_diverse_all12 --start-rank 1 --end-rank 12 --stop-on-error
python3 运行旧12骨架Mac增强_20260820.py run -- --profile balanced_adherence_all12 --start-rank 1 --end-rank 12 --stop-on-error
python3 运行旧12骨架Mac增强_20260820.py analyze
```

这不是官方 CUDA 等价复现。主分析摘要位于 [`../../resources/data/BoltzGen旧12骨架Mac增强摘要_20260820/`](../../resources/data/BoltzGen旧12骨架Mac增强摘要_20260820/)。
