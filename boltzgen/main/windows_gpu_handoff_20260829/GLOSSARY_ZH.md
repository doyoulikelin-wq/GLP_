# 术语表

- **AIV**：Artificial Intelligence Verification，AI 验证。这里不是模型训练，而是
  对输入、推理、评估和经验库链条做分阶段验证。
- **AIV0**：AI 验证第 0 阶段，只检查数据身份、来源和合同。
- **AIV1**：AI 验证第 1 阶段，验证 10 个固定候选在 16 个公开开发态上的统一
  推理和评价链条。
- **AIV2**：AI 验证第 2 阶段，用 240 个 baseline（基线）候选检查跨骨架覆盖；
  如需调整，最多比较两个受限配置变体。本交接包不执行 AIV2。
- **AIV3**：AI 验证第 3 阶段，用选定配置运行 2,400 条诊断批；通过后才冻结配置
  并允许进入 12,000 条生产采样。本交接包不执行 AIV3。
- **AIV4**：AI 验证第 4 阶段，在面板预先冻结后一次性打开锁箱，只作整个 campaign
  是否继续的群体门，不得据此重排单个候选。本交接包不含锁箱，也不执行 AIV4。
- **contract / 合同**：事先冻结的输入、版本、参数、输出和通过条件；不是法律合同。
- **gate / 门**：只有所有硬条件满足才能通过的阶段检查。
- **preflight / 预检**：正式运行前检查清单、路径、数量和合同是否完整；不运行正式推理。
- **engineering / 工程证据**：只证明机器、兼容性或一段流程可以运行；不能代替
  已冻结合同要求的正式门。
- **formal / 正式证据**：完全匹配已冻结版本、输入、参数、分母、哈希和收据后形成的
  结果。正式技术门通过仍不等于实验成功。
- **PASS / NOT_RUN / BLOCKED**：分别表示已满足本门全部条件、尚未运行、因前置条件
  不足而停止。`FAIL` 表示已运行但不满足条件。
- **闭合收据**：收据引用的输入、代码、环境、输出和上游收据都存在，数量正确，且
  哈希能逐项复算一致。
- **G1**：GPU 环境门。检查操作系统、驱动、Python、包、GPU 算子和哈希是否
  可复现。
- **G2**：单 GPU 端到端验收门。检查 BoltzGen 完整流程和生产 batch 的显存安全。
- **GPU**：Graphics Processing Unit，图形处理器。本项目用它运行神经网络推理。
- **CPU / RAM**：CPU 是通用处理器；RAM 是主机内存。RAM 与 GPU 自己的显存不是
  同一块内存。
- **VRAM / 显存**：GPU 自己使用的高速内存。
- **CUDA**：NVIDIA 的 GPU 计算平台。
- **NVIDIA driver / 显示驱动**：Windows 与 GPU 硬件通信的软件；在本方案中只装在
  Windows。它与 Python 环境内随 PyTorch 安装的 CUDA runtime 不是同一个东西。
- **CUDA runtime / CUDA 运行库**：程序执行 GPU 算子所需的用户态库；本方案由
  PyTorch 的 `cu128` 软件包提供，不在 WSL 内另装 Linux 显示驱动。
- **Blackwell**：RTX 50 系列使用的 NVIDIA GPU 架构名称。
- **PyTorch**：运行 BoltzGen 神经网络的 Python 框架。
- **cu126 / cu128**：PyTorch 分别针对 CUDA 12.6 / CUDA 12.8 构建的版本。
- **BF16**：bfloat16，一种 16 位数值格式；BoltzGen 用它降低显存和加速计算。
- **cuEquivariance**：BoltzGen 使用的一组 GPU 几何运算库；必须在 5070 Ti 上做
  原生 kernel 测试。
- **kernel**：GPU 上直接执行的底层计算函数，不是 Windows 或 Linux 内核。
- **native kernel / 原生 GPU 算子**：确实在 NVIDIA GPU 上执行的底层算子；前向和
  反向测试都通过，才能证明它不是静默退回 CPU 的假成功。
- **WSL2**：Windows Subsystem for Linux 2，在 Windows 中运行真实 Linux 内核的
  环境。
- **Ubuntu**：本项目在 WSL2 中使用的 Linux 发行版。
- **x86_64 / ARM**：两种不同的处理器指令架构。Windows 5070 Ti 笔记本通常是
  x86_64；Apple 芯片 Mac 是 ARM，二者的环境和二进制包不能混用。
- **SSH**：Secure Shell，加密远程命令连接。
- **systemd**：Linux 的服务和后台任务管理器。
- **PID 1**：Linux 启动后的第一个进程；本方案要求它是 `systemd`，以便可靠管理
  单 GPU 任务。
