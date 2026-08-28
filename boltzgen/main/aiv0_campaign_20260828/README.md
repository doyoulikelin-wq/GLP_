# AIV0 外置日志运行器

该目录提供正式 AIV0 数据资产检查的最小运行器。默认调用 validator 的
`--check` 模式；首次建立新登记册时可显式选择 `--mode write`。两种模式都不修改
validator 或源数据，`write` 只允许写入 Git 仓库外的派生登记册目录。

## 边界

- Git 中的 `run_aiv0_stage.py` 是权威源码。
- `--run-root` 必须是已经存在、且解析后位于 `--repo-root` 外部的 campaign 根。
- `--contract-root` 必须位于 Git 仓库的带日期 V2 资源目录；五个静态合同自动进入
  输入哈希，不依赖操作者逐个列举。
- `--output-root` 在 `check` 和 `write` 两种模式都必须显式提供，正式 CLI 只接受
  `workspace://boltzgen/data/ai_structure_asset_validation_registry_YYYYMMDD[_HHMMSS]/`
  形式，且
  永远拒绝历史 20260826 根。
- 日志固定写入
  `<run-root>/logs/stages/aiv0_asset_validation/attempt_NNN/`。
- attempt 目录一旦存在即拒绝复用；失败重试必须使用新的 attempt ID。
- validator 退出后先闭合输入和输出 SHA-256，最后原子发布 `receipt.json`。
- 指定的外置目录在执行前后分别写入
  `derived_outputs_before.SHA256SUMS` 和 `derived_outputs.SHA256SUMS`。
- 环境只记录固定白名单；token、密钥和其他任意环境变量不会写入日志。
- 子进程显式使用 Python `-B`，因此即使 `-I` 隔离模式忽略环境变量，也不会在
  源码目录生成 `__pycache__`。
- 该阶段的 `PASS` 只表示数据身份和登记合同通过，不表示模型有效或候选结合。
- 输入清单只写 `repo://` / `workspace://` 规范 URI；运行后再次哈希，发生执行中
  漂移时闭合 `RUNNER_EVIDENCE_ERROR` 失败收据。
- `check` 前后分别保存派生树哈希；任何只读检查造成的字节变化都会闭合失败收据。
- `runtime_fingerprint.json` 记录 Python 可执行文件哈希，以及 Gemmi 与 PyYAML 的
  版本、入口文件哈希和包树哈希。

## 调用

从 Git 仓库根运行，并由操作者显式提供本机路径。命令模板不在仓库中固定任何
用户名或绝对路径：

```bash
python3 boltzgen/main/aiv0_campaign_20260828/run_aiv0_stage.py \
  --repo-root "${REPO_ROOT:?}" \
  --run-root "${RUN_ROOT:?}" \
  --attempt-id attempt_001 \
  --validator-python "${VALIDATOR_PY:?}" \
  --validator "${AIV0_VALIDATOR:?}" \
  --project-root "${WORKSPACE_ROOT:?}" \
  --contract-root "${AIV0_CONTRACT_ROOT:?}" \
  --output-root "${AIV0_OUTPUT_ROOT:?}" \
  --migration-receipt "${MIGRATION_RECEIPT:?}" \
  --asset-root-manifest "${ASSET_ROOT_MANIFEST:?}" \
  --asset-root-summary "${ASSET_ROOT_SUMMARY:?}"
```

迁移 receipt、资产根 manifest 和摘要是 formal CLI 的三个必填证据；`--input` 可
继续重复追加其他文件。runner、validator、Python 可执行文件及五个 V2 静态合同会
自动进入输入哈希清单。

每个完成的 attempt 包含：

```text
command.json
derived_outputs_before.SHA256SUMS
derived_outputs.SHA256SUMS
environment_allowlist.json
runtime_fingerprint.json
runtime_fingerprint_after.json
started_at_utc.txt
ended_at_utc.txt
stdout.log
stderr.log
exit_code.txt
status.json
inputs.SHA256SUMS
inputs_after.SHA256SUMS
outputs.SHA256SUMS
receipt.json
```

validator 非零退出仍会形成闭合的 `FAIL` receipt，并把原退出码作为 runner 退出码。
attempt 冲突返回 73，配置错误返回 64。

新登记册的受控初始化使用新的 attempt ID 并增加 `--mode write`；之后必须再用
另一个 attempt ID 执行默认 `check`，只有最终 `check` 通过才可关闭 AIV0。

## 测试

测试只使用临时目录和假 validator：

```bash
python3 -B -m unittest discover \
  -s boltzgen/main/aiv0_campaign_20260828 \
  -p 'test_*.py'
```
