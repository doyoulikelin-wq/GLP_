# VHH 生成端部条件：实际机制诊断

结论：**pilot02 的生成模型确实接收并使用了 His7/Ala8 的条件标记，不能把观察到的 C 端接触归因于“生成模型没有读取绑定条件”。但这是软条件，不是保证接触端部的硬约束。** 为什么这四个设计最终选择了 C 端，本轮没有通过对照生成证明因果。

本轮只在 CPU 上读取实际配置、重新构造输入特征，并查看已有检查点的超参数及一个很小的条件嵌入矩阵。未实例化完整模型、未启动 GPU、未生成候选、未训练或修改权重。检查点使用内存映射，没有反复扫描大权重摘要。

## 1. “生成”和“复折叠”必须分开说

| 环节 | 实际检查点 | 是否有绑定条件分支 | 本项目中的含义 |
|---|---|---|---|
| 生成候选 | `boltzgen1_adherence.ckpt` | `add_binding_specification=true`；检查点含非零 `[3,384]` 条件嵌入 | 能接收期望接触的残基标签，但不保证遵守 |
| 后续自由复折叠 | `boltz2_conf_final.ckpt` | 先前机制复核确认没有该分支参数 | 属于独立自由检验，不应被描述为施加了 His/Ala 约束 |

实际生成配置没有覆盖关闭 `embedder_args.add_binding_specification`，也没有使用 EMA 替换检查点权重的选项。绑定类别分别为未指定、期望接触、不期望接触。三个嵌入行的范数约为 `0.291、1.601、1.622`；期望接触行与未指定行的差范数约 `1.645`。这表明检查点确有不同于初始化全零的条件参数，不是临时打开一个没有对应参数的开关。

源代码中，标签最终通过加法并入 token 表征：

```python
s = s + self.binding_specification_conditioning_init(feats["binding_type"])
```

这不是“所有生成结果都必须在 4.5 Å 内接触指定残基”的约束求解器。预测坐标仍由模型采样产生。

## 2. 两个已运行输入与一个新增框架的 CPU 检查

| 检查项 | 7XL0 框架输入 | 6APO 框架输入 |
|---|---:|---:|
| 总 token 数 | 151 | 146 |
| GLP-1 token 数 | 30 | 30 |
| 被设计的 CDR token 数 | 30 | 26 |
| `BINDING` 位置 | 0、1＝His7、Ala8 | 0、1＝His7、Ala8 |
| 目标其余未指定位置数 | 28 | 28 |
| `NOT_BINDING` 标记数 | 0 | 0 |
| target×VHH 可见距离条件对数 | 0 | 0 |
| CPU masker 后绑定标记是否保留 | 是 | 是 |

随后在新的私有诊断目录加入 `09_pdb_00008im0-B` 原样 spec，沿用实际 adherence 生成配置重建 CPU 特征：总 token 151、目标 30、CDR 30、框架组 91；His/Ala 仍精确位于 0、1，masker 保留；其余目标 28 位未指定，没有 `NOT_BINDING` 标记；跨组可见距离对为 0，平移测试差为 0。这个新增输入明确标记为 `NEW_SPEC_CPU_PREPARATION_NOT_HISTORICAL_RUN`，不冒充已经执行了 8IM0 生成。

因此，“指定开头两位需要接触”和“禁止接触其余部分”不是同一件事。当前输入做了前者，没有把其余 28 位标为 `NOT_BINDING`。本诊断不自动新增这些标记：它们会改变设计问题，而且对其余全部位置施加负条件也可能阻碍合理支撑界面。

## 3. 输入文件中的相对摆放意味着什么

当前结构组：GLP-1 为组 1，VHH 框架为组 2，被设计 CDR 为组 0。几何特征只保留同一正结构组内部的距离条件；没有把目标与 VHH 的跨组相对距离提供给这条通道。

CPU 检查把 VHH 的 token 中心整体平移 `(100,37,-29) Å` 后，两输入的**掩蔽后 token 距离通道**变化均为 `0 Å`。这仅说明这条几何条件通道不锁定两者的相对位置，不能扩大为“所有原始特征完全相同”或“整个网络对输入布局绝对不敏感”。

实际扩散采样从缩放的高斯噪声开始，不是从输入文件里两个分子的摆放开始逐步优化。因此，单纯把源 CIF 中的 VHH 移到 GLP-1 N 端，不能预设其会约束最终结合姿态。若以后研究共同结构组，应将其标明为新的受条件限制的设计对照，不能把被输入的相对位置再当独立验证结果。

## 4. 本诊断支持什么、不支持什么

- 支持：排除“当前生成路径完全丢失 His/Ala 标记”这一简单解释；标记位置正确，masker 保留，检查点已有条件分支。
- 支持：当前两处正条件没有配套的非端部负条件；跨组相对位置不是当前距离条件的一部分。
- 不支持：确定是条件强度、骨架、随机采样、目标端部几何或模型偏好中的哪一个造成了 C 端接触。
- 不支持：通过增大、篡改嵌入或开启未训练分支来修复；本项目仍只使用现成权重。
- 下一步可检验的机制问题：在来源、框架与预算匹配下，比较原端部标签、取消标签和有限非端部负标签的设计结果；必须事前明确它们是生成条件对照，且保留独立自由复折叠。本轮未执行这些生成试验。

## 5. 复现与源码证据

脚本：[check_vhh_generation_conditioning.py](scripts/check_vhh_generation_conditioning.py)；测试：[test_check_vhh_generation_conditioning.py](tests/test_check_vhh_generation_conditioning.py)。

```bash
python -B scripts/check_vhh_generation_conditioning.py \
  --pilot-index /absolute/private/pilot02/INDEX.json \
  --additional-spec /absolute/private/specs/09_pdb_00008im0-B/design.yaml \
  --output /absolute/private/new_conditioning_check
```

输出 `PRIVATE_CONDITIONING_DIAGNOSTIC.json` 和各输入的小型原始特征 NPZ 仅留私有目录；`PUBLIC_CONDITIONING_DIAGNOSTIC.json` 是显式挑选字段的公共摘要，不含序列、用户路径、原始特征或权重张量。新 CPU 特征是对实际配置路径的重建，不冒充已捕获的历史 GPU batch。

本机实际安装源码提供以下证据，公共 JSON 记录对应文件 SHA256：

| 源码位置（相对 `boltzgen` 包） | 核对的机制 |
|---|---|
| `data/parse/schema.py`，绑定标签解析段 | `binding: 1..2` 解析为指定残基的 `BINDING`；未指定位置保持未指定 |
| `task/predict/data_from_yaml.py`，`get_sample` | 从残基设计信息传到 token 的 `binding_type` |
| `data/feature/featurizer.py`，token 特征与距离掩码段 | 返回绑定标签；只保留同一正结构组距离 |
| `model/modules/masker.py`，token 特征复制段 | 绑定标签在模型前掩蔽中保留 |
| `model/modules/trunk.py`，输入嵌入前向段 | 条件嵌入加到 token 表征 |
| `task/predict/predict.py`，检查点加载段 | 严格加载实际检查点，并应用当前推理覆盖项 |
| `model/modules/diffusion.py`，采样初始化段 | 从高斯噪声生成起始坐标 |

附带发现：生成配置的 `override.masker_args` 指定 `mask=true, mask_backbone=false`，其有效 `mask_disto=false` 与检查点记录中的 `true` 不同。源码将这一字段用于被设计 token 的距离图监督掩码；本轮没有证据把它解释为端部条件丢失或 C 端接触的原因，也未修改它。
