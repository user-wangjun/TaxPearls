"""核心数据模型。

设计要点：每个指标都携带 source（来源说明）与 detail（计算过程），
使「可举证」成为数据结构的固有属性，而非事后拼装。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class Company:
    """被审计企业基本信息。"""

    name: str
    taxpayer_id: str
    industry: str
    period: str


@dataclass
class Account:
    """科目余额表的一行。"""

    code: str
    name: str
    opening: Decimal | None
    debit: Decimal | None
    credit: Decimal | None
    closing: Decimal | None


@dataclass
class Metric:
    """标准化后的一个指标。

    source  : 取数来源（科目编码 / 申报表项目），用于在报告中举证
    detail  : 计算过程描述，例如 "6001 贷方 1,200,000 + 6051 贷方 80,000"
    """

    name: str
    value: Decimal | float
    source: str
    detail: str = ""


@dataclass
class RelatedSubject:
    key: str
    name: str
    kind: str
    taxpayer_id: str
    source: str


@dataclass
class RelatedRelation:
    key: str
    owner_key: str
    company_key: str
    kind: str
    start_on: str
    end_on: str
    reviewed: bool
    basis: str
    source: str


@dataclass
class RelatedTrade:
    key: str
    seller_key: str
    buyer_key: str
    traded_on: str
    amount: Decimal
    anomaly_basis: str
    reviewed: bool
    source: str


@dataclass
class RelatedGraph:
    subjects: list[RelatedSubject]
    relations: list[RelatedRelation]
    trades: list[RelatedTrade]


@dataclass
class Dataset:
    """一次审计的全部输入，标准化后的形态。"""

    company: Company
    accounts: list[Account]
    declarations: dict[str, Decimal | float]
    metrics: dict[str, Metric]
    related_graph: RelatedGraph | None = None
    # None denotes legacy snapshots, whose available evidence is described conservatively.
    sources: list[str] | None = None

    def values(self) -> dict[str, Decimal | float]:
        """供规则引擎求值用的扁平数值表。"""
        return {k: m.value for k, m in self.metrics.items()}

    def get(self, key: str) -> Decimal | float | None:
        m = self.metrics.get(key)
        return m.value if m else None

    def source_of(self, key: str) -> str:
        m = self.metrics.get(key)
        return m.source if m else "（未取到该指标）"

    def detail_of(self, key: str) -> str:
        m = self.metrics.get(key)
        return m.detail if m else ""


@dataclass
class Rule:
    """一条 YAML 声明式风险规则。"""

    id: str
    name: str
    category: str
    tax_type: str
    severity: str
    logic: dict[str, Any]
    evidence: list[str]
    legal_basis: list[str]
    suggestion: str
    description: str = ""
    source_file: str = ""
    inputs: dict[str, str] = field(default_factory=dict)
    scope: str = ""
    threshold_basis: str = ""
    references: list[str] = field(default_factory=list)
    version: str = "1.0"
    effective_from: str | None = None
    effective_to: str | None = None


@dataclass
class EvidenceItem:
    """证据卡上的一行数据。"""

    label: str
    value: str
    source: str
    emphasis: bool = False


@dataclass
class Finding:
    """一条规则的判定结果。

    status 取值：
        hit      -- 命中风险
        pass     -- 已执行，未发现异常
        skipped  -- 因证据、口径或适用范围等限制未能执行（不算通过，须明示实际原因）
    """

    rule: Rule
    status: str
    measured: float | None
    threshold_desc: str
    conclusion: str
    evidence: list[EvidenceItem] = field(default_factory=list)
    calculation: str = ""
    skip_reason: str = ""

    @property
    def hit(self) -> bool:
        return self.status == "hit"

    @property
    def executed(self) -> bool:
        return self.status != "skipped"

    @property
    def severity_rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1}.get(self.rule.severity, 0)


SEVERITY_LABEL = {"high": "高风险", "medium": "中风险", "low": "低风险"}
STATUS_LABEL = {"hit": "命中", "pass": "通过", "skipped": "未执行"}


# ---------------------------------------------------------------------------
# 高校实训（teaching-training）：以下数据类与 webapp/schema.py 中实训表一一对应，
# 仅承载数据，不做 ORM 映射。
# ---------------------------------------------------------------------------


@dataclass
class CollegeUser:
    """高校实训管理员（教师账号），对应表 college_user。"""

    id: str                        # 主键，UUID 文本
    username: str                  # 登录名，全局唯一
    password_hash: str             # 登录密码哈希（不存明文）
    display_name: str              # 姓名 / 显示名
    college: str                   # 所属院校（实训数据的隔离键）
    created_at: str                # 创建时间（ISO 8601 文本）
    department: str | None = None  # 所属院系（可空）
    email: str | None = None       # 联系邮箱（可空）
    phone: str | None = None       # 联系电话（可空）
    role: str = "teacher"          # 角色：teacher=教师 / admin=实训管理员
    active: bool = True            # 是否启用


@dataclass
class TrainingTask:
    """一次高校实训任务，对应表 training_task。"""

    id: str                        # 主键，UUID 文本
    college: str                   # 所属院校（与 college_user.college 同域）
    title: str                     # 任务标题
    dataset: dict[str, Any]        # 实训数据集（仿真材料/科目余额，落库为 dataset_json）
    created_by: str                # 创建教师，外键 → college_user.id
    created_at: str                # 创建时间（ISO 8601 文本）
    description: str = ""          # 任务说明
    starts_at: str | None = None   # 开放开始时间（可空）
    ends_at: str | None = None     # 截止时间（可空，不早于 starts_at）
    published: bool = False        # 是否发布：False=草稿 / True=已发布


@dataclass
class StudentInfo:
    """实训学生信息，对应表 student_info。"""

    id: str                        # 主键，UUID 文本
    college: str                   # 所属院校
    student_no: str                # 学号，同一院校内唯一
    name: str                      # 姓名
    password_hash: str             # 登录密码哈希（学生登录作答用）
    created_at: str                # 建档时间（ISO 8601 文本）
    class_name: str | None = None  # 班级（可空）
    email: str | None = None       # 邮箱（可空）
    active: bool = True            # 是否在读 / 启用


@dataclass
class StudentSubmit:
    """一次学生实训提交，对应表 student_submit；每个任务每名学生仅一条提交。"""

    id: str                        # 主键，UUID 文本
    task_id: str                   # 实训任务，外键 → training_task.id
    student_id: str                # 学生，外键 → student_info.id
    answers: dict[str, Any]        # 作答内容（识别的风险点/证据引用，落库为 answers_json）
    submitted_at: str              # 提交时间（ISO 8601 文本）
    status: str = "submitted"      # submitted=已提交 / scored=已评分 / reviewed=已复核


@dataclass
class ScoreResult:
    """一次提交的评分结果，对应表 score_result；与提交记录 1:1。"""

    id: str                                # 主键，UUID 文本
    submission_id: str                     # 提交记录，外键 → student_submit.id（1:1）
    total_score: float                     # 总得分（误报扣分后允许为负）
    detail: dict[str, Any]                 # 逐规则得分明细与标准答案比对，落库为 detail_json
    scored_at: str                         # 评分时间（ISO 8601 文本）
    missed_count: int = 0                  # 漏检项数量（应发现而未发现）
    false_positive_count: int = 0          # 误报项数量（报告了不存在的问题）
    scored_by: str | None = None           # 评分人，外键 → college_user.id；系统自动评分为空


@dataclass
class ScoreRule:
    """一条实训评分规则，对应表 score_rule。"""

    id: str                        # 主键，UUID 文本
    task_id: str                   # 所属实训任务，外键 → training_task.id
    name: str                      # 规则名称，同一任务内唯一
    created_at: str                # 创建时间（ISO 8601 文本）
    category: str = "omission"     # omission=漏检 / false_positive=误报 / evidence=证据复核
    weight: float = 1.0            # 分值权重
    config: dict[str, Any] = field(default_factory=dict)  # 规则参数（阈值等，落库为 config_json）
    enabled: bool = True           # 是否启用
