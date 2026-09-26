"""税海拾珠 · AI 税务审计引擎 · 命令行入口（MVP）

用法：
    python main.py                                  # 使用默认样例材料
    python main.py -i 某企业材料.xlsx
    python main.py -i 材料.xlsx -r rules/ -o output/我的报告.pdf

流程：
    读取审计材料 → 标准化 → 加载 YAML 规则 → 逐条求值 → 渲染 HTML → 导出 A4 PDF
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import engine, loader, render  # noqa: E402
from src.models import SEVERITY_LABEL, STATUS_LABEL  # noqa: E402

DEFAULT_INPUT = ROOT / "samples" / "样例企业-审计材料.xlsx"
DEFAULT_RULES = ROOT / "rules"


def _hr(char: str = "─", width: int = 72) -> str:
    return char * width


def print_report(dataset, findings) -> None:
    c = dataset.company
    print()
    print(_hr("═"))
    print("  税海拾珠 · 税务风险审计引擎")
    print(_hr("═"))
    print(f"  被审计单位　{c.name}")
    print(f"  纳税人识别号　{c.taxpayer_id}")
    print(f"  所属行业　　{c.industry}")
    print(f"  审计期间　　{c.period}")
    print(_hr())
    print(f"  已提取指标 {len(dataset.metrics)} 项：")
    for name, m in dataset.metrics.items():
        print(f"    · {name:<16} {m.value:>16,.2f}    ← {m.source}")
    print(_hr())
    print(f"  规则判定结果（共 {len(findings)} 项）")
    print(_hr())

    for f in findings:
        status = STATUS_LABEL.get(f.status, f.status)
        sev = SEVERITY_LABEL.get(f.rule.severity, "")
        mark = {"hit": "✗", "pass": "✓", "skipped": "–"}.get(f.status, "?")
        label = f"[{status}]" + (f"[{sev}]" if f.hit else "")
        print(f"  {mark} {f.rule.id}  {f.rule.name}")
        print(f"      {label}  {f.conclusion}")
        if f.executed:
            print(f"      计算：{f.calculation}")
        print()

    hit = [f for f in findings if f.hit]
    print(_hr())
    print(
        f"  合计：命中 {len(hit)} 项"
        f"（高 {sum(1 for f in hit if f.rule.severity == 'high')}"
        f" / 中 {sum(1 for f in hit if f.rule.severity == 'medium')}"
        f" / 低 {sum(1 for f in hit if f.rule.severity == 'low')}）"
        f"　通过 {sum(1 for f in findings if f.status == 'pass')} 项"
        f"　未执行 {sum(1 for f in findings if f.status == 'skipped')} 项"
    )
    print(_hr("═"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="税海拾珠 · 税务风险审计引擎（MVP）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-i", "--input", default=str(DEFAULT_INPUT), help="审计材料 Excel 路径")
    ap.add_argument("-r", "--rules", default=str(DEFAULT_RULES), help="规则目录")
    ap.add_argument("-o", "--output", default=None, help="PDF 输出路径（默认自动命名）")
    ap.add_argument("-q", "--quiet", action="store_true", help="不打印中间结果")
    args = ap.parse_args(argv)

    try:
        dataset = loader.load(args.input)
    except loader.InputError as e:
        print(f"\n[输入错误] {e}\n", file=sys.stderr)
        return 2

    try:
        rules = engine.load_rules(args.rules)
    except engine.RuleError as e:
        print(f"\n[规则错误] {e}\n", file=sys.stderr)
        return 3

    try:
        findings = engine.run(rules, dataset)
    except engine.RuleError as e:
        print(f"\n[规则求值失败] {e}\n", file=sys.stderr)
        return 4

    if not args.quiet:
        print_report(dataset, findings)

    pdf_name = Path(args.output).name if args.output else None
    try:
        html_path, pdf_path = render.build(dataset, findings, pdf_name=pdf_name)
    except RuntimeError as e:
        print(f"\n[PDF 导出失败] {e}\n", file=sys.stderr)
        print(f"HTML 已生成，可用浏览器打开后手动打印为 PDF：{render.OUTPUT_DIR / 'report.html'}")
        return 5

    if args.output:
        target = Path(args.output)
        if target.resolve() != pdf_path.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(pdf_path.read_bytes())
            pdf_path = target

    print(f"  HTML 预览：{html_path}")
    print(f"  PDF 报告：{pdf_path}")
    print(f"  文件大小：{pdf_path.stat().st_size / 1024:.1f} KB")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