- **Slurm**：服务器集群的作业调度器；这台笔记本没有 Slurm。
- **flock**：Linux 文件锁，用来保证同时只有一个 GPU 任务。
- **checkpoint / ckpt**：已经训练好的模型权重文件。本项目冻结使用，不重新训练。
- **frozen weights / 冻结权重**：只读取 checkpoint 做推理，不更新其中的参数。
- **runtime / 运行时**：真正执行程序所需的软件、依赖、权重和分子字典的组合。
- **inference / 推理**：把输入交给冻结模型产生结构和分数。
- **target / 靶标**：候选希望识别的分子或结构状态；“正靶”是主要研究对象，
  “反靶/挑战靶”用于检查潜在脱靶风险。
- **scaffold / 骨架**：VHH 中保持固定或大部分固定的结构框架，提供设计起点。
- **baseline / 基线**：始终保留、不随本轮调整改变的参照配置或参照结果。
- **variant / 变体**：相对 baseline 只改少量预先登记变量的比较配置。
- **config / 配置**：一次运行使用的参数集合；resolved config 是程序解析默认值和
  覆盖项后真正执行的完整配置。
- **candidate / 候选**：BoltzGen 生成的一条待评价 VHH 序列及对应结构。
- **batch**：一次并行送入 GPU 的样本数量。batch 越大通常越占显存。
- **smoke test**：最小冒烟测试，只证明最短链条可运行，不证明科学效果。
- **CDR**：Complementarity-Determining Region，VHH 抗体中主要负责识别目标的
  可变环区域。
- **VHH**：骆驼科单域抗体的可变结构域，也常称 nanobody（纳米抗体）。
- **binder / nonbinder**：经真实实验确认结合 / 不结合的分子标签。当前计算数据没有
  这种真值，不能用模型分数自行生成。
- **affinity / 亲和力**：两个分子结合强弱的实验性质，常用解离常数 `K_D` 表示；
  结构模型分数不能直接换算成 `K_D`。
- **selectivity / 选择性**：相对正靶更偏好、较少结合反靶的实验性质；必须由配对实验
  支持，计算反筛只能给出风险代理。
- **GLP-1**：Glucagon-Like Peptide-1，胰高血糖素样肽-1；本项目的主要正靶。
- **GLP-2**：Glucagon-Like Peptide-2，胰高血糖素样肽-2；这里只作为公开开发期
  调参反靶，不是最终锁箱。
- **GIP**：Glucose-Dependent Insulinotropic Polypeptide，葡萄糖依赖性促胰岛素多肽；
  本项目把相关结构留在最终 lockbox 中；本包只有用于证明排除的身份元数据，没有
  结构文件字节或候选结果。
- **mmCIF / CIF**：Macromolecular Crystallographic Information File，保存原子、
  残基、链和结构坐标的文本格式。
- **PDB**：Protein Data Bank，蛋白质结构数据库；`7XL0`、`6XYM` 等是结构编号。
- **SAbDab**：Structural Antibody Database，抗体结构数据库；本项目只带已准入的
  12 个骨架，不带其原始大快照。
- **NPZ**：NumPy 的压缩数组文件，BoltzGen 用它保存坐标样本和数值指标。
- **mols.zip**：BoltzGen 使用的分子/化学字典压缩包；它是运行资产，不是候选结果。
- **JSON**：机器可读取的结构化文本格式。
- **CSV / TSV**：分别用逗号 / 制表符分隔的表格文本。
- **SHA-256**：文件内容指纹；文件只要改变一个字节，指纹通常就会改变。
- **manifest**：清单，列出文件、大小、身份或哈希。
- **provenance / 来源谱系**：一个文件或结果从哪里来、经过什么代码和参数生成、由哪些
  上游输入派生的可追溯关系。
- **allowlist / 允许清单**：合同逐项写明的唯一可读输入；程序不得扫描目录自动补入
  未登记文件。
- **receipt**：机器运行收据，绑定输入、代码、环境、输出和最终状态。
- **builder / 构建器**：根据冻结输入确定性生成清单、任务或交接包的程序；不运行模型。
- **runner / 运行器**：按固定顺序执行阶段、保留日志并最后生成收据的程序。
- **validator / 验证器**：只读核对字段、数量、哈希和规则的程序；不应顺手修改被验数据。
- **schema / 格式合同**：规定文件或数据库有哪些字段、类型、约束和关系的机器规则。
- **cell / 执行单元**：一组固定靶标、checkpoint、候选数量和 batch 参数构成的
  一次独立运行。
- **logical task / 逻辑任务**：一个固定候选在一个固定开发态上的一次评价任务。
- **sample row / 样本结果行**：逻辑任务中的一个复折叠样本产生的一行结果；每个
  正式 AIV1 逻辑任务要求 5 行。
