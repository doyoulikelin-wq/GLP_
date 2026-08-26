#!/usr/bin/env python3
"""Build the canonical Data Analytics report artifact for the MVP package."""

from __future__ import annotations

import csv
import html
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
META = ROOT / "metadata"
RAW = ROOT / "raw_sources"
CURATED = ROOT / "curated_project_inputs"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def code(value) -> str:
    return f"<code>{esc(value)}</code>"


def pretty_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.3f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def pre(text: str) -> str:
    return f'<pre tabindex="0"><code>{esc(text)}</code></pre>'


DETAIL_STYLE = """
<style>
  :root{color-scheme:light dark}
  .asset-guide{font:17px/1.65 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#19324a}
  .asset-guide *{box-sizing:border-box}
  .asset-guide .intro{margin:0 0 14px;color:#4b6378}
  .asset-guide details{border:1px solid #c8d8e3;border-radius:14px;margin:10px 0;background:#fff;overflow:clip}
  .asset-guide summary{list-style:none;cursor:pointer;padding:15px 17px;display:flex;align-items:center;gap:10px;justify-content:space-between;font-weight:760;color:#123754;background:#f5f9fb}
  .asset-guide summary::-webkit-details-marker{display:none}
  .asset-guide summary:focus-visible{outline:3px solid #0b7f78;outline-offset:-3px}
  .asset-guide summary::after{content:"＋";font-size:22px;color:#0b6e68;flex:0 0 auto}
  .asset-guide details[open] summary::after{content:"－"}
  .asset-guide .summary-text{min-width:0}
  .asset-guide .badge{display:inline-block;margin-left:7px;padding:2px 8px;border-radius:99px;font-size:12px;line-height:1.6;background:#dff3ef;color:#075f59;vertical-align:2px}
  .asset-guide .badge.warn{background:#fff0d5;color:#87510a}
  .asset-guide .badge.stop{background:#ffe5e2;color:#9b2f27}
  .asset-guide .body{padding:15px 17px 18px}
  .asset-guide .lead{margin:0 0 12px;font-weight:650;color:#213f59}
  .asset-guide .facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:8px;margin:10px 0 15px}
  .asset-guide .fact{padding:9px 11px;border-radius:10px;background:#f3f7f9;overflow-wrap:anywhere}
  .asset-guide .fact b{display:block;font-size:12px;letter-spacing:.04em;color:#60798d;margin-bottom:2px}
  .asset-guide h4{font-size:17px;margin:17px 0 7px;color:#153b59}
  .asset-guide p{margin:7px 0}
  .asset-guide ul{margin:7px 0;padding-left:23px}
  .asset-guide li{margin:4px 0}
  .asset-guide pre{font:14px/1.52 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre;overflow:auto;max-height:390px;padding:13px;border-radius:10px;background:#10283b;color:#e9f3f6;border:1px solid #27475b}
  .asset-guide code{overflow-wrap:anywhere}
  .asset-guide .note{border-left:4px solid #12827a;background:#edf8f6;padding:10px 12px;border-radius:0 9px 9px 0}
  .asset-guide .warning{border-left-color:#c37816;background:#fff6e7}
  .asset-guide .blocker{border-left-color:#b33b32;background:#fff0ee}
  .asset-guide .link-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
  .asset-guide a{color:#075f8c;font-weight:650}
  .asset-guide .mini-table{overflow:auto;margin:8px 0}
  .asset-guide table{border-collapse:collapse;width:100%;min-width:650px;font-size:14px}
  .asset-guide th,.asset-guide td{padding:8px 9px;border-bottom:1px solid #d9e4ea;text-align:left;vertical-align:top}
  .asset-guide th{background:#eef5f7;color:#29485f;position:sticky;top:0}
  @media(prefers-color-scheme:dark){
    .asset-guide{color:#dbe8ef}.asset-guide details{background:#172a38;border-color:#385364}
    .asset-guide summary{background:#1c3444;color:#eef7fa}.asset-guide .body{background:#172a38}
    .asset-guide .lead,.asset-guide h4{color:#e6f2f7}.asset-guide .intro{color:#afc5d2}
    .asset-guide .fact{background:#203948}.asset-guide .fact b{color:#a9c2d0}
    .asset-guide .note{background:#183d3b}.asset-guide .warning{background:#4a3519}.asset-guide .blocker{background:#4d2928}
    .asset-guide th{background:#203b4b;color:#d9eaf2}.asset-guide td,.asset-guide th{border-color:#3b5666}
    .asset-guide a{color:#7fcfff}
  }
  @media(max-width:560px){.asset-guide{font-size:16px}.asset-guide summary,.asset-guide .body{padding:13px}.asset-guide .facts{grid-template-columns:1fr}.asset-guide pre{font-size:13px}}
</style>
"""


def fact_grid(facts: list[tuple[str, str]]) -> str:
    return '<div class="facts">' + "".join(
        f'<div class="fact"><b>{esc(label)}</b>{esc(value)}</div>' for label, value in facts
    ) + "</div>"


def links_html(links: list[tuple[str, str]]) -> str:
    if not links:
        return ""
    return '<div class="link-row">' + "".join(
        f'<a href="{esc(url)}" target="_blank" rel="noreferrer">{esc(label)} ↗</a>'
        for label, url in links
    ) + "</div>"


def detail(
    title: str,
    badges: list[tuple[str, str]],
    lead: str,
    facts: list[tuple[str, str]],
    sample: str,
    reading: str,
    role: str,
    cleaning: str,
    caveat: str,
    links: list[tuple[str, str]],
) -> str:
    badges_text = "".join(f'<span class="badge {esc(kind)}">{esc(text)}</span>' for text, kind in badges)
    caveat_class = "blocker" if any(word in caveat for word in ["阻断", "不能", "不得", "未验证", "缺失"]) else "warning"
    return f"""
<details>
  <summary><span class="summary-text">{esc(title)}{badges_text}</span></summary>
  <div class="body">
    <p class="lead">{esc(lead)}</p>
    {fact_grid(facts)}
    <h4>一个真实样例</h4>
    {pre(sample)}
    <h4>如何读、行列或向量代表什么</h4>
    <div>{reading}</div>
    <h4>在流程中的输入／输出位置</h4>
    <p>{role}</p>
    <h4>清理决定</h4>
    <p>{cleaning}</p>
    <div class="note {caveat_class}"><b>限制与检查：</b> {caveat}</div>
    {links_html(links)}
  </div>
</details>"""


def html_block(title: str, intro: str, details: list[str]) -> str:
    return DETAIL_STYLE + f'<section class="asset-guide"><h2>{esc(title)}</h2><p class="intro">{esc(intro)}</p>' + "".join(details) + "</section>"


