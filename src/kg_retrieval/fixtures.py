"""Explicitly synthetic acceptance corpus. Never installed in a business database."""
from pathlib import Path
import hashlib

from .common import canonical, digest

TEXTS = [
    ("收入口径勾稽", "营业收入与申报销售额应在合成检查中核对主体、所属期及不含税口径。差额只形成待核查线索。"),
    ("交易撤回与票据处理", "货物售出后顾客退回，或原先开具的金额有误，本合成情境要求通过红字发票调整原始记录，并保存退货凭证。"),
    ("进项抵扣核对", "进项税额抵扣的合成检查需要核对凭证用途、认证期间以及可抵扣金额的来源。"),
    ("小规模优惠及排除项", "小规模优惠合成检查须同时核对适用条件。本条所述优惠不包括例外条款中列出的特殊交易。"),
    ("专项项目口径", "项目甲的旧口径用于历史对照。项目乙的核算口径独立，不因项目甲被修改而失效。"),
    ("历史运输项目", "运输服务历史月份的合成筛查使用当期版本，不能用后来的废止状态替代历史期间判断。"),
    ("浙江区域试验", "本合成浙江区域措施仅对 CN-33 的当地试验项目进行核对。"),
    ("施行日期待查", "本合成文件尚缺施行日期，不得作为已生效的政策依据。"),
    ("免税与零税率", "出口交易合成检查分别保存免税和零税率业务的原始分类及对应证明材料。"),
    ("跨期预收款", "收到预收款与确认销售收入的月份可能不同，合成检查须保留合同履约节点并核对时间差。"),
    ("不动产租赁", "不动产租赁的合成检查保存租赁起止期间、收款记录和合同，以便核对统计范围。"),
    ("完整长条款与例外", "本条为超预算工程样本，完整保留原文。" + "长条款的合成核对须同时考虑适用条件和全部排除情形，不得因长度删去例外。" * 100),
    ("发票申报跨来源比较", "销项发票与增值税申报比较之前，须核对开票日期、税款所属期、不含税金额、红冲和申报调整。金额不同仅是待核对线索。"),
    ("兼营分类", "兼营业务的合成检查按不同经营类别保存销售额与取数来源，不能混用各项目口径。"),
    ("小额尾差", "计算尾差的合成情境使用精确十进制金额，空白单元格不作为零，单位不同先复核。"),
    ("视同交易线索", "赠送、内部领用等合成交易线索需要补充业务用途、实际流向和原始凭证，不能仅凭科目名称判断。"),
    ("作废票据核查", "作废状态与开票方向分别核对，合成测试保留被排除票据的来源而不重复汇总。"),
    ("余额借贷方向", "科目余额合成检查区分期初、期末、借方及贷方，不能按天拆分余额后比较。"),
    ("合同折让凭证", "合同折让的合成检查应核对交易双方的折让协议、实际付款和票据调整记录。"),
    ("项目甲修订公告", "自2026年7月1日起，合成项目甲按修订后的范围核对。此变更不影响原文件的项目乙。"),
]


