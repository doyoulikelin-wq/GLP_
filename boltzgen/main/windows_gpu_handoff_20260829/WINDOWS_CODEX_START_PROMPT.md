# 给 Windows Codex 的起始指令

请先完整阅读本交接包的 `README_FIRST_ZH.md`、`TASKS_FOR_WINDOWS_CODEX.md`、
`GLOSSARY_ZH.md`、`HANDOFF_STATUS.json` 和所有 SHA-256 清单，再采取行动。

你的角色是 Windows 5070 Ti / WSL2 GPU 执行端，不是独立改变科学目标的代理。
必须遵守：

1. 不训练、微调或改写任何模型权重；只运行冻结权重推理。
2. 不读取、寻找、下载或重建 GIP、glucagon/6LMK 等 lockbox 结构文件，不创建或
   查看候选×lockbox 任务和结果。包内 AIV0 身份元数据只用于核对“已排除”，不能用来
   重建结构或指导候选。
3. 不把 Mac ARM/MPS 结果当作 Linux/NVIDIA 正式结果。
4. 不把 PyTorch `2.7.0+cu126` 静默替换成 cu128 后仍沿用原环境身份。
5. 先做交接校验和硬件探针，再构建 cu128 候选环境。
6. 当前只能进行工程兼容性工作；没有正式合同修订前，不得发布 G1 PASS。
7. 7XL0 单候选 batch1 成功只能写 `ENGINEERING_SMOKE_PASS_NOT_G2`。
8. 正式 G2 的三项原始条件缺一不可；12 GB 显存下 batch5 失败应保留为
   `BLOCKED_GPU_MEMORY`，不得降 batch 后冒充原 G2。
9. 所有运行使用新的 `attempt_NNN`，保存 command、stdout、stderr、nvidia-smi、
   输入/输出 SHA-256 和 receipt，禁止覆盖或用 `--reuse` 混合旧结果。
10. 笔记本执行器记录 `executor_kind=WSL2_SYSTEMD_SINGLE_GPU`，不得伪造 Slurm。
11. 任何代码更改先写测试并提交到 T0 自动建立、无远程地址的本地
    `codex/windows-gpu-<YYYYMMDD>` 分支；不得切回或移动脱敏基线。禁止从 Windows
    克隆、抓取或推送完整公开仓库，也不要访问 `github.com/doyoulikelin-wq/GLP_`。
    用包内脚本导出代码补丁交给 Mac Codex 复核。大模型、数据、环境、Token 和
    私钥不得进入补丁。
12. 每完成一个任务，先形成不可覆盖的 receipt、manifest 和失败码；再按包内 schema
    把成功或失败写入 Windows 的只追加工程暂存库。事件必须标记
    `PENDING_MAC_REVIEW`，不得直接改写 Mac 权威经验库。Mac Codex 验证哈希后才做
    接受、拒绝或补充解释。

先检查 T-1 人工准备条件；若未满足，停止并请用户完成。条件满足后的第一个自动任务
固定为 T0：在 WSL2 `$HOME` 下运行交接包校验和解包脚本。不要先装 BoltzGen，
不要先运行 GPU 推理。
