# SAbDab2 VHH 骨架筛选（2026-08-19）

公开入口：`筛选SAbDab2_VHH骨架_20260819.py`。默认不下载；显式 `--download` 才访问 SAbDab2 API。

## 数据逻辑

从 4,508 个 SD-H antibody instances 中限定 camelid-origin VHH，再应用结构完整性、主链几何、occupancy、框架二硫键、额外设计区二硫键、去重和框架聚类。最终冻结 10 PRIMARY + 2 RESERVE。

`12/12 BoltzGen check PASS` 只说明 CIF/YAML/编号合同可解析，不说明它们结合 GLP-1，也不说明可开发性。

## 外置内容

原始 TGZ、完整 mmCIF、SQLite 和 12 个完整结构包不进入 Git。筛选规则、登记表、漏斗和输入验证摘要位于 [`../../resources/data/SAbDab2_VHH骨架登记表_20260819/`](../../resources/data/SAbDab2_VHH骨架登记表_20260819/)。
