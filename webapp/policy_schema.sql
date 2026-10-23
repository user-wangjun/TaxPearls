-- 税海拾珠：第一版税务知识图谱 SQLite Schema
-- 仅建立先前商定的8种Schema：Policy, Clause, TaxRule, Entity, Metric, Condition, Exception, Relationship
-- 注意：SQLite的Relationship采用动态节点类型，跨表的目标ID应由业务代码检验。
PRAGMA foreign_keys = ON;

-- 1. 政策文件：同一政策的历史原文必须长期保留。
CREATE TABLE IF NOT EXISTS Policy (
  policy_id         TEXT PRIMARY KEY,
  title             TEXT NOT NULL,
  document_no       TEXT,
  issuer            TEXT NOT NULL,
  tax_type          TEXT NOT NULL DEFAULT 'VAT',
  publish_date      TEXT,                 -- ISO-8601 YYYY-MM-DD
  effective_from    TEXT,                 -- 政策起始适用日期，未知时NULL
  effective_to      TEXT,                 -- 政策终止适用日期，未知时NULL；不要把NULL直接视为永远有效
  validity_status   TEXT NOT NULL DEFAULT 'unverified'
                    CHECK(validity_status IN ('unverified','in_force','partially_modified','repealed','expired')),
  region_code       TEXT NOT NULL DEFAULT 'CN',
  source_url        TEXT NOT NULL,
  raw_file_path     TEXT,                 -- 原始文件保存位置
  file_sha256       TEXT,                 -- 原始文件哈希，用于去重和变更识别
  fetched_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 2. 政策条款：保存原文与位置、条款层级和独立的有效期。
CREATE TABLE IF NOT EXISTS Clause (
  clause_id         TEXT PRIMARY KEY,
  policy_id         TEXT NOT NULL REFERENCES Policy(policy_id),
  parent_clause_id  TEXT REFERENCES Clause(clause_id),
  clause_label      TEXT NOT NULL,        -- 示例：第三条第二款
  sort_index        INTEGER NOT NULL,
  content           TEXT NOT NULL,
  source_location   TEXT,                 -- 原文页码/段落/HTML位置
  effective_from    TEXT,
  effective_to      TEXT,
  extraction_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(extraction_status IN ('pending','parsed','needs_review','verified')),
  UNIQUE(policy_id, clause_label)
);

-- 3. 规则节点：政策规定和内部风险筛查都属于TaxRule，但明确区分来源。
CREATE TABLE IF NOT EXISTS TaxRule (
  rule_id              TEXT PRIMARY KEY,
  name                 TEXT NOT NULL,
  tax_type             TEXT NOT NULL DEFAULT 'VAT',
  rule_type            TEXT NOT NULL CHECK(rule_type IN ('policy_rule','risk_screen')),
  applicable_region    TEXT NOT NULL DEFAULT 'CN',
  valid_from           TEXT,
  valid_to             TEXT,
  condition_logic_json TEXT NOT NULL DEFAULT '{}'
                       CHECK(json_valid(condition_logic_json)),
  outcome_text         TEXT NOT NULL,
  review_status        TEXT NOT NULL DEFAULT 'candidate'
                       CHECK(review_status IN ('candidate','approved','rejected','retired')),
  reviewed_by          TEXT,
  reviewed_at          TEXT,
  notes                TEXT,
  created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 4. 业务概念实体：如小规模纳税人、某行业、某地区；不放实际客户公司或其单张发票。
CREATE TABLE IF NOT EXISTS Entity (
  entity_id       TEXT PRIMARY KEY,
  entity_type     TEXT NOT NULL,
  name            TEXT NOT NULL,
  standard_code   TEXT,
  description     TEXT,
  UNIQUE(entity_type, name)
);

-- 5. 指标字典：负责把图谱规则中的指标映射到企业材料的标准Schema。
CREATE TABLE IF NOT EXISTS Metric (
  metric_id        TEXT PRIMARY KEY,
  metric_code      TEXT NOT NULL UNIQUE,
  name             TEXT NOT NULL,
  value_type       TEXT NOT NULL CHECK(value_type IN ('decimal','text','boolean','date')),
  unit             TEXT,
  source_schema    TEXT,                  -- 例如 Invoice / VATDeclaration / AccountBalanceRow / Company
  source_field     TEXT,                  -- 例如 amount_excluding_tax / declared_value
  source_filter_json TEXT NOT NULL DEFAULT '{}'
                     CHECK(json_valid(source_filter_json)),
  aggregation      TEXT NOT NULL DEFAULT 'direct'
                   CHECK(aggregation IN ('direct','sum','count','derived')),
  calculation_code TEXT,                  -- derived指标由程序实现预审核的计算函数，不运行AI任意代码
  period_basis     TEXT,                  -- 例如 invoice_date / tax_period / accounting_period
  definition       TEXT
);

-- 6. 基础判断条件：左右可以是固定值或另一个指标；组合逻辑由TaxRule.condition_logic_json表达。
CREATE TABLE IF NOT EXISTS Condition (
  condition_id       TEXT PRIMARY KEY,
  rule_id            TEXT NOT NULL REFERENCES TaxRule(rule_id),
  left_metric_id     TEXT NOT NULL REFERENCES Metric(metric_id),
  operator           TEXT NOT NULL
                     CHECK(operator IN ('=','!=','<','<=','>','>=','IN','NOT IN')),
  right_kind         TEXT NOT NULL CHECK(right_kind IN ('literal','metric')),
  right_literal      TEXT,                -- 小数存十进制字符串以避免浮点精度问题
  right_metric_id    TEXT REFERENCES Metric(metric_id),
  unit               TEXT,
  condition_source   TEXT NOT NULL DEFAULT 'pending'
                     CHECK(condition_source IN ('pending','statutory','internal_screening')),
  source_clause_id   TEXT REFERENCES Clause(clause_id),
  notes              TEXT,
  CHECK(
    (right_kind='literal' AND right_literal IS NOT NULL AND right_metric_id IS NULL)
    OR
    (right_kind='metric' AND right_metric_id IS NOT NULL AND right_literal IS NULL)
  )
);

-- 7. 例外：例外可以由一条或多条Condition组成，TRUE时执行排除/改变结果/转人工。
CREATE TABLE IF NOT EXISTS Exception (
  exception_id       TEXT PRIMARY KEY,
  rule_id            TEXT NOT NULL REFERENCES TaxRule(rule_id),
  name               TEXT NOT NULL,
  trigger_logic_json TEXT NOT NULL CHECK(json_valid(trigger_logic_json)),
  effect             TEXT NOT NULL CHECK(effect IN ('exclude','adjust_outcome','needs_review')),
  source_clause_id   TEXT REFERENCES Clause(clause_id),
  description        TEXT
);

-- 8. 知识图谱边：连接“政策/条款/规则/实体/指标/条件/例外”。
CREATE TABLE IF NOT EXISTS Relationship (
  relationship_id  TEXT PRIMARY KEY,
  from_type        TEXT NOT NULL CHECK(from_type IN ('Policy','Clause','TaxRule','Entity','Metric','Condition','Exception')),
  from_id          TEXT NOT NULL,
  relation_type    TEXT NOT NULL,        -- 示例：HAS_CLAUSE / DERIVES_RULE / APPLIES_TO / USES_METRIC / HAS_CONDITION / HAS_EXCEPTION / AMENDS / REPEALS
  to_type          TEXT NOT NULL CHECK(to_type IN ('Policy','Clause','TaxRule','Entity','Metric','Condition','Exception')),
  to_id            TEXT NOT NULL,
  evidence_clause_id TEXT REFERENCES Clause(clause_id),
  effective_from   TEXT,
  effective_to     TEXT,
  review_status    TEXT NOT NULL DEFAULT 'candidate'
                   CHECK(review_status IN ('candidate','approved','rejected')),
  UNIQUE(from_type, from_id, relation_type, to_type, to_id)
);

CREATE INDEX IF NOT EXISTS idx_clause_policy ON Clause(policy_id, sort_index);
CREATE INDEX IF NOT EXISTS idx_rule_type_status ON TaxRule(tax_type, rule_type, review_status, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_condition_rule ON Condition(rule_id);
CREATE INDEX IF NOT EXISTS idx_exception_rule ON Exception(rule_id);
CREATE INDEX IF NOT EXISTS idx_relation_source ON Relationship(from_type, from_id, relation_type);
CREATE INDEX IF NOT EXISTS idx_relation_target ON Relationship(to_type, to_id, relation_type);
CREATE INDEX IF NOT EXISTS idx_policy_region_time ON Policy(region_code, effective_from, effective_to);

-- Local human review evidence is separate from the eight transferable graph node types.
-- A reviewed coverage window is not a policy's legal expiry date.
CREATE TABLE IF NOT EXISTS PolicyReview (
  review_id TEXT PRIMARY KEY,
  policy_id TEXT NOT NULL REFERENCES Policy(policy_id),
  file_sha256 TEXT NOT NULL,
  clause_snapshots_json TEXT NOT NULL CHECK(json_valid(clause_snapshots_json)),
  end_kind TEXT NOT NULL CHECK(end_kind IN ('unknown','fixed','unfixed')),
  coverage_start TEXT,
  coverage_end TEXT,
  evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
  reviewed_by TEXT NOT NULL,
  reviewed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_policy_review_coverage ON PolicyReview(policy_id, coverage_start, coverage_end);

-- 程序规则（不是SQL自动执行）：
-- A. 所有AI抽取的规则/关系先candidate，经过来源/时间/条件/例外审验才approved。
-- B. 评估器必须支持 TRUE、FALSE、UNKNOWN，缺数据不等于FALSE。
-- C. condition_logic_json 支持嵌套AND/OR/NOT：
--    {"and":[{"ref":"C001"},{"or":[{"ref":"C002"},{"ref":"C003"}]}]}
-- D. Exception.trigger_logic_json引用同一TaxRule下的Condition ID，由程序校验。
-- E. 非法条款修订和部分废止，优先建立条款级关系并保留时间/证据，不只改Policy的总体状态。
-- F. Relationship中from_id和to_id的目标存在性、合法类型配对、approved证据，应用层校验。
-- G. 对企业数据的映射采用已有Invoice/VATDeclaration/AccountBalanceRow Schema；这些不属于政策图谱八张表。
