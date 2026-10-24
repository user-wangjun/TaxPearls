"""B1 样板数据：3 个教学单元 + 3 个实操案例（发票合规/账申勾稽/税会差异）。

幂等：按 (certificate_id, name) 判断，已存在则跳过。
用法：
    python -B -X utf8 scripts/seed_teaching_units.py [--db PATH] [--dry-run]

口径：单元/案例只存 rule_code；知识点从 knowledge_point_link 反查自动挂靠；
案例材料为脱敏仿真说明（来自 samples/ 合成数据派生，不含真实企业信息）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import settings as _settings  # noqa: F401,F402  先加载 .env（FIELD_KEY 与主库绑定）
_ = _settings
from webapp.storage import Store  # noqa: E402
from webapp import teaching_units  # noqa: E402

CERT_CODE = "cjkj-2027"

UNITS = [
    {
        "name": "凭证层·发票合规",
        "stage": "voucher",
        "standard": "票合法吗：票种、税率、购销方税号、开票日期与红冲状态自洽",
        "rule_codes": ["R-016", "R-019"],
        "deliverables": "发票要素核对清单 + 红字发票处理说明",
    },
    {
        "name": "数据层·账申勾稽",
        "stage": "data",
        "standard": "数对得上吗：账、表、票、申报四方口径一致，差异有调节过程",
        "rule_codes": ["R-002", "R-005", "R-006"],
        "deliverables": "勾稽底稿 + 差异调节表",
    },
    {
        "name": "结论层·税会差异",
        "stage": "conclusion",
        "standard": "判断可回溯吗：结论有原件位置、法条版本与计算过程，缺数标未执行",
        "rule_codes": ["R-010", "R-021", "R-023"],
        "deliverables": "证据卡（指标值·来源·计算·阈值·依据·建议）",
    },
]

CASES = [
    {
        "unit": "凭证层·发票合规",
        "name": "样板·开票与申报调节（脱敏仿真）",
        "scenario": "某商贸公司 2026 年 9 月属期：申报销售额与开票金额存在跨期确认差异。",
        "rule_codes": ["R-016"],
        "materials": [
            {"name": "进销项发票明细（仿真）", "note": "由 samples/ 合成数据派生，税号与名称已脱敏"},
            {"name": "增值税申报主表及附表（仿真）", "note": "合成生成，栏次口径与真实申报表一致"},
        ],
        "standard_conclusion": [
            {"rule_code": "R-016", "expected": "hit",
             "note": "申报销售额与开票金额差异需提供跨期确认调节说明；命中仅代表触发核对，不等于少报。"},
        ],
        "desensitization_note": "全部数据由合成样例派生，不含任何真实企业信息。",
    },
    {
        "unit": "数据层·账申勾稽",
        "name": "样板·一般计税应纳税额勾稽（脱敏仿真）",
        "scenario": "某制造业公司 2026 年 10 月属期：账面销项-进项与申报应纳税额存在留抵与转出差异。",
        "rule_codes": ["R-002"],
        "materials": [
            {"name": "科目余额表（仿真）", "note": "22210105/22210101 等科目余额合成生成"},
            {"name": "增值税申报主表（仿真）", "note": "一般计税应纳税额栏次合成生成"},
            {"name": "抵扣勾稽底稿（仿真）", "note": "期初留抵、进项转出、抵扣调整合成生成"},
        ],
        "standard_conclusion": [
            {"rule_code": "R-002", "expected": "hit",
             "note": "差异来自期初留抵与进项转出口径，须出调节表；不能直接以销项减进项代表应纳税额。"},
        ],
        "desensitization_note": "全部数据由合成样例派生，不含任何真实企业信息。",
    },
    {
        "unit": "结论层·税会差异",
        "name": "样板·毛利率偏离参考区间（脱敏仿真）",
        "scenario": "某零售公司 2026 年度：毛利率显著偏离同行业参考区间，且期间费用收入比异常。",
        "rule_codes": ["R-021", "R-023"],
        "materials": [
            {"name": "利润表（仿真）", "note": "收入、成本、期间费用合成生成"},
            {"name": "行业参考区间说明", "note": "示意核对参数，非税法规定的重要性标准"},
        ],
        "standard_conclusion": [
            {"rule_code": "R-021", "expected": "hit",
             "note": "偏离参考区间仅触发核对：需说明定价、品类结构等业务原因，差异必须能解释。"},
            {"rule_code": "R-023", "expected": "pass",
             "note": "期间费用收入比在容差内，通过但需保留计算过程留痕。"},
        ],
        "desensitization_note": "全部数据由合成样例派生，不含任何真实企业信息。",
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description="B1 样板单元与案例（幂等）")
    parser.add_argument("--db", default=None, help="库文件路径（默认 TAXPEARLS_DB / 真库）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    store = Store(args.db, keepalive=True) if args.db else Store(keepalive=True)
    try:
        with store.connect() as db:
            cert = db.execute("SELECT id FROM certificate WHERE code=?", (CERT_CODE,)).fetchone()
            if not cert:
                print(f"未找到证书 {CERT_CODE}，退出。")
                return 1
            cert_id = cert["id"]
            existing_units = {r["name"] for r in db.execute(
                "SELECT name FROM teaching_unit WHERE certificate_id=?", (cert_id,))}
            existing_cases = {r["name"] for r in db.execute(
                "SELECT name FROM teaching_case WHERE certificate_id=?", (cert_id,))}
            linked = {}
            for row in db.execute(
                    "SELECT target_id, knowledge_point_id FROM knowledge_point_link"
                    " WHERE target_type='rule' AND active=1"):
                linked.setdefault(row["target_id"], []).append(row["knowledge_point_id"])
            unlinked = teaching_units.unlinked_rules(store)

        staff = {"id": None, "name": "seed"}
        created = {"units": [], "cases": []}
        unit_ids = {}
        for unit in UNITS:
            if unit["name"] in existing_units:
                print(f"跳过已有单元：{unit['name']}")
                continue
            kps = sorted({kp for code in unit["rule_codes"] for kp in linked.get(code, [])})
            record = teaching_units.create_unit(
                store, staff, cert_id, name=unit["name"], stage=unit["stage"],
                standard=unit["standard"], knowledge_point_ids=kps,
                rule_codes=unit["rule_codes"], deliverables=unit["deliverables"])
            # 样板直接发布，供 B2 教师端展示
            record = teaching_units.update_unit(store, staff, record["id"], status="published")
            unit_ids[unit["name"]] = record["id"]
            created["units"].append(record["name"])
            print(f"单元：{unit['name']}（知识点 {len(kps)} 条，规则 {len(unit['rule_codes'])} 条）")

        for case in CASES:
            if case["name"] in existing_cases:
                print(f"跳过已有案例：{case['name']}")
                continue
            record = teaching_units.create_case(
                store, staff, cert_id, name=case["name"],
                unit_id=unit_ids.get(case["unit"]) or _find_unit(store, cert_id, case["unit"]),
                scenario=case["scenario"], rule_codes=case["rule_codes"],
                materials=case["materials"],
                standard_conclusion=case["standard_conclusion"],
                desensitization_note=case["desensitization_note"])
            record = teaching_units.update_case(store, staff, record["id"], status="published")
            created["cases"].append(record["name"])
            print(f"案例：{case['name']}（规则 {len(case['rule_codes'])} 条）")

        if unlinked:
            print(f"提示：以下规则未挂靠任何知识点（该班学不到）："
                  f"{', '.join(r['rule_code'] for r in unlinked)}")
        print(f"完成：新增单元 {len(created['units'])} 个、案例 {len(created['cases'])} 个"
              f"（dry-run={args.dry_run}，本脚本幂等可直接重跑）。")
        return 0
    finally:
        store.close()


def _find_unit(store, cert_id: str, name: str) -> str | None:
    with store.connect() as db:
        row = db.execute(
            "SELECT id FROM teaching_unit WHERE certificate_id=? AND name=?",
            (cert_id, name)).fetchone()
        return row["id"] if row else None


if __name__ == "__main__":
    raise SystemExit(main())
