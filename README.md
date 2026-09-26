# 税海拾珠 · TaxPearls

AI 税务审计 SaaS 平台。

> 当前状态：**MVP 已跑通** —— 输入审计材料 Excel，输出带完整证据链的 A4 PDF 审计报告。
> 判定全部由确定性规则引擎完成，未使用生成式模型进行风险识别。

---

## 快速开始

```bash
# 1. 依赖（项目内置独立 venv，不污染全局环境）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

# 2. 生成仿真样例材料（首次运行需要）
.venv/Scripts/python.exe scripts/make_sample.py

# 3. 端到端运行：材料 → 规则判定 → PDF 报告
.venv/Scripts/python.exe main.py
```

产物：

| 路径 | 说明 |
| --- | --- |
| `output/report.html` | 报告 HTML，可用浏览器直接预览 |
| `output/税务风险审计报告-<企业>-<日期>.pdf` | A4 PDF 报告（4 页） |

选项：

```bash
python main.py -i 某企业材料.xlsx      # 指定输入
python main.py -r rules/               # 指定规则目录
python main.py -o output/报告.pdf      # 指定输出路径
python main.py -q                      # 静默模式
```

---

## 目录结构

```
TaxPearls/
├── main.py                        命令行入口
├── requirements.txt
├── src/
│   ├── config.py                  科目映射与行业参考值（标准化的落地位置）
│   ├── models.py                  核心数据模型
│   ├── loader.py                  审计材料解析与标准化
│   ├── engine.py                  YAML 规则加载与条件求值
│   └── render.py                  HTML 渲染与 PDF 导出
├── rules/                         YAML 规则库（声明式）
├── templates/report.html          审计报告模板（中文排版）
├── samples/                       仿真样例材料
├── scripts/make_sample.py         样例数据生成器
├── docs/                          产品与技术文档
└── prototype/                     早期界面原型（仅作参考，非工程基线）
```

---

## 输入：审计材料

单个 Excel，三张必需工作表 + 一张说明表：

| 工作表 | 内容 |
| --- | --- |
| `企业信息` | 企业名称、纳税人识别号、所属行业、所属期 |
| `科目余额表` | 科目编码、科目名称、期初余额、本期借方、本期贷方、期末余额 |
| `增值税申报` | 销售额、销项税额、进项税额、应纳税额、期末留抵税额 |
| `填表说明` | 供填表人阅读，系统不读取 |

**标准化**：各财务软件科目编码口径不同，统一在 `src/config.py` 的 `ACCOUNT_MAP` 中映射为标准指标。
科目编码不在映射表覆盖范围内的，该科目不参与计算，不报错。

---

## 输出：审计报告

A4 PDF，共 4 页：

1. **封面** —— 被审计单位、审计期间、报告编号、出具日期
2. **审计概况 + 结论摘要 + 规则执行清单** —— 含命中/通过/未执行统计
3. **风险事项明细** —— 每项风险一张证据卡
4. **检查通过事项 + 未能执行事项 + 报告声明** —— 含编制/复核签署栏

每张**证据卡**包含：

```
风险类型 / 命中规则 / 风险等级
计算过程        —— 完整算式与阈值
证据表          —— 项目 / 金额数值 / 取数来源（逐项标明数据从哪来）
法律依据        —— 具体法条
整改建议
```

---

## 规则库（YAML 声明式）

三种求值类型：

| type | 语义 |
| --- | --- |
| `deviation` | 相对偏离度：`\|left − right\| / \|right\| > threshold` |
| `amount_mismatch` | 绝对差额：`\|left − right\| > tolerance` |
| `ratio_range` | 比率区间：`numerator / denominator` 落在 `[min, max]` 之外 |

规则示例：

```yaml
id: R-001
name: 收入未足额申报
category: 收入完整性
tax_type: 增值税
severity: high            # high / medium / low
logic:
  type: deviation
  left: 营业收入
  right: 增值税.销售额
  threshold: 0.10
evidence: [营业收入, 增值税.销售额]
legal_basis:
  - 《中华人民共和国增值税法》第三条、第七条
suggestion: 核查账面收入与申报销售额的差异构成……
```

设计要点：**每条规则自带 `evidence` 与 `legal_basis`**，使「可举证」成为规则的固有属性，
而非事后拼装。会计与教师可直接参与编写规则，无需写代码。

### 三种判定状态

| 状态 | 含义 |
| --- | --- |
| 命中 | 超出阈值，构成风险事项 |
| 通过 | 已执行，未发现异常 |
| **未执行** | **材料不足，未能执行。不构成「已通过」结论**，报告中单独列示 |

---

## 技术路线

**HTML + CSS → Chromium 打印 → PDF**，不使用 Word 转换。

中文排版的精细控制（字体、行距、首行缩进、表格数字对齐、分页不断行、页脚页码）
只有 CSS 能做得好；Word 自动化又慢又脆。通过 Playwright 驱动本机已安装的
Chrome（`channel="chrome"`），无需下载 Chromium。

排版规范：

- 正文思源宋体 10.5pt / 行距 1.75 / 首行缩进 2em / 两端对齐
- 标题黑体，与正文形成字形对比
- 表格金额使用等宽数字（`tabular-nums`）+ 右对齐，便于纵向比对
- 风险卡片 `break-inside: avoid`，绝不跨页断裂
- 页脚含报告编号与「第 X 页 / 共 Y 页」

---

## 数据与安全

🚨 **样例数据全部由脚本生成，不含任何真实企业信息。**
**严禁使用真实企业账套作为教学或演示数据。**

详见 `docs/01-输入输出与业务逻辑.md` 第六节「数据安全基线」。

---

## 文档

| 文档 | 内容 |
| --- | --- |
| `docs/00-项目方向.md` | 市场定位、技术路线、能力排期、决策状态 |
| `docs/01-输入输出与业务逻辑.md` | 输入输出定义、业务逻辑、规则模型、安全基线、MVP 验收标准 |

---

## 免责声明

本项目输出的报告不构成税务鉴证、涉税鉴证或法律意见，不能替代税务机关的认定结论，
不能作为纳税申报的唯一依据。`rules/` 中涉及的行业税负率参考区间为示意值，
正式使用前须按当地税务机关公布的行业预警值校准。
