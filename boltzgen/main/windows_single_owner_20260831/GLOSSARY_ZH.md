# Windows 单机所有者模式术语表

- **Windows 单机所有者模式**：Windows/WSL2 工作区自己保存完整代码、数据、
  日志和 Git 历史，可以自行推理、评价、调整、提交和推送，不等待 Mac 审批。
- **Codex**：在项目工作区内读写文件、运行命令并协助开发的 AI 编程代理。
- **AI**：Artificial Intelligence，人工智能。本项目先做 AI 计算验证，不把它写成实验结论。
- **`AGENTS.md`**：Codex 进入项目时读取的项目级持久指令。它界定项目工作权限，
  但不能越过 Codex 应用或操作系统的沙箱。
- **permission / 权限**：Codex 能否读写文件、访问网络和执行命令的本机设置。
- **sandbox / 沙箱**：限制 Codex 可读写路径和可执行操作的安全边界。
- **Danger Full Access**：Codex 的本地高权限模式，移除本地沙箱限制；只应在自己的
  可信机器上开启。
- **Never approval / 不询问**：执行命令时不再逐次弹出审批。这不会取消 GitHub 自己的
  账户登录。
- **WSL2**：Windows Subsystem for Linux 2，Windows 内的 Linux 运行环境。
- **Ubuntu**：本项目在 WSL2 内使用的 Linux 发行版。
- **GPU**：Graphics Processing Unit，图形处理器；本项目用 RTX 5070 Ti 运行神经网络推理。
- **VRAM / 显存**：GPU 自己的高速内存，与电脑的 RAM（主内存）不同。
- **CUDA**：NVIDIA 的 GPU 计算平台。
- **BF16**：bfloat16，一种 16 位数值格式，用于降低显存占用并加快计算。
- **cuEquivariance**：BoltzGen 依赖的 GPU 几何运算库。
- **kernel / 原生 GPU 算子**：直接在 GPU 上执行的底层计算函数，不是 Linux 或 Windows
  操作系统内核。
- **BoltzGen**：本项目使用的生物分子结构生成与评价软件。
- **VHH**：骆驼科单域抗体的可变结构域，也常称 nanobody（纳米抗体）。
- **checkpoint / 权重**：已经训练好的模型参数文件。本项目只读取，不重新训练或修改。
- **training / 训练**：用数据更新模型权重的过程；本项目不进行。
- **fine-tuning / 微调**：在现成模型上继续训练并修改权重；本项目也不进行。
- **inference / 推理**：将输入交给现成权重，生成候选结构、序列或评分。
- **runtime asset / 运行资产**：推理时必须存在的权重、分子字典和配套文件。
- **`mols.zip`**：BoltzGen 使用的分子与化学字典压缩包，属于运行资产，不是候选结果。
- **`--reuse`**：BoltzGen 复用旧运行输出的命令选项。本项目禁用，避免新旧结果混在一起。
- **scaffold / 骨架**：VHH 中作为设计起点的结构框架。
- **candidate / 候选**：由模型生成、待进一步评价的 VHH 序列及对应结构。
- **batch / 批大小**：一次并行送入 GPU 的样本数；数值越大通常越占显存。
- **sampling step / 采样步数**：生成过程逐步更新的次数；增加通常会增加运行时间。
- **recycling / 循环次数**：把一次预测再送回模型继续精化的次数。
- **refold / 复折叠**：从完整候选序列再次预测三维结构，用于检查候选的稳定性代理指标。
- **T7**：旧任务序列的第 7 步。旧版要求 Mac 签发 GPU 环境合同；所有者模式将它
  替换为本机 `LOCAL_ENV_ACCEPTANCE`。
- **LOCAL_ENV_ACCEPTANCE / 本机环境验收**：检查 GPU、CUDA、BF16、原生算子、依赖、
  运行资产和磁盘后，当场决定能否继续；不需要 Mac 收据。
