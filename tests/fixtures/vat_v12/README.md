# v1.2 用户提供的合成验收素材

来源：税海拾珠_v1.2_完整文档与40项本地模拟测试数据包.zip，SHA-256 34e7dd48e75042017db9b35db8e60ff97557c188030df76a2d46e9a93ec404cb。

原始 JSON/CSV 保持不变；只供隔离测试，approved_for_fixture_only 不等于业务 approved。编号以 V12-Kxx 与既有 K01–K13 区分。图谱字段需由测试适配器转换；目标命令使用当前 src.kg_retrieval.cli，不创建第二套 app.cli。

适配前置条件：K05/K06/K07/K14 将原 expected.audit_date 显式移入实际查询；词面和语义测试补上实际查询句。对有明确生效日的虚构政策，工程 oracle 明确限定本套模拟核验覆盖到 2026-12-31；这不是从空 effective_to 推定永久有效，也不修改原始 JSON。K09 保持生效日及覆盖未知。例外增加独立未知指标，以便在主事实已知时测试风险仍待核查。原六条检查卡中仅 R01 有本轮可执行模型，另外五条不伪造成已批准。K15 的 needs_review 是任务风险状态，不是新增 TaxRule.review_status 枚举。