def rows_sample(columns: list[str], rows: list[list[str]], limit: int = 6) -> str:
    lines = ["\t".join(columns)]
    lines.extend("\t".join(str(value) for value in row) for row in rows[:limit])
    return "\n".join(lines)


def main() -> None:
    scope = load_json(META / "mvp_scope.json")
    quality = load_json(META / "quality_checks.json")
    asset_profile = load_json(META / "asset_profile.json")
    checkpoint_profile = load_json(META / "checkpoint_profile.json")
    curation_manifest = load_json(ROOT / "curation_manifest.json")
    machine_summary = load_json(META / "machine_readable_summary.json")
    variants = load_json(CURATED / "sequence_chemistry" / "GLP1_project_variants.json")
    inventory = read_tsv(META / "file_inventory.tsv")
    allowlist = read_tsv(CURATED / "project_input_allowlist.tsv")
    used_components = read_tsv(CURATED / "used_components.tsv")
    profiles = asset_profile["profiles"]

    checkpoint_sources = {
        "boltzgen1_diverse.ckpt": "https://huggingface.co/boltzgen/boltzgen-1/blob/c1be29e1f82ffcc72264f64b993c43fb4e0d17f0/boltzgen1_diverse.ckpt",
        "boltzgen1_adherence.ckpt": "https://huggingface.co/boltzgen/boltzgen-1/blob/c1be29e1f82ffcc72264f64b993c43fb4e0d17f0/boltzgen1_adherence.ckpt",
        "boltzgen1_ifold.ckpt": "https://huggingface.co/boltzgen/boltzgen-1/blob/c1be29e1f82ffcc72264f64b993c43fb4e0d17f0/boltzgen1_ifold.ckpt",
        "boltz2_conf_final.ckpt": "https://huggingface.co/boltzgen/boltzgen-1/blob/c1be29e1f82ffcc72264f64b993c43fb4e0d17f0/boltz2_conf_final.ckpt",
    }
    checkpoint_names = {
        "boltzgen1_diverse.ckpt": "设计模型：diverse",
        "boltzgen1_adherence.ckpt": "设计模型：adherence",
        "boltzgen1_ifold.ckpt": "逆折叠模型：ifold",
        "boltz2_conf_final.ckpt": "复折叠／置信度模型：Boltz-2 conf",
    }

    runtime_details = []
    for profile in checkpoint_profile["profiles"]:
        name = Path(profile["path"]).name
        tensor_lines = ["parameter_key\tdtype\tshape\telements"]
        for item in profile["sample_tensors"][:12]:
            tensor_lines.append(f"{item['key']}\t{item['dtype']}\t{item['shape']}\t{item['elements']}")
        top_fields = json.dumps(profile["top_level_fields"], ensure_ascii=False, indent=2)
        sample = "[checkpoint 顶层字段]\n" + top_fields + "\n\n[state_dict 参数样例]\n" + "\n".join(tensor_lines)
        runtime_details.append(detail(
            checkpoint_names[name],
            [("必需运行资产", ""), ("已验SHA", "")],
            "这是预训练模型检查点：它是原训练过程的输出，也是本次MVP推理的输入；不是一张按蛋白样本排列的训练数据表。",
            [
                ("本地路径", profile["path"]),
                ("文件大小", f"{profile['bytes']:,} B｜{pretty_bytes(profile['bytes'])}"),
                ("参数张量", f"{profile['tensor_count']:,} 个"),
                ("张量元素", f"{profile['total_tensor_elements']:,} 个"),
                ("状态字典位置", profile["state_location"]),
                ("安全读取", profile["load_policy"]),
            ],
            sample,
            """<ul><li><b>一条记录</b>定义为一个命名参数张量，而不是一个生物样本。</li><li>向量 <code>[d]</code> 常表示每个隐藏特征一个偏置或缩放值。</li><li>线性层矩阵 <code>[O,I]</code> 通常是：行对应输出特征，列对应输入特征；但嵌入矩阵 <code>[V,H]</code> 则是行对应类别、列对应隐藏维度。</li><li>更高阶张量的轴必须结合参数名与源码解释，不能把任意二维张量说成“残基×氨基酸”。</li><li>权重数值本身没有可直接阅读的生物学标签；样例展示的是键、dtype与shape。</li></ul>""",
            "BoltzGen在design、inverse folding或Boltz-2 refold阶段加载；该文件不会被项目重新训练。",
            "保留官方固定revision下的完整文件；不裁剪、不改名、不反序列化不可信来源。",
            "只有在文件大小与SHA-256均匹配官方锁定值后才允许加载。PyTorch 2.9的weights_only读取器不支持这些protocol-5文件；本报告没有降级到不受限torch.load，而是只读ZIP中的data.pkl，并用禁止模块导入/项目类执行的受限元数据解析器生成键与shape。",
            [("固定版本模型文件", checkpoint_sources[name]), ("BoltzGen v0.3.2运行资产映射", "https://github.com/HannesStark/boltzgen/blob/v0.3.2/src/boltzgen/cli/boltzgen.py#L120-L139")],
        ))

    mols = profiles["mols_zip"]
    mol_sample = json.dumps({
        "archive_members_first_12": mols["sample_member_names"],
        "selected_component_summaries": mols["project_relevant_samples"],
    }, ensure_ascii=False, indent=2)
    runtime_details.append(detail(
        "mols.zip：CCD／RDKit化学组分字典",
        [("必需运行字典", ""), ("完整保留", "warn")],
        "这是BoltzGen按CCD三字母代码索引的化学字典。45,227个成员不是45,227条本项目训练样本。",
        [
            ("本地路径", mols["path"]),
            ("ZIP大小", f"{mols['bytes']:,} B｜{pretty_bytes(mols['bytes'])}"),
            ("成员数", f"{mols['member_count']:,} 个.pkl"),
            ("成员解压总量", f"{mols['uncompressed_bytes']:,} B｜{pretty_bytes(mols['uncompressed_bytes'])}"),
            ("目录结构", "根目录唯一.pkl；无重复、无路径穿越"),
            ("项目使用记录", f"used_components.tsv列出当前输入出现的{len(used_components)}种标准组分"),
        ],
        mol_sample,
        """<ul><li>每个 <code>ALA.pkl</code>、<code>HIS.pkl</code> 等成员是一张RDKit分子图：原子是节点、化学键是边。</li><li>若对象含构象，坐标矩阵形状是 <code>[N,3]</code>：每行一个原子，三列依次为x、y、z。</li><li>原子对索引常见 <code>[2,E]</code>：每列是一条边的两个端点；距离上下界向量 <code>[E]</code> 与这些边逐一对齐。</li><li>手性/芳环索引的每一列是一组共同定义一个几何约束的原子编号，不能按普通特征矩阵解释。</li></ul>""",
        "BoltzGen解析蛋白、肽或非标准组分时按需查表；标准流程还会预载20种标准氨基酸。",
        "整包保留并从ZIP按需读取；不解压、不从45,227项中删出一个“小字典”。另用used_components.tsv记录项目实际出现过的组分。",
        "Python pickle不应在HTML或未知环境中直接执行。此报告只展示在官方SHA校验后离线生成的RDKit摘要。",
        [("固定revision的mols.zip", "https://huggingface.co/datasets/boltzgen/inference-data/blob/c3d36fd276e9caf098c75d4113c6d5eb320b1a4c/mols.zip"), ("官方读取代码", "https://github.com/HannesStark/boltzgen/blob/v0.3.2/src/boltzgen/data/mol.py")],
    ))

    raw_details = []
    fasta = profiles["uniprot_fasta"]
    uj = profiles["uniprot_json"]
    ux = profiles["uniprot_xml"]
    ut = profiles["uniprot_tsv"]
    uniprot_sample = (
        "[FASTA]\n" + fasta["sample"] +
        "\n\n[JSON 摘要]\n" + json.dumps(uj["sample"], ensure_ascii=False, indent=2)[:6500] + "\n…（显示截断）" +
        "\n\n[TSV]\n" + "\n".join("\t".join(row) for row in ut["sample_rows"]) +
        "\n\n[XML]\n" + ux["sample"]
    )
    raw_details.append(detail(
        "UniProtKB P01275：人胰高血糖素原",
        [("公开来源", ""), ("溯源输入", "")],
        "四个文件是同一个P01275条目的不同序列化，不是四个独立生物样本；P01275本身是180 aa前体，不是30 aa的GLP-1。",
        [
            ("文件", "FASTA / JSON / XML / TSV，共4个"),
            ("记录", "每种格式1条数据库记录"),
            ("序列", "180 aa pro-glucagon前体"),
            ("JSON注释", f"{uj['feature_count']}个features"),
            ("TSV", f"{ut['column_count']}列×{ut['data_row_count']}数据行"),
            ("角色", "序列、加工位点和Arg127酰胺注释的来源"),
        ],
        uniprot_sample,
        """<ul><li>FASTA第一行 <code>&gt;</code> 后是身份信息，后续字母是连续氨基酸序列；换行没有生物学边界。</li><li>JSON的 <code>features</code> 是注释对象列表，不是数值向量；<code>start/end</code> 是一基闭区间。</li><li>TSV一行代表一个数据库条目，一列代表固定属性。</li><li>XML是嵌套树；一个 <code>&lt;entry&gt;</code> 包含同一条记录的序列、注释和引用。</li><li>前体98..127对应7–36，100..127对应9–36；必须由sidecar表达末端化学，FASTA做不到。</li></ul>""",
        "仅作为清理/注册表生成的来源输入；不会把180 aa整条前体直接交给nanobody-anything。",
        "原始四格式完整保留；清理目录只留下项目四种GLP-1状态的FASTA和带端基说明的JSON。",
        "UniProt序列字母本身不能编码C端–NH2；酰胺证据来自feature注释，必须与结构几何分开保存。",
        [("UniProt P01275", "https://www.uniprot.org/uniprotkb/P01275/entry"), ("UniProt REST说明", "https://www.uniprot.org/help/api_queries")],
    ))

    psdf = profiles["pubchem_sdf"]
    pj = profiles["pubchem_json"]
    px = profiles["pubchem_xml"]
    pubchem_sample = (
        "[SDF头部与原子/键片段]\n" + psdf["sample"] +
        "\n\n[JSON结构摘要]\n" + json.dumps(pj["sample"], ensure_ascii=False, indent=2)[:6000] + "\n…（显示截断）" +
        "\n\n[XML]\n" + px["sample"]
    )
    raw_details.append(detail(
        "PubChem CID 16133831：GLP-1(7–36)酰胺化学记录",
        [("公开来源", ""), ("化学身份", "")],
        "SDF、JSON和XML是同一个CID的三种表示；它们提供化学连接与身份核对，不提供可直接替代蛋白三维结构的坐标。",
        [
            ("文件", "SDF / JSON / XML，共3个"),
            ("记录", "每种格式1个化合物"),
            ("显式原子", f"{psdf['molecules'][0]['atom_count']}个（含氢）"),
            ("化学键", f"{psdf['molecules'][0]['bond_count']}条"),
            ("构象", "1个二维构象；z=0"),
            ("角色", "确认30 aa与C端酰胺的化学身份"),
        ],
        pubchem_sample,
        """<ul><li>SDF计数行中的 <code>460465</code> 是两个固定宽度字段：460个原子和465条键，不是460,465。</li><li>原子块每行一个原子，前三列x/y/z；本记录是二维图，所以z为0。</li><li>键块每行一条键：两个一基原子编号加键级。</li><li>JSON中 <code>atoms.aid[i]</code> 与 <code>atoms.element[i]</code> 是并行数组的同一原子；<code>bonds.aid1[i]</code>、<code>aid2[i]</code>、<code>order[i]</code> 共同定义第i条边。</li></ul>""",
        "作为化学身份参考输入；不会把二维SDF当作BoltzGen三维target结构。",
        "保留原始三格式；另生成一条只含项目相关身份字段的精简JSON，但不改写官方原始记录。",
        "二维连接图能证明原子连接和端基标注，却不能说明肽在溶液或受体中的三维构象。PubChem SDF第二行含动态OEChem生成时间，故完整文件SHA是下载快照SHA；去掉该时间戳行后的规范化内容哈希已独立比对一致。",
        [("PubChem CID 16133831", "https://pubchem.ncbi.nlm.nih.gov/compound/16133831"), ("PUG REST", "https://pubchem.ncbi.nlm.nih.gov/docs/pug-rest")],
    ))

    pdb_info = {
        "1d0r": {
            "title": "RCSB PDB 1D0R：7–36 amide溶液NMR集合",
            "role": "正靶几何来源",
            "clean": "只含GLP-1链A；清理后保留20个NMR模型。",
            "caveat": "标题写amide，但坐标末端ARG含OXT，元素组成与真正酰胺化记录不一致，普通聚合物坐标没有把C端酰胺原子级编码。它还来自三氟乙醇/水NMR环境，20个归档模型不是生理水相概率分布；只能作为几何来源。",
        },
        "6x18": {
            "title": "RCSB PDB 6X18：GLP-1–GLP-1R复合物",
            "role": "受体结合态正靶几何来源",
            "clean": "从完整复合物只抽取label链E／auth链P的30 aa肽；去掉受体、G蛋白、Nanobody35和水。",
            "caveat": "来源实体名明确NH2，但233个重原子中没有额外的C端酰胺氮；ARG内名为NH2的原子属于精氨酸胍基侧链，不是C端酰胺。标准聚合物坐标不能证明BoltzGen看见或往返保留该端基。",
        },
        "9ivg": {
            "title": "RCSB PDB 9IVG：9–36–GLP-1R–Gs复合物",
            "role": "反靶／挑战态几何参考",
            "clean": "从复合物只抽取label链A／auth链P；声明28 aa，但仅9..29共21个残基有坐标。",
            "caveat": "阻断：缺失C端7个残基、条目未声明NH2，且构象受GLP-1R/Gs约束；不能当完整游离9–36NH2真值或实验阴性标签。",
        },
        "7eow": {
            "title": "RCSB PDB 7EOW：caplacizumab VHH示例来源",
            "role": "纳米抗体scaffold冒烟测试来源",
            "clean": "只保留VHH链B的128个有坐标残基；去抗原和水。",
            "caveat": "官方示例不等于筛选、验证或推荐的生产VHH框架；清理文件状态为provisional_example。",
        },
        "7xl0": {
            "title": "RCSB PDB 7XL0：vobarilizumab VHH示例来源",
            "role": "纳米抗体scaffold冒烟测试来源",
            "clean": "只保留VHH链A的121个有坐标残基；去第二晶体拷贝、硫酸根、甘油和水。",
            "caveat": "官方示例不等于生产scaffold库；清理文件状态为provisional_example。",
        },
    }
    for pdb_id, info in pdb_info.items():
        profile = profiles[f"raw_cif_{pdb_id}"]
        sample = rows_sample(profile["sample_columns"], profile["sample_rows"])
        raw_details.append(detail(
            info["title"],
            [("PDBx/mmCIF", ""), ("原始结构", "")],
            "一个文件是一条PDB deposition；文件内部可包含多个模型、多条链、非聚合物、水以及大量原子观测。",
            [
                ("本地路径", profile["path"]),
                ("文件大小", f"{profile['bytes']:,} B｜{pretty_bytes(profile['bytes'])}"),
                ("坐标模型", str(profile["model_count"])),
                ("_atom_site行", f"{profile['atom_site_row_count']:,}"),
                ("label链数（含非聚合物）", str(profile["label_chain_count"])),
                ("项目用途", info["role"]),
            ],
            sample,
            """<ul><li>mmCIF由多个命名category组成，不是一张单一矩阵；<code>loop_</code>下的字段名是列。</li><li><code>_atom_site</code>每行是某个模型中的一个原子观测。</li><li><code>label_asym_id</code>是标准化链，<code>auth_asym_id</code>是作者链；BoltzGen应使用label坐标。</li><li><code>label_seq_id</code>是一基标准残基号；<code>Cartn_x/y/z</code>是Å坐标。</li><li>抽成 <code>[N,3]</code> 坐标矩阵后：每行一个原子，三列为x/y/z；整体旋转和平移无意义，相对距离才有意义。</li></ul>""",
            "原始结构是清理脚本的来源输入；完整复合物从不直接进入项目BoltzGen配置。",
            info["clean"],
            info["caveat"],
            [(f"RCSB {pdb_id.upper()}", f"https://www.rcsb.org/structure/{pdb_id.upper()}"), ("wwPDB atom_site说明", "https://mmcif.wwpdb.org/docs/tutorials/content/atomic-description.html")],
        ))

    for scaffold in ["7eow", "7xl0"]:
        yp = profiles[f"example_yaml_{scaffold}"]
        raw_details.append(detail(
            f"BoltzGen v0.3.2 {scaffold.upper()} scaffold YAML示例",
            [("官方仓库示例", "warn"), ("配置文件", "")],
            "这是一条scaffold配方，不是模型训练数据，也不是官方验证过的生产框架库。",
            [
                ("本地路径", yp["path"]),
                ("文件大小", f"{yp['bytes']:,} B"),
                ("记录", "1个YAML配置树"),
                ("顶层字段", ", ".join(yp["top_level_keys"])),
                ("配对结构", f"{scaffold}.cif"),
                ("状态", "provisional_example / smoke_test_only"),
            ],
            yp["sample"],
            """<ul><li>YAML是键值和列表组成的配置树，不是矩阵。</li><li><code>path</code>指向配对CIF；<code>include</code>选链；<code>design</code>定义允许设计的CDR区。</li><li><code>exclude</code>先删模板残基，<code>design_insertions</code>规定插入长度范围。</li><li>残基编号一基，并按mmCIF的label坐标解释；路径相对YAML所在目录。</li></ul>""",
            "与清理后的VHH CIF配对，可作为nanobody-anything的冒烟测试输入。",
            "原始YAML保留作溯源；清理目录复制同一规则并把相对路径对准清理后的同名CIF。",
            "不能仅因文件位于官方example目录就称为VHH数据库或项目批准scaffold。",
            [("v0.3.2 nanobody_scaffolds示例目录", "https://github.com/HannesStark/boltzgen/tree/v0.3.2/example/nanobody_scaffolds"), (f"{scaffold} YAML", f"https://github.com/HannesStark/boltzgen/blob/v0.3.2/example/nanobody_scaffolds/{scaffold}.yaml")],
        ))

    curated_details = []
    variants_sample = json.dumps({
        "source_accession": variants["source_accession"],
        "coordinate_system": variants["coordinate_system"],
        "variants": variants["variants"],
    }, ensure_ascii=False, indent=2)
    curated_details.append(detail(
        "GLP1_project_variants：四种项目化学／序列状态",
        [("清理后注册表", ""), ("4条", "")],
        "FASTA保存残基字母；JSON sidecar保存长度、前体坐标、N/C端说明与项目角色，两者必须成对读取。",
        [
            ("FASTA路径", "curated_project_inputs/sequence_chemistry/GLP1_project_variants.fasta"),
            ("JSON路径", "curated_project_inputs/sequence_chemistry/GLP1_project_variants.json"),
            ("记录", "4个状态：7–36NH2、9–36、7–37、9–37"),
            ("正靶", "GLP1_7-36_NH2"),
            ("反靶/挑战", "9–36以及7–37、9–37"),
            ("来源", "UniProt P01275 + PubChem CID16133831"),
        ],
        variants_sample,
        """<ul><li>JSON顶层 <code>variants</code> 是对象列表；每个对象是一种明确的序列/端基状态，不是数值向量。</li><li><code>sequence</code>由单字母氨基酸组成；<code>length_residues</code>必须等于字母数。</li><li><code>precursor_coordinates_1_based</code>使用UniProt前体的一基闭区间。</li><li>FASTA不能编码–NH2，所以不得把FASTA名称当作原子级端基已验证的证据。</li></ul>""",
        "这是靶状态准备与多状态评估的输入；会被转换成每个构象一个结构/配置任务。",
        "只保留当前项目四种GLP-1状态，未把P01275中的胰高血糖素、OXM、GLP-2等其他产物混入项目注册表。",
        "9–36的项目端基需另行定义；9IVG本身不验证NH2。注册表不会把推导状态伪装成直接实验结构证据。",
        [("UniProt P01275", "https://www.uniprot.org/uniprotkb/P01275/entry"), ("PubChem CID16133831", "https://pubchem.ncbi.nlm.nih.gov/compound/16133831")],
    ))

    pubchem_project_path = CURATED / "sequence_chemistry" / "PubChem_CID16133831_project_record.json"
    pubchem_project = load_json(pubchem_project_path)
    curated_details.append(detail(
        "PubChem_CID16133831_project_record：精简化学身份",
        [("清理后参考", ""), ("1条", "")],
        "这是从官方PubChem JSON中过滤出的项目相关字段，便于人工核对；官方原始JSON仍完整保留。",
        [
            ("本地路径", str(pubchem_project_path.relative_to(ROOT))),
            ("文件大小", f"{pubchem_project_path.stat().st_size:,} B"),
            ("记录", "1个过滤后的化合物对象"),
            ("角色", "化学身份与端基参考"),
            ("直接模型输入", "否"),
            ("官方原始记录", "raw_sources/pubchem_CID16133831/CID16133831.json"),
        ],
        json.dumps(pubchem_project, ensure_ascii=False, indent=2)[:7000] + "\n…（如有更多字段则显示截断）",
        "<p>对象的每个键是一项身份或属性；它不是特征向量，也没有可用于训练的行×列样本结构。</p>",
        "供靶状态注册与人工QC读取；不直接传给BoltzGen。",
        "只复制项目所需身份字段；没有删除或覆盖raw目录中的官方记录。",
        "精简JSON是派生物，权威来源仍是固定CID的官方原始记录与SHA。",
        [("PubChem CID16133831", "https://pubchem.ncbi.nlm.nih.gov/compound/16133831")],
    ))

    curated_records = {}
    for path in CURATED.rglob("*_curation.json"):
        record = load_json(path)
        curated_records[record["curated_path"]] = record

    curated_profile_items = [
        (key, value) for key, value in profiles.items() if key.startswith("curated_cif_")
    ]
    for _, profile in sorted(curated_profile_items, key=lambda pair: pair[1]["path"]):
        record = curated_records[profile["path"]]
        mapping_path = ROOT / record["mapping_path"]
        mapping_rows = read_tsv(mapping_path)
        mapping_columns = list(mapping_rows[0].keys()) if mapping_rows else []
        mapping_sample = rows_sample(mapping_columns, [[row[column] for column in mapping_columns] for row in mapping_rows[:6]]) if mapping_rows else "（无映射行）"
        sample = (
            "[_atom_site坐标片段]\n" + rows_sample(profile["sample_columns"], profile["sample_rows"]) +
            "\n\n[residue_mapping.tsv片段]\n" + mapping_sample +
            "\n\n[curation.json核心字段]\n" + json.dumps({
                "artifact_id": record["artifact_id"],
                "status": record["status"],
                "project_role": record["project_role"],
                "source_chain_mapping": record["source_chain_mapping"],
                "raw_declared_sequence_length": record["raw_declared_sequence_length"],
                "observed_coordinate_sequence_length": record["observed_coordinate_sequence_length"],
                "unresolved_declared_positions": record["unresolved_declared_positions"],
                "atom_count_semantics": record.get("atom_count_semantics"),
                "terminal_chemistry": record["terminal_chemistry"],
                "paired_yaml": record.get("paired_yaml"),
                "excluded_content": record["excluded_content"],
            }, ensure_ascii=False, indent=2)
        )
        is_vhh = record["status"] == "provisional_example"
        is_9ivg = record["source_pdb_id"] == "9IVG"
        if is_vhh:
            badges = [("清理后VHH", ""), ("仅冒烟测试", "warn")]
        elif is_9ivg:
            badges = [("清理后反靶几何", ""), ("不完整/阻断", "stop")]
        else:
            badges = [("清理后正靶几何", ""), ("端基待验证", "warn")]
        facts = [
            ("本地路径", profile["path"]),
            ("文件大小", f"{profile['bytes']:,} B｜{pretty_bytes(profile['bytes'])}"),
            ("模型", str(profile["model_count"])),
            ("_atom_site行", f"{profile['atom_site_row_count']:,}"),
            ("声明/观察残基", f"{record['raw_declared_sequence_length']} / {record['observed_coordinate_sequence_length']}"),
            ("状态", record["status"]),
        ]
        if is_vhh and record["source_pdb_id"] == "7EOW":
            cleaning_text = f"{record['excluded_content']}；仅保留单一VHH有坐标核心。因未观测Met1被删除，派生YAML的design/visibility/exclude/insertion索引全部减1；raw官方YAML保持不变。"
            caveat = "修正后的CIF/YAML索引配对已通过逐残基复核，但该结构仍只进入smoke-test白名单；不得作为项目批准的生产VHH框架。"
        elif is_vhh:
            cleaning_text = f"{record['excluded_content']}；只保留链A，并把27对altloc A/B原子统一解析为A构象，使_atom_site从942行降至915行。"
            caveat = "等占有率altloc中选择A只是为形成单构象输入，不表示A具有生物学优势；该结构仍只可smoke test。"
        elif is_9ivg:
            cleaning_text = f"{record['excluded_content']}；保留21个实际有坐标残基并在映射中记录来源编号，不虚构缺失坐标。"
            caveat = "阻断：缺失的7个C端残基不得用零坐标或臆测补齐；NH2未验证，且来源为受体/Gs结合态。"
        else:
            cleaning_text = f"{record['excluded_content']}；只保留GLP-1肽链，完整记录源链、重编号与每个残基的原始索引。"
            caveat = "可用于几何MVP，但C端酰胺尚未完成BoltzGen解析→生成→复折叠输出的原子/键闭环验证。"
        curated_details.append(detail(
            record["artifact_id"],
            badges,
            f"这是从RCSB原始结构提取出的单链项目资产；项目角色：{record['project_role']}。",
            facts,
            sample,
            """<ul><li>坐标表每行仍是一个清理后重原子；<code>Cartn_x/y/z</code>三列单位为Å。</li><li>原始<code>_atom_site</code>行数不一定等于运行时原子数：1D0R原始每模型458行含224个H，清理后是234个重原子；7XL0原始链A的942行含27对altloc，清理后是915个单构象原子。</li><li>残基映射TSV每行是“一个模型中的一个保留残基”，不是一个原子；它把清理顺序、label/auth编号和保留原子数连起来。</li><li>多个NMR模型是同一分子的多个构象，不是多个不同序列样本。</li><li><code>curation.json</code>是一条转换记录，声明删除内容、观察缺口、端基限制与哈希。</li></ul>""",
            "清理CIF是几何输入；mapping和curation JSON是该输入的审计输出，并在运行前用于自动QC。",
            cleaning_text,
            caveat,
            [(f"RCSB {record['source_pdb_id']}", f"https://www.rcsb.org/structure/{record['source_pdb_id']}"), ("wwPDB atom_site说明", "https://mmcif.wwpdb.org/docs/tutorials/content/atomic-description.html")],
        ))

    allowlist_sample = rows_sample(list(allowlist[0].keys()), [[row[key] for key in allowlist[0].keys()] for row in allowlist])
    component_sample = rows_sample(list(used_components[0].keys()), [[row[key] for key in used_components[0].keys()] for row in used_components[:8]])
    curated_details.append(detail(
        "project_input_allowlist.tsv + used_components.tsv",
        [("配置边界", ""), ("机器可读", "")],
        "白名单决定项目配置可引用什么；组分表只记录实际使用轨迹，不用于裁剪mols.zip。",
        [
            ("白名单条目", str(len(allowlist))),
            ("当前组分", str(len(used_components))),
            ("白名单路径", "curated_project_inputs/project_input_allowlist.tsv"),
            ("组分路径", "curated_project_inputs/used_components.tsv"),
            ("raw直连", "禁止"),
            ("mols.zip策略", "完整保留"),
        ],
        "[project_input_allowlist.tsv]\n" + allowlist_sample + "\n\n[used_components.tsv 前8行]\n" + component_sample,
        "<p>两个TSV都是普通表：每行一个资产或一个CCD组分，每列一个固定属性。<code>source_assets</code>中的分号仅是列表分隔，不是数值向量。</p>",
        "配置生成器只允许读取白名单路径；组分表用于追踪而非模型特征。",
        "白名单显式标出geometry-only、blocked和smoke-test-only，避免“文件存在”被误解为“可以用于生产结论”。",
        "任何绕过白名单直接指向raw_sources的配置都应被拒绝。",
        [("BoltzGen示例格式说明", "https://github.com/HannesStark/boltzgen/blob/v0.3.2/example/README.md")],
    ))

    qa_rows = quality["checks"]
    qa_sample = json.dumps({
        "overall_status": quality["overall_status"],
        "checks": [{"id": item["id"], "passed": item["passed"]} for item in qa_rows],
    }, ensure_ascii=False, indent=2)
    qa_details = [detail(
        "最终机器校验与范围清单",
        [(quality["overall_status"], "warn")],
        "所有纳入的官方文件通过大小与SHA检查；科学限制被保留为阻断项，而不是被计入失败下载。",
        [
            ("质量检查", f"{len(qa_rows)}项"),
            ("通过", f"{sum(item['passed'] for item in qa_rows)}/{len(qa_rows)}"),
            ("官方下载", f"{scope['included']['official_download_file_count']}文件｜{scope['included']['official_download_bytes']:,} B"),
            ("运行资产", f"{scope['included']['runtime_required_file_count']}文件｜{scope['included']['runtime_required_bytes']:,} B"),
            ("原始来源", f"{scope['included']['raw_source_file_count']}文件｜{scope['included']['raw_source_bytes']:,} B"),
            ("清理结构", f"{machine_summary['curated_structure_count']}个文件／{machine_summary['curated_coordinate_model_count']}个模型"),
        ],
        qa_sample,
        "<p>质量JSON的每个对象是一项检查；<code>passed</code>是布尔值，<code>items</code>保存逐文件证据。不同记录单位不能相加：checkpoint用张量数，mols用CCD成员数，mmCIF用原子行/模型数。</p>",
        "这是数据准备流程的输出，也是每次运行前的门控输入。",
        "保留raw、clean、runtime三层；删除/排除的仅是MVP不需要的远程资产，没有删除溯源原件。",
        "PASS_WITH_DECLARED_LIMITATIONS不等于所有科学问题已解决；它表示下载与清理正确，且端基、缺失坐标、示例scaffold限制已被明确标注。",
        [("BoltzGen v0.3.2", "https://github.com/HannesStark/boltzgen/releases/tag/v0.3.2"), ("PyTorch序列化安全说明", "https://docs.pytorch.org/docs/main/notes/serialization.html")],
    )]

    future_output_html = DETAIL_STYLE + """
<section class="asset-guide">
  <h2>运行后会产生什么输出</h2>
  <p class="intro">当前目录只有准备与运行输入；尚未运行BoltzGen，所以没有把虚构的NPZ/CSV/CIF当成已有数据样例。</p>
  <div class="mini-table"><table><thead><tr><th>未来输出</th><th>一条记录</th><th>形状/字段</th><th>作用</th></tr></thead><tbody>
    <tr><td>intermediate_designs/*.cif</td><td>一个候选复合物</td><td>mmCIF原子表</td><td>design阶段输出、inverse folding输入</td></tr>
    <tr><td>*.npz</td><td>一个候选的数组包</td><td>如design_mask[T]、aa_constraint_mask[T,20]、coords[S,A,3]</td><td>阶段间机器输入</td></tr>
    <tr><td>refold_cif/*.cif</td><td>一个复折叠候选复合物</td><td>三维坐标</td><td>分析与筛选主输入</td></tr>
    <tr><td>aggregate_metrics_analyze.csv</td><td>一行一个候选</td><td>结构置信度与界面指标列</td><td>筛选/排序输出；不是KD</td></tr>
    <tr><td>results_overview.pdf</td><td>一次运行报告</td><td>可视化页面</td><td>人工审阅输出</td></tr>
  </tbody></table></div>
  <div class="note warning"><b>NPZ轴：</b> <code>[T]</code>每个位置一个token；<code>[T,20]</code>行是位置、列是20种标准氨基酸；<code>[S,A,3]</code>依次是采样结构、原子、x/y/z。读取时先用<code>allow_pickle=False</code>。</div>
</section>
"""

    inventory_rows = []
    stage_label = {"runtime": "运行资产", "raw_source": "原始来源", "curated": "清理后"}
    for row in inventory:
        inventory_rows.append({
            "stage": stage_label.get(row["stage"], row["stage"]),
            "asset": row["asset"],
            "role": row["role"],
            "format": row["format"],
            "record_unit": row["record_unit"],
            "record_count": int(row["record_count"]),
            "size": row["size_display"],
            "status": row["status"],
        })

    runtime_sizes = []
    for item in inventory:
        if item["stage"] == "runtime":
            runtime_sizes.append({
                "asset": item["asset"],
                "size_gb": round(int(item["size_bytes"]) / 1_000_000_000, 6),
                "bytes": int(item["size_bytes"]),
            })
    runtime_sizes.sort(key=lambda item: item["bytes"], reverse=True)

    runtime_table = "\n".join(
        f"- `{row['asset']}`：{row['bytes']:,} B（{pretty_bytes(row['bytes'])}）"
        for row in runtime_sizes
    )
    excluded_lines = "\n".join(
        f"- **{item['asset']}**：{item['reason']}" for item in scope["excluded_not_downloaded"]
    )
    blockers = "\n".join(f"- {item}" for item in scope["blocking_caveats"])

    generated_at = datetime.now(timezone.utc).isoformat()
    summary_source = {
        "id": "package_summary_query",
        "label": "MVP package summary from reviewed manifests",
        "path": "metadata/file_inventory.tsv",
        "query": {
            "engine": "duckdb",
            "sql": """WITH inventory AS (
  SELECT * FROM read_csv_auto('metadata/file_inventory.tsv', delim='\\t', header=true)
), model_summary AS (
  SELECT curated_coordinate_model_count
  FROM read_json_auto('metadata/machine_readable_summary.json')
)
SELECT
  count(*) FILTER (WHERE stage='runtime') AS runtime_files,
  sum(size_bytes) FILTER (WHERE stage='runtime') / 1000000000.0 AS runtime_gb,
  count(*) FILTER (WHERE stage='raw_source') AS raw_files,
  max(curated_coordinate_model_count) AS curated_models
FROM inventory CROSS JOIN model_summary""",
            "description": "Reproduces the four package headline metrics from the reviewed local manifests.",
            "tables_used": ["metadata/file_inventory.tsv", "metadata/machine_readable_summary.json"],
        },
    }
    runtime_size_source = {
        "id": "runtime_size_query",
        "label": "Verified runtime asset sizes",
        "path": "metadata/file_inventory.tsv",
        "query": {
            "engine": "duckdb",
            "sql": """SELECT asset, size_bytes / 1000000000.0 AS size_gb, size_bytes AS bytes
FROM read_csv_auto('metadata/file_inventory.tsv', delim='\\t', header=true)
WHERE stage='runtime'
ORDER BY size_bytes DESC""",
            "description": "Ranks the five verified runtime assets by exact downloaded bytes.",
            "tables_used": ["metadata/file_inventory.tsv"],
        },
    }
    inventory_source = {
        "id": "inventory_query",
        "label": "Final file inventory",
        "path": "metadata/file_inventory.tsv",
        "query": {
            "engine": "duckdb",
            "sql": """SELECT stage, asset, role, format, record_unit,
       CAST(record_count AS BIGINT) AS record_count,
       size_display AS size, status
FROM read_csv_auto('metadata/file_inventory.tsv', delim='\\t', header=true)
ORDER BY CASE stage WHEN 'runtime' THEN 1 WHEN 'raw_source' THEN 2 ELSE 3 END, path""",
            "description": "Returns the reviewed runtime, raw-source and curated file inventory shown in the report.",
            "tables_used": ["metadata/file_inventory.tsv"],
        },
    }
    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "BoltzGen nanobody-anything MVP：数据资产、样例与清理审计",
            "description": "版本锁定的运行资产、公开来源、项目清理输入、格式/轴解释和逐项样例。",
            "generatedAt": generated_at,
            "cards": [{
                "id": "package_summary",
                "description": "MVP数据包的精确范围。",
                "dataset": "summary",
                "source": summary_source,
                "metrics": [
                    {"label": "必需运行资产", "field": "runtime_files"},
                    {"label": "运行资产总计（GB）", "field": "runtime_gb"},
                    {"label": "公开来源文件", "field": "raw_files"},
                    {"label": "清理坐标模型", "field": "curated_models"},
                ],
            }],
            "charts": [{
                "id": "runtime_size_chart",
                "title": "MVP运行资产下载体积",
                "subtitle": "5个nanobody-anything必需资产；按十进制GB排序，精确字节见下方清单。",
                "type": "leaderboard",
                "dataset": "runtime_sizes",
                "source": runtime_size_source,
                "valueFormat": "decimal",
                "encodings": {
                    "x": {"field": "asset", "type": "nominal", "label": "资产"},
                    "y": {"field": "size_gb", "type": "quantitative", "label": "GB"},
                },
            }],
            "tables": [{
                "id": "asset_inventory",
                "title": "逐文件数据资产清单",
                "subtitle": "记录数量必须结合“记录单位”解释，不能跨checkpoint、化学字典和结构文件直接相加。",
                "dataset": "inventory",
                "source": inventory_source,
                "density": "compact",
                "layout": "full",
                "columns": [
                    {"field": "stage", "label": "层级", "type": "text"},
                    {"field": "asset", "label": "资产", "type": "text"},
                    {"field": "role", "label": "输入/输出角色", "type": "text"},
                    {"field": "format", "label": "格式", "type": "text"},
                    {"field": "record_unit", "label": "一条数据", "type": "text"},
                    {"field": "record_count", "label": "数量"},
                    {"field": "size", "label": "体积", "type": "text"},
                    {"field": "status", "label": "状态", "type": "text"},
                ],
            }],
            "sources": [
                {"id": "scope_manifest", "label": "MVP scope manifest", "path": "metadata/mvp_scope.json", "href": "https://github.com/HannesStark/boltzgen/releases/tag/v0.3.2"},
                {"id": "runtime_manifest", "label": "Verified runtime manifest", "path": "runtime_cache/runtime_manifest.json", "href": "https://huggingface.co/boltzgen/boltzgen-1/tree/c1be29e1f82ffcc72264f64b993c43fb4e0d17f0"},
                {"id": "file_inventory", "label": "Final file inventory", "path": "metadata/file_inventory.tsv"},
            ],
            "blocks": [
                {
                    "id": "summary",
                    "type": "markdown",
                    "body": f"""## 技术结论

MVP数据包已按 **runtime / raw / curated** 三层分离。官方远程下载共 **{scope['included']['official_download_file_count']}个文件、{scope['included']['official_download_bytes']:,} B**；其中5个必需运行资产为 **{scope['included']['runtime_required_bytes']:,} B（{scope['included']['runtime_required_bytes']/1_000_000_000:.3f} GB）**。原始来源保留完整以便复现，但项目配置不得直接读取raw目录。

最终QA为 **{quality['overall_status']}**：文件完整性与清理链路通过；C端酰胺的原子级往返、9IVG缺失坐标以及VHH示例框架的生产适用性仍是明确限制。""",
                },
                {"id": "metrics", "type": "metric-strip", "cardIds": ["package_summary"]},
                {"id": "runtime_chart", "type": "chart", "chartId": "runtime_size_chart"},
                {
                    "id": "runtime_exact",
                    "type": "markdown",
                    "body": "## 精确运行资产体积\n\n" + runtime_table + f"\n\n合计：**{scope['included']['runtime_required_bytes']:,} B**。`mols.zip`运行时可直接从ZIP按需读取，解压后的1.820 GB不是额外必须占用的磁盘。",
                },
                {
                    "id": "scope",
                    "type": "markdown",
                    "body": """## 范围、数据与定义

- **运行资产**：预训练checkpoint和完整化学组分字典；它们是推理输入，不是本项目要重新训练的数据集。
- **原始来源**：UniProt、PubChem、RCSB和官方示例配置的不可变副本；用于溯源和清理。
- **清理后输入**：只含项目选定GLP-1肽链、两条VHH示例链、序列/端基注册表、映射和白名单。
- **记录数量**：必须连同记录单位阅读。一个checkpoint记录单位是张量，一个mmCIF记录单位可指模型或原子行，一个ZIP记录单位是CCD组分。
- **输入/输出**：同一个文件可以是上一步输出、下一步输入。例如清理CIF是清理流程输出，也是推理准备输入。
- **生产就绪状态**：当前是可审计的MVP准备包，不是可直接得出选择性结论的生产输入；尚缺正式顶层run YAML、端基原子级闭环和完整9–36构象集合。""",
                },
                {"id": "inventory", "type": "table", "tableId": "asset_inventory", "layout": "full"},
                {"id": "runtime_details", "type": "html", "body": html_block("运行时模型与化学字典：点击查看样例", "每个模块都给出真实本地统计、样例、shape解释、输入/输出角色和清理策略。", runtime_details)},
                {"id": "raw_details", "type": "html", "body": html_block("公开原始来源：点击查看样例", "raw_sources保持不可变；完整复合物与180 aa前体只用于溯源，不会直接进入推理。", raw_details)},
                {"id": "curated_details", "type": "html", "body": html_block("项目清理数据：点击查看样例", "curated_project_inputs只保留项目相关内容；每个结构都配有残基映射和清理记录。", curated_details)},
                {"id": "future_outputs", "type": "html", "body": future_output_html},
                {
                    "id": "method",
                    "type": "markdown",
                    "body": """## 清理与统计方法

1. 固定BoltzGen v0.3.2、模型仓库revision和mols字典revision。
2. 下载到暂存区，核对预期字节数与SHA-256后才原子移动到runtime_cache。
3. raw_sources保存官方文件；对RCSB结构按`label_asym_id`抽链，删除受体、G蛋白、抗原、水、配体、离子和多余晶体拷贝。
4. 为每个清理结构写`curation.json`与`residue_mapping.tsv`，保留源哈希、链映射、缺失残基、端基限制和删除内容。
5. checkpoint只在官方哈希通过后剖析。由于PyTorch 2.9的`weights_only=True`不支持其pickle protocol 5，本报告只读取ZIP中的`data.pkl`，用受限元数据解析器把storage/tensor重建为shape代理；不导入项目类、不读取张量storage，也没有回退到`weights_only=False`。
6. `mols.zip`保持完整，通过中央目录统计45,227个成员；仅在哈希通过后读取少量标准氨基酸生成离线摘要。
7. 最终配置只能引用`project_input_allowlist.tsv`。""",
                },
                {"id": "qa", "type": "html", "body": html_block("质量检查与可追溯性", "PASS_WITH_DECLARED_LIMITATIONS表示完整性通过且科学边界已明确，不表示所有结构化学问题已解决。", qa_details)},
                {
                    "id": "limitations",
                    "type": "markdown",
                    "body": "## 限制与稳健性\n\n" + blockers + "\n\n### 本次明确未下载\n\n" + excluded_lines,
                },
                {
                    "id": "next_steps",
                    "type": "markdown",
                    "body": """## 下一步

1. 先用7EOW或7XL0中的**一个**示例scaffold和一个7–36几何构象做50个设计的smoke test。
2. 在正式比较7–36NH2与9–36NH2前，完成C端酰胺在BoltzGen解析、生成CIF和Boltz-2复折叠输出中的原子/键往返测试。
3. 为9–36建立完整的多构象参考，不用9IVG的21残基片段替代28残基完整状态。
4. 生产阶段另建经审核、去标签、编号一致且覆盖多个框架家族的VHH scaffold库。
5. 运行后把每个候选的CIF、NPZ与CSV纳入同一manifest，再做正靶/反靶统一重预测与实验校准。

## 进一步问题

- BoltzGen v0.3.2是否能可靠往返保留自定义C端酰胺组分和共价键？
- 9–36完整构象集合应来自MD、同源约束还是独立结构预测组合？
- 两个示例VHH框架是否满足表达、二硫键、CDR长度与序列责任范围要求？""",
                },
            ],
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "summary": [{
                    "runtime_files": scope["included"]["runtime_required_file_count"],
                    "runtime_gb": round(scope["included"]["runtime_required_bytes"] / 1_000_000_000, 3),
                    "raw_files": scope["included"]["raw_source_file_count"],
                    "curated_models": machine_summary["curated_coordinate_model_count"],
                }],
                "runtime_sizes": runtime_sizes,
                "inventory": inventory_rows,
            },
        },
        "package_info": {
            "mode": "portable_html",
            "controls": {"edit": False, "refresh": False, "persistence": False, "copyAsImage": False},
        },
    }
    output = META / "report_artifact.json"
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "blocks": len(artifact["manifest"]["blocks"]),
        "inventory_rows": len(inventory_rows),
        "runtime_details": len(runtime_details),
        "raw_details": len(raw_details),
        "curated_details": len(curated_details),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
