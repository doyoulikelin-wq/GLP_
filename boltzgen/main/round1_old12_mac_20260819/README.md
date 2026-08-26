# 旧 12 VHH 骨架第一轮 Mac 推理（2026-08-19）

公开入口：`运行旧12骨架第一轮_Mac_20260819.py`。内部实现文件名保持不变，以免破坏历史 manifest 和脚本相对路径。

## 输入与输出

- 输入：单一 PDB 6X18 派生 GLP-1(7–36) 几何、10 PRIMARY + 2 RESERVE VHH 骨架；
- 每骨架 2 条候选，共 24 条；
- 24/24 链路完成，严格过滤 0/24。

该轮没有输入 9–36 反靶或多构象，所以不能评价选择性。它使用冻结预训练权重做推理，不是训练。

```bash
python3 运行旧12骨架第一轮_Mac_20260819.py prepare
python3 运行旧12骨架第一轮_Mac_20260819.py run -- --start-rank 1 --end-rank 12
python3 运行旧12骨架第一轮_Mac_20260819.py analyze
```

完整 inputs/runs/vendor/环境需按 manifest 外部恢复。结果摘要见 [`../../resources/data/BoltzGen旧12骨架第一轮摘要_20260819/`](../../resources/data/BoltzGen旧12骨架第一轮摘要_20260819/)。
