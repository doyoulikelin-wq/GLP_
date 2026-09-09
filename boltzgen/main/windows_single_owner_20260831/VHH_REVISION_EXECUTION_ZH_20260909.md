# VHH 六项修订执行记录（2026-09-09）

本次授权：实施六项审查建议，将修改推送 GitHub，并按修订后的阶段规则继续。
本记录区分已完成的软件修改、已完成的计算和未验证的科学假设。

## 关键解释更正

项目终点是活性 `GLP-1(7–36)NH₂` 相对截短 `GLP-1(9–36)NH₂` 的识别偏好。
人工定位的参考构象不是实验复合物；对它的 RMSD 只能衡量姿态自洽性，
不能直接推断脱离目标、不能结合或选择性。历史 T11/T12 RMSD 数值不变。

T12 将 target 和 framework 分成两个模板槽，不包含两者相对位置几何。
当前 folding checkpoint 不消费 `binding_type` 条件分支；输入含 His/Ala 标记
不等于复折叠被该位点约束。自由复核与条件化设计分开，不开启未训练的条件分支。

## 六项改进的交付边界

| 改进 | 本轮软件或规则交付 | 科学上仍需完成 |
|---|---|---|
| 评价回到端部机制 | 端部接触、界面接触图、重复稳定性和重叠提示；RMSD 保留为辅助 | 接触不是结合或选择性真值 |
| 明确模型输入的实际作用 | 区分模板可见性、位点元数据和权重实际消费分支 | 不能把强制保持姿态作为独立验证 |
| 加入已知对照 | 先做 CPU 几何自检，再做有界自由复折叠；两者分别记状态 | 已知结构可能存在训练集记忆；通过不保证新 GLP-1 候选可预测 |
| 活性／截短配对 | 同一候选身份绑定、匹配输入条件、来源分组及端部化学检查 | 新候选不得继承旧候选的多状态结果；未建模修饰须明示 |
| 候选多样性与风险 | 校准后的小型多框架／识别环／姿态试点；可开发性代理描述 | 不保证表达、稳定性或选择性；未生成不写成已完成 |
| 精简执行 | 每轮一个问题、明确预算和停止条件；输入只读、新目录、增量验证 | 不重复全树审计；实际使用的权重仍需运行前后完整性确认 |

## 运行状态

参考姿态解释已修订；旧 owner 测试集 `96 passed`，另有一条既有 `pynvml` 弃用警告。
新增评价器 16 项测试、阶段规则 24 项测试、原生对照 18 项测试分别通过。
整合后的 owner 测试集共 `154 passed`，耗时 27.93 秒，只有上述既有弃用警告。
本轮变更文件的仓库政策检查通过；全工作树检查仍报告历史测试缺少模块说明和
既存缓存等问题，未将这些无关历史文件清理或改写，不能声称全仓政策检查通过。

现有结果重评完成：同一组 6 个候选，T11/T12 各 30 次预测。两臂分析分别用时
0.326 秒和 0.314 秒（含本臂小型输入的一次读取和哈希，不含开发测试耗时）。
任意 target–CDR 接触均为 30/30；His/Ala 同时接触为 0/30 与 2/30。
两臂均无一个候选在自身全部 5 次预测中同时接触两个端部残基。
低于 1.5 Å 的 target–CDR 原子距离提示各为 1/30；这是 CDR 范围的几何提示，
不是全 VHH 的原子重叠总数，也不是按元素半径判断的 clash。

已知对照 `6JB8` 的 CPU 准备完成：使用实验已解析的 VHH 125 残基和目标 129 残基，
未解析的 VHH 两端合计 11 残基明确记录；原生 45 个重原子残基接触对只用于评分。
输入 VHH 平移 100 Å，模板只显示目标、不显示 VHH；原生／平移输入的模板特征相等。
CPU 自检不是自由复折叠成功。GPU 对照将使用 2 次预测、900 秒上限，不自动重试。
两次预测均需目标对齐的 VHH CA RMSD ≤5 Å 且原生接触召回率 ≥0.3，
这是运行前固定的粗粒度工程校准阈值，不是生物学成功标准。

当前阶段门：现有结果重评 `COMPLETE`，原生自由复折叠 `READY`；
多样性设计与活性／截短配对尚未执行，不得写成已改进或已验证的科学结果。
见 [本轮脱敏结果](reports/vhh_revision_public_20260909/README.md)。

## 可复现入口

在已接受的 Python 环境中运行，各输出参数必须指向不存在的新目录：

```bash
python -B scripts/evaluate_vhh_epitope.py \
  --design-root "$DESIGN_ROOT" --output-dir "$ANALYSIS_OUTPUT" \
  --method t12_split_template --expected-candidates 6 --expected-samples 5
python -B scripts/vhh_stage_gate.py \
  --contract configs/vhh_revision_20260909.json \
  --bindings "$INPUT_BINDINGS" --receipts "$CPU_STAGE_RECEIPT"
python -B scripts/run_vhh_native_control.py \
  --workspace "$WORKSPACE_ROOT" --repo-root "$REPO_ROOT" \
  --prepared "$NATIVE_PREPARED" --output "$NATIVE_OUTPUT" \
  --runtime-root "$RUNTIME_ROOT" --contract configs/vhh_revision_20260909.json \
  --bindings "$INPUT_BINDINGS" --prerequisite-receipt "$CPU_STAGE_RECEIPT" \
  --hard-timeout-seconds 900
```

以上相对路径以本 owner 目录为当前目录。详细结果和来源绑定只在本地保留。
GPU 命令返回 0 只表示计算完成，继续之前仍须用实际 GPU 收据重新调用阶段门。

## 保留与公开范围

- 既有 CIF/NPZ、运行日志、收据、历史公开 JSON 和失败状态均不改写。
- GitHub 仅接收代码、规则、聚合结果与脱敏来源摘要。
- 原始结构、候选序列、逐样本结果、权重和本机绝对路径继续保留在本地。
- 本轮不训练、不修改权重，不自动转入 BindCraft，不启动湿实验。

相关入口：[修订方案](VHH_REVISED_PROTOCOL_ZH_20260909.md)、
[机器可读规则](configs/vhh_revision_20260909.json)、[当前目录首页](README.md)。