- **G1 / G2**：旧方案的 GPU 环境门和单 GPU 端到端验收门。所有者模式保留相应
  技术检查，但不等待跨机器签发，也不沿用旧的“正式通过”标签。
- **AIV**：Artificial Intelligence Verification，AI 验证；对输入、推理、评价和经验反馈做
  分阶段计算检查，不是模型训练或实验结合验证。
- **AIV0**：AI 验证第 0 阶段，主要检查输入数据的身份、来源和完整性。
- **AIV1**：AI 验证第 1 阶段，检查固定候选在多个开发结构状态上的统一推理和评价。
- **AIV4**：AI 验证第 4 阶段，原意是在所有规则冻结后才使用从未调参的锁箱集合。
- **lockbox / 锁箱**：在候选、阈值和排序规则确定之前不开启的保留数据。如果提前用于
  调参，它就必须改称开发对照，不能再作为无偏最终验证。
- **positive state / 正向开发态**：希望候选识别的目标分子结构状态。
- **countertarget / 反对照**：用来检查潜在非目标识别风险的开发对照结构。
- **GLP-1**：Glucagon-Like Peptide-1，胰高血糖素样肽-1；本项目的主要目标。
- **GLP-2**：Glucagon-Like Peptide-2，胰高血糖素样肽-2；本项目中作为开发期反对照。
- **anchor / 锚定候选**：选定后在后续多状态评价中重复比较的候选。
- **mmCIF / CIF**：Macromolecular Crystallographic Information File，保存原子、残基、链和
  三维结构坐标的文本文件格式。
- **PDB / `7XL0` / `6XYM`**：PDB 是 Protein Data Bank（蛋白质结构数据库）；`7XL0`
  和 `6XYM` 是其中两个三维结构的唯一编号。
- **NPZ**：NumPy 的压缩数组文件格式，本项目用它保存坐标样本和数值指标。
- **NaN / Inf**：Not a Number（非数值）/ Infinity（无穷大），都表示数值输出异常。
- **OOM**：Out Of Memory，内存或显存不足。
- **SHA-256**：根据文件内容计算的 256 位指纹。本包用它发现传输损坏，不把它当作
  签名、授权或审批。
- **manifest / 清单**：列出文件路径、大小或指纹的文件。
- **Git**：保存代码版本历史的工具。
- **Git bundle**：把 Git 提交和分支历史封装成可离线克隆的单个文件，不包含密钥。
- **sanitized repository / 脱敏仓库**：旧交接包为 Windows 重新建立的单一根提交仓库，
  不含原项目完整 Git 历史。
- **patch / 补丁**：记录两个文件树之间代码差异的文件。迁移脚本用它把 Windows
  已完成的 T3–T6 改动放回完整项目历史，不再用它向 Mac 申请审批。
- **commit / 提交**：一次带唯一指纹的代码快照。
- **branch / 分支**：一条独立代码工作线。
- **origin / 远程仓库**：本地 Git 仓库关联的 GitHub 地址。
- **GitHub**：保存 Git 代码历史的在线服务。它的账户登录是访问控制，不是 Mac 项目审批。
- **Token / 访问令牌**：可代表账户访问 GitHub 等服务的秘密凭据；不应从 Mac 复制或
  放入交接包。
- **JSON / JSONL**：JSON 是机器可读的结构化文本；JSONL 是每行一个 JSON 对象的日志格式。
- **stdout / stderr**：程序的标准输出和标准错误日志。
- **exit code / 退出码**：命令结束时返回的整数；通常 0 表示成功，非 0 表示失败。
- **tar.zst**：先用 tar 保留目录结构，再用 zstd 压缩的归档文件。
- **GB / GiB**：文件容量单位。GB 通常按 10 进制计算；GiB 按 2 进制计算，
  1 GiB = 1,073,741,824 字节。
- **symlink / 符号链接**：只保存另一个路径的特殊文件，类似 Linux 中的可控快捷方式。