- **refold / 复折叠**：根据候选序列再次预测三维结构，用来检查设计是否稳定和可信。
- **design / 结构设计**：从条件输入出发采样候选复合物几何的阶段。
- **inverse folding / 逆折叠**：给定候选三维骨架，反推或采样适配该骨架的氨基酸序列。
- **folding / 复折叠预测**：把完整候选序列和靶标重新预测成三维复合物；本项目用它
  评价候选，而不是证明真实结合。
- **analysis / 分析**：从输出结构计算距离、偏差、置信度和不确定性等指标。
- **filtering / 过滤**：按事先冻结的规则保留或标记候选；过滤通过不等于实验命中。
- **adherence checkpoint**：更强调遵守输入几何条件的官方预训练设计权重分支。
- **diverse checkpoint**：更强调候选结构多样性的官方预训练设计权重分支。两者名称
  描述训练目标倾向，不保证某一分支科学效果更好。
- **sampling step / 采样步数**：扩散生成或预测时逐步更新的次数；增加步数通常更耗时。
- **recycling / 循环次数**：把一次模型预测再送回模型继续精化的次数。
- **diffusion sample / 扩散样本**：同一输入独立采样得到的一份结构预测；正式 AIV1
  每个逻辑任务要求 5 份，不能只保留成功样本。
- **attempt_NNN**：第 N 次不可覆盖尝试，例如 `attempt_001`。失败重试必须使用新号。
- **anchor**：通过正式 G2 后冻结的锚定候选，供后续 AIV 阶段重复比较。
- **lockbox**：一次性锁箱。只有最终规则冻结后才能打开的保留结构与候选级结果。
  本包保留 AIV0 身份元数据用于隔离审计，但不含结构文件字节或候选×锁箱结果。
- **wheel / wheelhouse**：wheel 是 Python 安装包；wheelhouse 是经哈希验证的离线
  安装包目录。
- **requirements lock**：带精确版本和哈希的 Python 依赖清单。
- **GiB / MiB**：二进制容量单位；1 GiB = 1024 MiB，1 MiB = 1,048,576 字节。
  它们与十进制 GB/MB 略有差异。
- **scratch / 作业空间**：保存运行中间文件的临时磁盘空间，不是 RAM 或显存。本方案
  工程阶段至少 80 GiB，申请正式 G1 前至少 250 GiB。
- **tar / zstd**：tar 把多个文件按目录结构归档；zstd 再对归档进行压缩。本包用
  `.tar.zst`，不是 Windows ZIP。
- **symlink / 符号链接**：一个只保存目标路径的特殊文件，类似受控快捷方式；本包只
  创建五个明确允许的兼容链接，拒绝其他链接注入。
- **Git bundle**：把 Git 提交和分支历史装进一个可离线克隆的文件，不包含密钥。
- **Git branch / 分支**：一条独立代码工作线；Windows 工作必须新建自己的分支。
- **Git commit / 提交**：一次带唯一哈希的代码快照。
- **Git origin**：当前本地仓库关联的远程仓库地址。
- **sanitized single-root commit / 脱敏单根提交**：从允许文件重新建立的一个全新首
  提交，不继承公共仓库旧历史，因此 Windows 不能回看被排除路径。
- **patch / 代码补丁**：只记录脱敏基线与最终文件树之间差异的文本文件。本包会把
  Windows 的中间提交压平，不导出作者、正文或中途又删除的内容；Mac Codex 复核后
  才能应用和推送。
- **GitHub**：保存 Git 代码历史的在线服务；本项目公开仓库不存大模型或私有数据。
- **GitHub Token**：GitHub 登录凭据；不能从 Mac 复制到 Windows，也不能打包。
- **OOM**：Out Of Memory，内存或显存不足。
- **NaN**：Not a Number，异常的非数值结果。
- **stdout / stderr**：程序的标准输出 / 标准错误日志；两者都必须保留，不能只截取
  看起来成功的几行。
- **exit code / 退出码**：程序结束时返回的整数；通常 0 表示命令成功，非 0 表示失败
  或阻塞，但最终仍须结合收据和清单判断。
- **append-only / 只追加**：只允许增加新事件，不允许更新或删除历史行；纠错要增加
  一个引用旧事件的新事件。
- **JSONL**：每一行都是一个完整 JSON 对象的日志格式，适合只追加事件库。
- **SQLite**：把结构化表保存在单个文件中的轻量数据库；正式 AIV1 经验库会使用，
  本交接包只带工程事件暂存 JSONL 和已有的静态 SQL 骨架。
- **URI**：Uniform Resource Identifier，统一资源标识符。本项目用 `repo://`、
  `workspace://`、`wsl://` 或 `windows://` 表达可迁移身份，而不把某台机器的绝对路径
  当成数据身份。
- **MPS**：Metal Performance Shaders，Apple GPU 后端；不能作为 Windows NVIDIA
  正式结果。
