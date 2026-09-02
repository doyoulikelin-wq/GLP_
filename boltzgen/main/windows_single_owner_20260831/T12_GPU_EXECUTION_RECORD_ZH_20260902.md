# T12 GPU 执行记录（2026-09-02）

本文只记录执行过程、工程终态和公开边界，不对生成结构作进一步科研结果解释。

## 1. 授权与固定边界

- 历史 CPU 门保持 `FAIL：7/30 < 10/30`，没有追溯性改阈值或改写为 PASS。
- 负责人在本轮明确授权一次 T12 GPU 探索性 override。
- 固定范围：split-template folding；6 个候选；每个 5 个样本；合计 30 个。
- 总硬上限 5400 秒；单 GPU；不 `--reuse`；不自动追加 seed；不启动 BindCraft。
- 使用现成权重推理，没有训练、微调或修改权重。

## 2. 源码与测试

- 初始冻结 runner 提交：`f3fd57bfd214a523f7584aa0f2d66a6df915f00a`。
- 日志零失败判定修复提交：`3fd14d5552f3e6dc41a3b608e47e6782e2f1fbce`。
- 修复后的完整 owner CPU 测试：`84 passed`，另有 1 条已知 `pynvml` 弃用警告。
- 真实 T11 六候选 CPU preflight：每个模板形状 `[2,151]`，slot 可见数 `[30,91]`，
  CDR 在两个 slot 中均不可见。

## 3. 执行历史

### Attempt 1：按失败原样保留

- 逻辑位置：`workspace://gpu_work/owner_mode/t12_split_template_gpu/7xl0_highcontact_split_template_f5_20260902/attempt_20260902T015819Z/`
- GPU folding 实际完成 6/6，BoltzGen 报告 `Number of failed structure predictions: 0`。
- runner 的初版日志规则把这条零失败摘要误判为 fatal，因此终态按 fail-closed 规则封存为
  `T12_SPLIT_TEMPLATE_FAILED`；未覆盖、未改写、未复用其输出。
- 私有输出 manifest SHA-256：`ccae7cc9ffdd2a712667e887ce5203499c0c14aff8d90316e225a1e7e102b5a5`。

随后将 fatal 规则收紧为只匹配非零失败计数，并加入“0 不失败、1 失败”的回归测试。

### Attempt 2：完成终态

- 逻辑位置：`workspace://gpu_work/owner_mode/t12_split_template_gpu/7xl0_highcontact_split_template_f5_20260902/attempt_20260902T020257Z/`
- 源码提交：`3fd14d5552f3e6dc41a3b608e47e6782e2f1fbce`。
- 终态：`T12_SPLIT_TEMPLATE_COMPLETE`；总退出码 0；folding 退出码 0。
- 输出闭合：6 个候选 × 5 个样本 = 30 个；独立 validator `PASS`。
- 有限数检查：PASS；OOM：false；超时：false；终态 GPU compute process：0。
- 输入前后哈希一致：true；运行资产前后哈希一致：true；源码 commit/tree 未变化：true。
- 总耗时：142.697647 秒；5400 秒硬上限得到遵守。
- 私有输出 manifest 共绑定 50 个文件，SHA-256：
  `0cdf473a6e204db1189c5d405ff2a387210f19e30a446743a5758f74b88a0b06`。

## 4. GitHub 公开内容

公开目录 [`reports/t12_gpu_public_20260902/`](reports/t12_gpu_public_20260902/) 只包含：

- 脱敏过程收据；
- 30 样本工程验证摘要；
- 不含本机路径或权重路径的公开配置；
- 私有证据的聚合索引；
- 公开文件 SHA-256 清单。

完整 CIF/NPZ、候选序列、原始日志、GPU 监控、完整逐文件 manifest、权重和环境没有提交到
GitHub。以上结果是计算推理与工程完整性记录，不是实验结合、亲和力、选择性、安全性或
成药性结论。