def seed(db, originals_dir):
    from webapp import policy_graph as pg
    existing = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('users','audits')").fetchone()
    if existing:
        raise ValueError("合成验收语料禁止写入含账号或审计的业务数据库。")
    pg.migrate(db)
    if db.execute("SELECT count(*) FROM Policy").fetchone()[0]:
        raise ValueError("验收库已含政策，使用新文件或直接复用已建验收库。")
    originals_dir = Path(originals_dir)
    originals_dir.mkdir(parents=True, exist_ok=True)
    group = {key: [] for key in pg.IDS}
    for index, (title, text) in enumerate(TEXTS, 1):
        suffix = f"{index:02}"
        pid, cid, rid, mid = "P" + suffix, "CL" + suffix, "R" + suffix, "M" + suffix
        full_text = "【纯合成工程样本，非真实政策】\n" + text
        file = originals_dir / (pid + ".txt")
        with file.open("x", encoding="utf-8") as stream:
            stream.write(full_text)
        group["Policy"].append({"policy_id": pid, "title": "虚构测试：" + title, "document_no": "合成示例-" + suffix,
                                "issuer": "工程验收合成机关", "tax_type": "VAT", "effective_from": "2026-01-01",
                                "effective_to": "2026-12-31", "validity_status": "in_force", "region_code": "CN",
                                "source_url": "local-fixture://" + pid, "raw_file_path": str(file.resolve()),
                                "file_sha256": hashlib.sha256(file.read_bytes()).hexdigest()})
        group["Clause"].append({"clause_id": cid, "policy_id": pid, "clause_label": "第一条", "sort_index": 1,
                               "content": full_text, "source_location": pid + ".txt:L1", "extraction_status": "verified"})
        if index == 20:
            continue
        group["TaxRule"].append({"rule_id": rid, "rule_code": "DEMO-" + suffix, "name": title,
                                "rule_type": "risk_screen", "valid_from": "2026-01-01", "valid_to": "2026-12-31",
                                "condition_logic_json": {"ref": "C" + suffix}, "outcome_text": "纯合成检查线索，待核查。"})
        group["Metric"].append({"metric_id": mid, "metric_code": "demo-" + suffix, "name": title + "测试金额",
                               "value_type": "decimal", "unit": "元", "source_schema": "VATDeclaration",
                               "source_field": "declared_value", "period_basis": "tax_period",
                               "source_filter_json": {"form_type": "main", "form_name": "合成主表", "item_key": suffix, "column_scope": "本期数"}})
        group["Condition"].append({"condition_id": "C" + suffix, "rule_id": rid, "left_metric_id": mid,
                                  "operator": ">", "right_kind": "literal", "right_literal": "100000", "unit": "元",
                                  "condition_source": "internal_screening", "source_clause_id": None if index == 2 else cid})
    # Two clauses share the same source document; only 项目甲 is amended.
    group["Clause"][4]["content"] = "【纯合成】项目乙保持原核算口径，项目甲的修订不改变项目乙。"
    group["Clause"].append({"clause_id": "CL05A", "policy_id": "P05", "clause_label": "第二条", "sort_index": 2,
                           "content": "【纯合成】项目甲旧范围仅用于修订之前的检查。", "source_location": "P05.txt:项目甲", "extraction_status": "verified"})
    group["Policy"][4]["validity_status"] = "partially_modified"
    for suffix in ("04", "12"):
        group["Clause"].append({"clause_id": "CL" + suffix + "E", "policy_id": "P" + suffix,
                               "clause_label": "第二条", "sort_index": 2, "content": "【纯合成例外】特殊交易不适用上述一般处理，资料不足时必须复核。",
                               "source_location": "P" + suffix + ".txt:例外", "extraction_status": "verified"})
        group["Metric"].append({**group["Metric"][int(suffix) - 1], "metric_id": "MX" + suffix,
                               "metric_code": "demo-exception-" + suffix, "name": "例外测试金额"})
        group["Condition"].append({"condition_id": "CX" + suffix, "rule_id": "R" + suffix, "left_metric_id": "MX" + suffix,
                                  "operator": ">", "right_kind": "literal", "right_literal": "0", "unit": "元",
                                  "condition_source": "internal_screening", "source_clause_id": "CL" + suffix + "E"})
        group["Exception"].append({"exception_id": "X" + suffix, "rule_id": "R" + suffix, "name": "纯合成例外",
                                  "trigger_logic_json": {"ref": "CX" + suffix}, "effect": "needs_review", "source_clause_id": "CL" + suffix + "E"})
    group["Relationship"].append({"relationship_id": "AMEND-05", "from_type": "Clause", "from_id": "CL20", "relation_type": "AMENDS",
                                  "to_type": "Clause", "to_id": "CL05A", "evidence_clause_id": "CL20", "effective_from": "2026-07-01"})
    group["Policy"][5].update(effective_from="2018-01-01", effective_to="2024-12-31", validity_status="repealed")
    group["TaxRule"][5].update(valid_from="2018-01-01", valid_to="2024-12-31")
    group["Policy"][6]["region_code"] = "CN-33"
    group["TaxRule"][6]["applicable_region"] = "CN-33"
    group["Policy"][7].update(effective_from=None, validity_status="unverified")
    # Cross-material comparison deliberately starts without a comparability confirmation.
    group["Metric"].append({**group["Metric"][12], "metric_id": "MI13", "metric_code": "demo-invoice-13",
                           "name": "销项发票测试金额", "source_schema": "Invoice", "source_field": "amount_excluding_tax",
                           "period_basis": "issue_date", "source_filter_json": {"direction": "output", "red_letter_status": "蓝字", "void_status": "正常"}})
    group["Condition"][12].update(right_kind="metric", right_literal=None, right_metric_id="MI13", operator="!=")
    for policy in group["Policy"]:
        related = [c for c in group["Clause"] if c["policy_id"] == policy["policy_id"]]
        full_source = "\n\n".join(c["content"] for c in related)
        file = Path(policy["raw_file_path"])
        file.write_text(full_source, encoding="utf-8")
        policy["file_sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
        for clause in related:
            clause["source_location"] = file.name + ":L" + str(full_source[:full_source.index(clause["content"])].count("\n") + 1)
    pg.import_bundle(db, group)
    # This is the isolated fixture oracle, not human approval or a business-source promotion.
    db.execute("UPDATE TaxRule SET review_status='approved',reviewed_by='synthetic-fixture-oracle',reviewed_at='2026-10-15T00:00:00Z'")
    db.execute("UPDATE Relationship SET review_status='approved'")
    metadata = {"data_scope": "synthetic", "policy_count": 20, "source": "user-authorized fabricated engineering cases",
                "legal_review": False, "fixture_fingerprint": digest(group)}
    db.execute("CREATE TABLE kg_fixture_identity (metadata TEXT NOT NULL)")
    db.execute("INSERT INTO kg_fixture_identity VALUES (?)", (canonical(metadata),))
    return metadata
