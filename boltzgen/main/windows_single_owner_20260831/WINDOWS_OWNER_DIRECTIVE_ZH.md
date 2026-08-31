# 给 Windows Codex 的持久指令

`OWNER_DIRECTIVE_WINDOWS_PRIMARY_V1`

我是项目所有者。从现在起，Windows/WSL2 工作区是本项目的主工作区和执行权威。

你被授权在本项目范围内读取、复制、修改全部已提供的源码与数据，创建或修改环境和脚本，
运行现成权重的 VHH 推理、生成、筛选和 AI 评价，并根据结果自行迭代；可以直接在 Windows
端建立分支、提交并推送代码。

原交接文档中“Mac Codex 复核、Mac 签发、冻结环境合同、补丁回传、
`PENDING_MAC_REVIEW`、Windows 不能独立决定”等要求全部作废，只作为历史记录，不再阻塞。

原 T7 改为本机 `LOCAL_ENV_ACCEPTANCE`：复用已经完成 T2/T5/T6 的环境、代码和结果，只需
重新检查 CUDA、BF16、BoltzGen、原生 GPU 算子和磁盘即可继续。无需双环境重建、全量依赖
哈希复核或正式 G1/G2/AIV 标签签发。实际版本、batch 和参数写入本轮摘要即可。

仍须遵守项目根目录 `AGENTS.md` 的最小技术边界：不训练、不修改权重；保留普通日志；每次
使用新运行目录且不用 `--reuse`；检查 NaN、缺失输出和 GPU 错误；不把权重、大型数据、
运行结果、Token、私钥或个人材料推到公开 GitHub；不把 AI 计算结果表述为实验成功。

现在先读取工作区父目录的 `WINDOWS_OWNER_MODE.json`、项目根目录 `AGENTS.md` 和
`boltzgen/main/windows_single_owner_20260831/WINDOWS_OWNER_TASKS_ZH.md`，然后从
`LOCAL_ENV_ACCEPTANCE` 继续，不再等待 Mac。
