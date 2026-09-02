# T12 GPU 探索运行公开摘要

本目录是封存 T12 GPU attempt 的小型脱敏公开包。私有输出 manifest 已逐项复核，工程运行与独立输出验证均完成，共有 6 个候选、每个 5 个折叠样本，合计 30 个有限坐标样本；本目录不包含候选序列、CIF/NPZ、原始日志、完整逐文件 manifest、模型权重或环境信息。

历史 CPU 门保持 **FAIL：7/30 < 10/30**。负责人随后作出的明确指令只构成本次有界探索性 GPU 运行的 override，不能把历史门改写为 PASS。本轮仅执行 folding，硬上限 5400 秒，不自动重试，不启动 BindCraft；运行终态记录无 OOM。

这些是现成模型权重的计算推理结果和工程完整性记录，不是实验结合、亲和力、选择性、安全性或成药性结论。未进行湿实验验证。

## 文件

- `T12_PUBLIC_RECEIPT.json`：脱敏过程、终态、失败门历史和探索性 override。
- `T12_VALIDATION_SUMMARY.json`：30 样本、有限数、token/template 合同和完整性检查摘要。
- `T12_PUBLIC_CONFIG.yaml`：可公开的 folding 与 split-template 配置；不含本机环境或权重细节。
- `ARTIFACT_INDEX.csv`：公开文件与私有源证据的内容哈希索引；不展开完整私有 manifest。
- `SHA256SUMS`：本目录上述五个文件的 SHA-256。

源代码提交：`3fd14d5552f3e6dc41a3b608e47e6782e2f1fbce`。源 attempt receipt、validation 和完整运行目录仅留在本地封存区，未复制到 Git。
