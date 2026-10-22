"""Additive SQLite schema installation; invoked only by explicit Store creation."""


def initialize(store) -> None:
    from webapp import classroom, members, email_auth, oauth, mistake_book
    with store.connect() as db:
        # WAL 一次设置、持久化于库文件
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
                role TEXT NOT NULL, org_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                expires_at TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL,
                taxpayer_id TEXT NOT NULL, accountant_id TEXT REFERENCES users(id),
                created_at TEXT NOT NULL, UNIQUE(org_id, taxpayer_id)
            );
            CREATE TABLE IF NOT EXISTS audits (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL,
                client_id TEXT REFERENCES clients(id), created_by TEXT NOT NULL REFERENCES users(id),
                company_name TEXT NOT NULL, taxpayer_id TEXT NOT NULL,
                industry TEXT NOT NULL, period TEXT NOT NULL,
                dataset_json TEXT NOT NULL, findings_json TEXT NOT NULL,
                summary_json TEXT NOT NULL, audited_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audits_org ON audits(org_id, audited_at DESC);
            CREATE TABLE IF NOT EXISTS audit_report_versions (
                audit_id TEXT NOT NULL REFERENCES audits(id), version INTEGER NOT NULL,
                html TEXT NOT NULL, html_sha256 TEXT NOT NULL,
                manifest_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
                content_sha256 TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL, pdf_bytes BLOB, pdf_sha256 TEXT, pdf_created_at TEXT,
                PRIMARY KEY(audit_id,version), UNIQUE(audit_id,content_sha256)
            );
            CREATE TABLE IF NOT EXISTS org_reports (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL,
                created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
                html TEXT NOT NULL, html_sha256 TEXT NOT NULL,
                pdf_bytes BLOB, pdf_sha256 TEXT, pdf_created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_org_reports_org ON org_reports(org_id,created_at DESC);
            CREATE TABLE IF NOT EXISTS report_protections (
                id TEXT PRIMARY KEY,org_id TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('audit','org')),
                audit_id TEXT,version INTEGER,org_report_id TEXT REFERENCES org_reports(id),
                FOREIGN KEY(audit_id,version) REFERENCES audit_report_versions(audit_id,version),
                CHECK((kind='audit' AND audit_id IS NOT NULL AND version IS NOT NULL AND org_report_id IS NULL)
                   OR (kind='org' AND org_report_id IS NOT NULL AND audit_id IS NULL AND version IS NULL))
            );
            CREATE TABLE IF NOT EXISTS assignments (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, title TEXT NOT NULL,
                audit_id TEXT NOT NULL REFERENCES audits(id), created_by TEXT NOT NULL REFERENCES users(id),
                target_student_id TEXT REFERENCES users(id), weights_json TEXT NOT NULL,
                false_positive_penalty REAL NOT NULL, published INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generated_exercises (
                audit_id TEXT PRIMARY KEY REFERENCES audits(id),org_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,metadata_sha256 TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_assignments_teacher_audit
                ON assignments(org_id,created_by,audit_id);
            CREATE TABLE IF NOT EXISTS submissions (
                id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES assignments(id),
                student_id TEXT NOT NULL REFERENCES users(id), answers_json TEXT NOT NULL,
                score REAL NOT NULL, details_json TEXT NOT NULL, submitted_at TEXT NOT NULL,
                adjusted_score REAL, feedback TEXT, reviewed_by TEXT REFERENCES users(id),
                UNIQUE(assignment_id, student_id)
            );
            CREATE TABLE IF NOT EXISTS rule_state (
                rule_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                updated_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rule_overrides (
                rule_id TEXT PRIMARY KEY, version TEXT NOT NULL,
                logic_json TEXT NOT NULL, threshold_basis TEXT NOT NULL,
                updated_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rule_version_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT NOT NULL, version TEXT NOT NULL,
                effective_from TEXT, effective_to TEXT,
                logic_json TEXT NOT NULL, threshold_basis TEXT NOT NULL,
                rule_json TEXT,
                updated_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL,
                UNIQUE(rule_id, version),
                CHECK(effective_from IS NOT NULL OR effective_to IS NULL)
            );
            CREATE INDEX IF NOT EXISTS idx_rule_version_period
                ON rule_version_history(rule_id, effective_from, effective_to);
            CREATE TABLE IF NOT EXISTS org_settings (
                org_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '税海拾珠',
                report_title TEXT NOT NULL DEFAULT '税务风险审计报告',
                footer_text TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                logo_mime TEXT, logo_bytes BLOB, logo_updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT,
                org_id TEXT NOT NULL, action TEXT NOT NULL,
                target_type TEXT NOT NULL, target_id TEXT NOT NULL,
                detail TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_service_keys (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
                model TEXT NOT NULL, secret_value TEXT NOT NULL,
                disable_thinking INTEGER NOT NULL, revision TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_key_exclusions (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_preferences (
                user_id TEXT PRIMARY KEY REFERENCES users(id),
                audit_completed INTEGER NOT NULL DEFAULT 0,
                high_risk INTEGER NOT NULL DEFAULT 0,
                email_enabled INTEGER NOT NULL DEFAULT 0,
                updated_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                org_id TEXT NOT NULL, audit_id TEXT NOT NULL REFERENCES audits(id),
                event TEXT NOT NULL, summary_json TEXT NOT NULL,
                created_at TEXT NOT NULL, read_at TEXT,
                UNIQUE(user_id,audit_id,event)
            );
            CREATE INDEX IF NOT EXISTS idx_notifications_user
                ON notifications(user_id,org_id,created_at);
            CREATE TABLE IF NOT EXISTS notification_deliveries (
                notification_id TEXT PRIMARY KEY REFERENCES notifications(id),
                recipient_email TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, claim_token TEXT,
                provider_id TEXT, error_code TEXT, claimed_at TEXT, payload_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notification_deliveries_status
                ON notification_deliveries(status,created_at,notification_id);
            CREATE TABLE IF NOT EXISTS finding_interpretations (
                audit_id TEXT NOT NULL REFERENCES audits(id),
                rule_id TEXT NOT NULL, evidence_hash TEXT NOT NULL,
                model TEXT NOT NULL, result_json TEXT NOT NULL,
                created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                PRIMARY KEY(audit_id, rule_id, evidence_hash)
            );
            CREATE TABLE IF NOT EXISTS audit_narratives (
                audit_id TEXT NOT NULL REFERENCES audits(id),
                evidence_hash TEXT NOT NULL, model TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                PRIMARY KEY(audit_id, evidence_hash)
            );
        """)
        version_columns = {row["name"] for row in db.execute("PRAGMA table_info(rule_version_history)")}
        if "rule_json" not in version_columns:
            db.execute("ALTER TABLE rule_version_history ADD COLUMN rule_json TEXT")
        delivery_columns = {row["name"] for row in db.execute("PRAGMA table_info(notification_deliveries)")}
        if "payload_json" not in delivery_columns:
            db.execute("ALTER TABLE notification_deliveries ADD COLUMN payload_json TEXT")
        from webapp.notifications import migrate_deliveries
        migrate_deliveries(db)
        # Preserve pre-B12 overrides as undated legacy versions. Existing
        # audits already contain immutable Finding snapshots.
        db.execute("""INSERT OR IGNORE INTO rule_version_history
                   (rule_id,version,effective_from,effective_to,logic_json,
                    threshold_basis,updated_by,updated_at)
                   SELECT rule_id,version,NULL,NULL,logic_json,
                          threshold_basis,updated_by,updated_at FROM rule_overrides""")
        # Existing P1 databases predate configurable organization logos.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(org_settings)")}
        for name, sql_type in (
            ("logo_mime", "TEXT"), ("logo_bytes", "BLOB"), ("logo_updated_at", "TEXT"),
            ("org_type", "TEXT CHECK (org_type IN ('enterprise','university'))")
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE org_settings ADD COLUMN {name} {sql_type}")
        # 注册与开户（FR-G10/G11）：用户绑定邮箱。email 可空但唯一（部分唯一索引）。
        # 因「用户自设密码」，password_hash 保持 NOT NULL——仅需加列，保留已有表。
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "email" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN email TEXT")
        db.execute("""CREATE TABLE IF NOT EXISTS email_tokens (
                token_hash TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                purpose TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                session_key TEXT,
                code_hash TEXT,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL
            )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_email_tokens_email ON email_tokens(email, purpose)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_audits_org_period ON audits(org_id,taxpayer_id,period,audited_at DESC)")
        email_auth.migrate(db)
        oauth.migrate(db)
        from webapp import channel_bindings
        channel_bindings.migrate(db)
        db.execute("""CREATE TABLE IF NOT EXISTS invite_codes (
                token_hash TEXT PRIMARY KEY,
                org_name TEXT NOT NULL,
                seats INTEGER NOT NULL,
                bound_email TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                redeemed_by TEXT REFERENCES users(id),
                redeemed_at TEXT,
                revoked INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                CHECK (redeemed_by IS NULL OR redeemed_at IS NOT NULL)
            )""")
        # Old invitations have no reliable type; keep NULL instead of guessing.
        if "org_type" not in {row["name"] for row in db.execute("PRAGMA table_info(invite_codes)")}:
            db.execute("ALTER TABLE invite_codes ADD COLUMN org_type TEXT "
                       "CHECK (org_type IN ('enterprise','university'))")
        db.execute("""CREATE TABLE IF NOT EXISTS org_quota (
                org_id TEXT PRIMARY KEY,
                seats INTEGER NOT NULL,
                updated_by TEXT REFERENCES users(id),
                updated_at TEXT NOT NULL
            )""")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email "
                   "ON users(email) WHERE email IS NOT NULL AND email <> ''")
        count = db.execute("SELECT COUNT(*) FROM users WHERE role='platform_admin'").fetchone()[0]
        if count > 1:
            raise RuntimeError("现有数据库含多个平台管理员，需人工确认归并后才能安装唯一约束；未自动删除账号。")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_single_platform_admin "
                   "ON users(role) WHERE role='platform_admin'")
        classroom.migrate(db)
        from webapp import ai_coach
        ai_coach.migrate(db)
        from webapp import training_tasks
        training_tasks.migrate(db)
        from webapp import training_submissions
        training_submissions.migrate(db)
        mistake_book.migrate(db)
        members.migrate(db)
        from webapp import material_batches
        material_batches.migrate(db)
        from src import core_mapping
        core_mapping.migrate(db)
        from webapp import core_inputs
        core_inputs.migrate(db)
        # ------------------------------------------------------------------
        # 高校实训（teaching-training）：实训管理员、任务、学生、提交与评分。
        # 仅新增表，不改动既有结构；与 src/models.py 中对应 dataclass 一一对应。
        # 建表顺序即外键依赖顺序：college_user → training_task → student_info
        #   → student_submit → score_result → score_rule。
        # ------------------------------------------------------------------
        db.executescript("""
            CREATE TABLE IF NOT EXISTS college_user (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                username TEXT NOT NULL UNIQUE,            -- 登录名，全局唯一
                password_hash TEXT NOT NULL,              -- 登录密码哈希（禁存明文）
                display_name TEXT NOT NULL,               -- 姓名 / 显示名
                college TEXT NOT NULL,                    -- 所属院校（实训数据的隔离键）
                department TEXT,                          -- 所属院系（可空）
                email TEXT,                               -- 联系邮箱（可空）
                phone TEXT,                               -- 联系电话（可空）
                role TEXT NOT NULL DEFAULT 'teacher',     -- 角色：teacher=教师 / admin=实训管理员
                active INTEGER NOT NULL DEFAULT 1,        -- 是否启用：1=启用 0=停用
                created_at TEXT NOT NULL                  -- 创建时间（ISO 8601 文本）
            );
            CREATE INDEX IF NOT EXISTS idx_college_user_college
                ON college_user(college, active);
            CREATE TABLE IF NOT EXISTS training_task (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                college TEXT NOT NULL,                    -- 所属院校（与 college_user.college 同域）
                title TEXT NOT NULL,                      -- 任务标题
                description TEXT NOT NULL DEFAULT '',     -- 任务说明
                dataset_json TEXT NOT NULL,               -- 实训数据集（仿真材料/科目余额等，JSON 文本）
                created_by TEXT NOT NULL REFERENCES college_user(id),  -- 创建教师（外键 → college_user.id）
                starts_at TEXT,                           -- 开放开始时间（可空，ISO 文本）
                ends_at TEXT,                             -- 截止时间（可空，ISO 文本，不早于 starts_at）
                published INTEGER NOT NULL DEFAULT 0,     -- 是否发布：0=草稿 1=已发布
                created_at TEXT NOT NULL,                 -- 创建时间（ISO 8601 文本）
                CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at >= starts_at)
            );
            CREATE INDEX IF NOT EXISTS idx_training_task_college
                ON training_task(college, created_at DESC);
            CREATE TABLE IF NOT EXISTS student_info (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                college TEXT NOT NULL,                    -- 所属院校（租户键）
                student_no TEXT NOT NULL,                 -- 学号（同一院校内唯一）
                name TEXT NOT NULL,                       -- 姓名
                class_name TEXT,                          -- 班级（可空）
                password_hash TEXT NOT NULL,              -- 登录密码哈希（学生登录作答用）
                email TEXT,                               -- 邮箱（可空）
                active INTEGER NOT NULL DEFAULT 1,        -- 是否在读/启用：1=是 0=否
                created_at TEXT NOT NULL,                 -- 建档时间（ISO 8601 文本）
                UNIQUE(college, student_no)               -- 同一院校内学号唯一
            );
            CREATE INDEX IF NOT EXISTS idx_student_info_college
                ON student_info(college, active);
            CREATE TABLE IF NOT EXISTS student_submit (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                task_id TEXT NOT NULL REFERENCES training_task(id),    -- 实训任务（外键 → training_task.id）
                student_id TEXT NOT NULL REFERENCES student_info(id),  -- 学生（外键 → student_info.id）
                answers_json TEXT NOT NULL,               -- 作答内容（识别的风险点/证据引用，JSON 文本）
                status TEXT NOT NULL DEFAULT 'submitted'
                    CHECK (status IN ('submitted','scored','reviewed')),  -- 状态：已提交/已评分/已复核
                submitted_at TEXT NOT NULL,               -- 提交时间（ISO 8601 文本）
                UNIQUE(task_id, student_id)               -- 每个任务每名学生一条提交（沿用 submissions 惯例）
            );
            CREATE INDEX IF NOT EXISTS idx_student_submit_task
                ON student_submit(task_id, submitted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_student_submit_student
                ON student_submit(student_id, submitted_at DESC);
            CREATE TABLE IF NOT EXISTS score_result (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                submission_id TEXT NOT NULL UNIQUE REFERENCES student_submit(id),  -- 提交记录（外键，1:1）
                total_score REAL NOT NULL DEFAULT 0,      -- 总得分（误报扣分后允许为负）
                missed_count INTEGER NOT NULL DEFAULT 0,          -- 漏检项数量（应发现而未发现）
                false_positive_count INTEGER NOT NULL DEFAULT 0,  -- 误报项数量（报告了不存在的问题）
                detail_json TEXT NOT NULL,                -- 得分明细（逐规则得分/标准答案比对，JSON 文本）
                scored_by TEXT REFERENCES college_user(id),  -- 评分人（外键 → college_user.id；系统自动评分为空）
                scored_at TEXT NOT NULL                   -- 评分时间（ISO 8601 文本）
            );
            CREATE TABLE IF NOT EXISTS score_rule (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                task_id TEXT NOT NULL REFERENCES training_task(id),  -- 所属实训任务（外键 → training_task.id）
                name TEXT NOT NULL,                       -- 规则名称（同一任务内唯一）
                category TEXT NOT NULL DEFAULT 'omission'
                    CHECK (category IN ('omission','false_positive','evidence')),  -- 类别：漏检/误报/证据复核
                weight REAL NOT NULL DEFAULT 1.0,         -- 分值权重
                config_json TEXT NOT NULL DEFAULT '{}',   -- 规则参数（阈值、适用范围等，JSON 文本）
                enabled INTEGER NOT NULL DEFAULT 1,       -- 是否启用：1=启用 0=停用
                created_at TEXT NOT NULL,                 -- 创建时间（ISO 8601 文本）
                UNIQUE(task_id, name)                     -- 同一任务内规则名唯一
            );
            CREATE INDEX IF NOT EXISTS idx_score_rule_task
                ON score_rule(task_id, enabled);
        """)

        # ------------------------------------------------------------------
        # 考证刷题内容层（FR-K01～K08，需求见 docs/07-高校考证刷题线需求.md）：
        # 证书目录、考试日期、考纲知识点、知识点关联与标注、学生目标。
        # 内容表为全局共享（证书/考纲不按院校隔离）；个人数据经 student_info 外键归属。
        # ------------------------------------------------------------------
        db.executescript("""
            CREATE TABLE IF NOT EXISTS certificate (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                code TEXT NOT NULL UNIQUE,                -- 证书编码，如 'cjkj'（初级会计职称）
                name TEXT NOT NULL,                       -- 证书名称
                description TEXT NOT NULL DEFAULT '',     -- 说明（报考条件、考试形式等）
                subjects_json TEXT NOT NULL DEFAULT '[]', -- 科目清单（JSON 数组文本）
                source_ref TEXT NOT NULL DEFAULT '',      -- 内容依据说明（官方目录/公告引用）
                active INTEGER NOT NULL DEFAULT 1,        -- 是否启用：1=启用 0=下架
                created_at TEXT NOT NULL,                 -- 创建时间（ISO 8601 文本）
                updated_at TEXT NOT NULL                  -- 最近更新时间
            );
            CREATE TABLE IF NOT EXISTS exam_date (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                certificate_id TEXT NOT NULL REFERENCES certificate(id),  -- 证书（外键）
                round_label TEXT NOT NULL DEFAULT '',     -- 年度/批次标识，如 '2027年第一批'
                date_type TEXT NOT NULL
                    CHECK (date_type IN ('official','expected','personal')),  -- 官方/预计/个人计划
                exam_date TEXT NOT NULL,                  -- 考试日期（ISO 日期文本）
                student_id TEXT REFERENCES student_info(id),  -- 学生（外键；date_type=personal 时必填）
                note TEXT NOT NULL DEFAULT '',            -- 备注
                created_by TEXT REFERENCES college_user(id),  -- 录入人（外键；内置数据为空）
                created_at TEXT NOT NULL,                 -- 创建时间
                updated_at TEXT NOT NULL,                 -- 最近更新时间
                CHECK (date_type != 'personal' OR student_id IS NOT NULL)
            );
            CREATE INDEX IF NOT EXISTS idx_exam_date_cert
                ON exam_date(certificate_id, date_type, exam_date);
            CREATE INDEX IF NOT EXISTS idx_exam_date_student
                ON exam_date(student_id);
            CREATE TABLE IF NOT EXISTS knowledge_point (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                certificate_id TEXT NOT NULL REFERENCES certificate(id),  -- 证书（外键）
                code TEXT NOT NULL,                       -- 知识点编码，证书内唯一
                name TEXT NOT NULL,                       -- 知识点名称
                subject TEXT NOT NULL DEFAULT '',         -- 所属科目
                parent_id TEXT REFERENCES knowledge_point(id),  -- 上级知识点（外键，层级树）
                description TEXT NOT NULL DEFAULT '',     -- 说明
                source_ref TEXT NOT NULL DEFAULT '',      -- 考纲依据引用（章节/条目号）
                outline_version TEXT NOT NULL DEFAULT '', -- 考纲版本
                active INTEGER NOT NULL DEFAULT 1,        -- 是否启用
                created_at TEXT NOT NULL,                 -- 创建时间
                updated_at TEXT NOT NULL,                 -- 最近更新时间
                UNIQUE(certificate_id, code)
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_point_cert
                ON knowledge_point(certificate_id, subject, active);
            CREATE TABLE IF NOT EXISTS knowledge_point_link (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                knowledge_point_id TEXT NOT NULL REFERENCES knowledge_point(id),  -- 知识点（外键）
                target_type TEXT NOT NULL
                    CHECK (target_type IN ('rule','task','question')),  -- 规则/实训任务/题目
                target_id TEXT NOT NULL,                  -- 目标对象 ID
                active INTEGER NOT NULL DEFAULT 1,        -- 是否启用：0=停用（出题不再选用，治理题源）
                created_by TEXT REFERENCES college_user(id),  -- 建立人（外键）
                created_at TEXT NOT NULL,                 -- 创建时间
                UNIQUE(knowledge_point_id, target_type, target_id)
            );
            CREATE INDEX IF NOT EXISTS idx_kp_link_target
                ON knowledge_point_link(target_type, target_id);
            CREATE TABLE IF NOT EXISTS knowledge_point_mark (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                knowledge_point_id TEXT NOT NULL REFERENCES knowledge_point(id),  -- 知识点（外键）
                mark_type TEXT NOT NULL
                    CHECK (mark_type IN ('high_freq','risk_context','error_prone')),  -- 考证高频/企业风险情境/学生易错
                level TEXT NOT NULL DEFAULT 'high' CHECK (level IN ('high','medium','low')),  -- 程度
                basis_ref TEXT NOT NULL DEFAULT '',       -- 标注依据（考纲章节/统计口径说明；高频类必填由服务层校验）
                basis_version TEXT NOT NULL DEFAULT '',   -- 依据版本
                created_by TEXT REFERENCES college_user(id),  -- 标注人（外键）
                created_at TEXT NOT NULL,                 -- 创建时间
                UNIQUE(knowledge_point_id, mark_type, basis_ref)
            );
            CREATE INDEX IF NOT EXISTS idx_kp_mark_kp
                ON knowledge_point_mark(knowledge_point_id, mark_type);
            CREATE TABLE IF NOT EXISTS knowledge_point_relation (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                from_kp_id TEXT NOT NULL REFERENCES knowledge_point(id),  -- 关系主体知识点（外键）
                to_kp_id TEXT NOT NULL REFERENCES knowledge_point(id),    -- 关系客体知识点（外键）
                relation_type TEXT NOT NULL
                    CHECK (relation_type IN ('prerequisite','concept','confusable')),
                                                          -- 前置知识/概念关联/易混淆
                basis_ref TEXT NOT NULL DEFAULT '',       -- 关系依据（考纲章节/教材说明；服务层强制必填）
                basis_version TEXT NOT NULL DEFAULT '',   -- 依据版本
                created_by TEXT REFERENCES college_user(id),  -- 建立人（外键）
                created_at TEXT NOT NULL,                 -- 建立时间
                UNIQUE(from_kp_id, to_kp_id, relation_type),
                CHECK (from_kp_id <> to_kp_id)            -- 不允许自环
            );
            CREATE INDEX IF NOT EXISTS idx_kp_relation_from
                ON knowledge_point_relation(from_kp_id, relation_type);
            CREATE INDEX IF NOT EXISTS idx_kp_relation_to
                ON knowledge_point_relation(to_kp_id, relation_type);
            CREATE TABLE IF NOT EXISTS training_tutor_messages (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                student_id TEXT NOT NULL REFERENCES student_info(id),  -- 学生（外键；本人隔离）
                certificate_id TEXT REFERENCES certificate(id),  -- 证书上下文（外键）
                knowledge_point_id TEXT REFERENCES knowledge_point(id),  -- 知识点入口（可空）
                attempt_id TEXT REFERENCES training_self_practice_attempts(id),  -- 题目入口（可空，须已判分）
                question TEXT NOT NULL,                   -- 学生提问
                answer TEXT NOT NULL,                     -- 助手回答（仅解读；标准答案以作答数据为准）
                citations_json TEXT NOT NULL DEFAULT '[]',  -- 引用节点 id（JSON 数组）
                grounding_json TEXT NOT NULL DEFAULT '{}',  -- 组装上下文快照（证据节点）
                model TEXT NOT NULL DEFAULT '',           -- 模型名（降级为空）
                degraded TEXT NOT NULL DEFAULT '',        -- 降级说明（空=正常回答）
                created_at TEXT NOT NULL                  -- 提问时间
            );
            CREATE INDEX IF NOT EXISTS idx_tutor_student
                ON training_tutor_messages(student_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS student_goal (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                student_id TEXT NOT NULL REFERENCES student_info(id),  -- 学生（外键）
                certificate_id TEXT NOT NULL REFERENCES certificate(id),  -- 目标证书（外键）
                planned_date TEXT,                        -- 个人计划考试日（可空，ISO 日期）
                official_date_id TEXT REFERENCES exam_date(id),  -- 选定的官方考试日（可空，外键）
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','paused','achieved','archived')),  -- 进行/暂停/达成/归档
                created_at TEXT NOT NULL,                 -- 创建时间
                updated_at TEXT NOT NULL                  -- 最近更新时间（倒计时取值：官方日期优先，否则个人计划日）
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_student_goal_active
                ON student_goal(student_id, certificate_id) WHERE status = 'active';  -- 同证书仅一个进行中目标
            CREATE INDEX IF NOT EXISTS idx_student_goal_student
                ON student_goal(student_id, status);
            CREATE TABLE IF NOT EXISTS student_kp_notes (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                student_id TEXT NOT NULL REFERENCES student_info(id),  -- 学生（外键；本人隔离）
                knowledge_point_id TEXT NOT NULL REFERENCES knowledge_point(id),  -- 知识点（外键）
                content TEXT NOT NULL,                    -- 笔记正文（学生个人知识库，按知识点一卡）
                created_at TEXT NOT NULL,                 -- 创建时间
                updated_at TEXT NOT NULL,                 -- 最近更新时间
                UNIQUE(student_id, knowledge_point_id)    -- 每生每知识点一条，保存即覆盖
            );
            CREATE INDEX IF NOT EXISTS idx_student_kp_notes_student
                ON student_kp_notes(student_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS training_student_sessions (
                token_hash TEXT PRIMARY KEY,              -- 会话令牌 SHA-256（与主站 sessions 同惯例）
                student_id TEXT NOT NULL REFERENCES student_info(id),  -- 学生（外键）
                expires_at TEXT NOT NULL,                 -- 过期时间（ISO 8601 文本）
                created_at TEXT NOT NULL                  -- 创建时间
            );
            CREATE INDEX IF NOT EXISTS idx_training_student_sessions_student
                ON training_student_sessions(student_id);
            CREATE TABLE IF NOT EXISTS training_staff_sessions (
                token_hash TEXT PRIMARY KEY,              -- 教师/管理员会话令牌 SHA-256
                staff_id TEXT NOT NULL REFERENCES college_user(id),  -- 实训教师或管理员（外键）
                expires_at TEXT NOT NULL,                 -- 过期时间（ISO 8601 文本）
                created_at TEXT NOT NULL                  -- 创建时间
            );
            CREATE INDEX IF NOT EXISTS idx_training_staff_sessions_staff
                ON training_staff_sessions(staff_id);
            CREATE TABLE IF NOT EXISTS training_self_practice_attempts (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                student_id TEXT NOT NULL REFERENCES student_info(id),  -- 学生（外键）
                certificate_id TEXT NOT NULL REFERENCES certificate(id),  -- 证书（外键）
                knowledge_point_id TEXT REFERENCES knowledge_point(id),  -- 知识点（外键，可空=整证随机）
                rule_id TEXT NOT NULL,                    -- 本题目标规则（评分后随解析展示，未交前不下发）
                seed INTEGER NOT NULL,                    -- 仿真种子（确定性复现题目材料）
                level TEXT NOT NULL DEFAULT 'normal',     -- 难度
                year INTEGER NOT NULL DEFAULT 2026,       -- 教学年度（年度变化时提示适用性）
                source_type TEXT NOT NULL DEFAULT 'simulated',  -- 题源：simulated=规则仿真（真题/回忆/模拟预留，不冒充真题）
                rule_version TEXT NOT NULL DEFAULT '',    -- 出题时规则版本快照（旧作答可追溯当时内容）
                digest TEXT NOT NULL,                     -- 出题时材料指纹（提交时校验规则未变更）
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','scored')),  -- 作答中 / 已判分（重复提交不再计数）
                answers_json TEXT,                        -- 学生选择的风险点（JSON 数组文本）
                result_json TEXT,                         -- 判分结果与逐项解析（JSON 文本）
                created_at TEXT NOT NULL,                 -- 开始时间
                scored_at TEXT                            -- 判分时间
            );
            CREATE INDEX IF NOT EXISTS idx_training_self_practice_attempts_student
                ON training_self_practice_attempts(student_id, status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_training_self_practice_attempts_kp
                ON training_self_practice_attempts(knowledge_point_id);
            CREATE TABLE IF NOT EXISTS training_content_version (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                certificate_id TEXT NOT NULL REFERENCES certificate(id),  -- 证书（外键）
                label TEXT NOT NULL,                      -- 版本标签，证书内唯一（如 '2026 考纲仿真题 v1'）
                source_type TEXT NOT NULL DEFAULT 'simulated'
                    CHECK (source_type IN ('simulated','real','recall','mock')),
                                                          -- 题源类型：仿真/真题/回忆/模拟（数据模型预留，首发仅仿真）
                year INTEGER NOT NULL,                    -- 适用年度（年度变化时提示旧作答适用性）
                rules_digest TEXT NOT NULL DEFAULT '',    -- 本版本覆盖规则的指纹（sha256，检测内容漂移）
                rule_ids_json TEXT NOT NULL DEFAULT '[]', -- 本版本覆盖的规则清单（JSON 数组文本）
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','retired')),  -- 启用中 / 已轮换退役（行保留供旧作答回溯）
                source_ref TEXT NOT NULL DEFAULT '',      -- 依据/授权说明（真题等须有授权文号，服务层校验）
                note TEXT NOT NULL DEFAULT '',            -- 备注
                created_by TEXT REFERENCES college_user(id),  -- 发布人（自动基线版本为空）
                created_at TEXT NOT NULL,                 -- 发布时间
                updated_at TEXT NOT NULL,                 -- 最近更新时间
                UNIQUE(certificate_id, label)
            );
            CREATE INDEX IF NOT EXISTS idx_training_content_version_cert
                ON training_content_version(certificate_id, status);
        """)
        # 既有库的作答表补挂内容版本列（additive 迁移；旧行为 NULL=未版本化，按材料指纹回溯）。
        content_columns = {row['name'] for row in db.execute('PRAGMA table_info(training_content_version)')}
        if 'rules_snapshot_json' not in content_columns:
            db.execute("ALTER TABLE training_content_version ADD COLUMN rules_snapshot_json TEXT")
        if 'knowledge_snapshot_json' not in content_columns:
            db.execute("ALTER TABLE training_content_version ADD COLUMN knowledge_snapshot_json TEXT")
        attempt_columns = {row["name"] for row in db.execute(
            "PRAGMA table_info(training_self_practice_attempts)")}
        if "content_version_id" not in attempt_columns:
            db.execute(
                "ALTER TABLE training_self_practice_attempts"
                " ADD COLUMN content_version_id TEXT REFERENCES training_content_version(id)")
        # 作答细分（FR-K05/K06 待完善项）：新题/原题重做分开标记；跳过不删行、不进练习统计。
        if "mode" not in attempt_columns:
            db.execute(
                "ALTER TABLE training_self_practice_attempts"
                " ADD COLUMN mode TEXT NOT NULL DEFAULT 'new'")
        if "skipped_at" not in attempt_columns:
            db.execute(
                "ALTER TABLE training_self_practice_attempts ADD COLUMN skipped_at TEXT")
        # 真题/回忆/模拟题库作答（第四条就绪：真题授权先行、审核启用）。
        # 题库题作答与规则仿真共用 attempts 行：bank_question_id 非空即题库题，
        # rule_id 置空串、seed 置 0；判分按选项集合比对，解析判分后开放。
        if "bank_question_id" not in attempt_columns:
            db.execute(
                "ALTER TABLE training_self_practice_attempts"
                " ADD COLUMN bank_question_id TEXT REFERENCES training_bank_question(id)")
        # 题源与出题时规则版本：建表语句里有、但旧库（CREATE IF NOT EXISTS 不会补列）
        # 缺这两列，_scored_view 取 row["source_type"] 会 IndexError -> 提交接口 500。
        if "source_type" not in attempt_columns:
            db.execute(
                "ALTER TABLE training_self_practice_attempts"
                " ADD COLUMN source_type TEXT NOT NULL DEFAULT 'simulated'")
        if "rule_version" not in attempt_columns:
            db.execute(
                "ALTER TABLE training_self_practice_attempts"
                " ADD COLUMN rule_version TEXT NOT NULL DEFAULT ''")
        # 真题授权与题库、考纲升版、试用反馈（第四条就绪三件套）。
        db.executescript("""
            CREATE TABLE IF NOT EXISTS training_source_authorization (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                certificate_id TEXT NOT NULL REFERENCES certificate(id),  -- 授权适用证书（外键）
                licensor TEXT NOT NULL,                   -- 授权方（命题管理方或其授权出版方）
                auth_no TEXT NOT NULL,                    -- 授权文号（书面授权凭证编号，证书内唯一）
                question_types TEXT NOT NULL,             -- 覆盖题源（逗号分隔：real/recall/mock，服务层校验）
                valid_from TEXT NOT NULL,                 -- 生效日（ISO 日期）
                valid_until TEXT,                         -- 失效日（可空=长期；到期后不可再发布/导入）
                scope_note TEXT NOT NULL DEFAULT '',      -- 使用范围与限制说明
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','revoked')),  -- 生效 / 已撤销（撤销不留痕删除，可审计）
                created_by TEXT REFERENCES college_user(id),  -- 登记人（外键）
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(certificate_id, auth_no)
            );
            CREATE TABLE IF NOT EXISTS training_bank_question (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                certificate_id TEXT NOT NULL REFERENCES certificate(id),  -- 所属证书（外键）
                source_type TEXT NOT NULL
                    CHECK (source_type IN ('real','recall','mock')),
                                                          -- 题源：真题/回忆题/模拟题（仿真题为规则生成，不入库）
                year INTEGER NOT NULL,                    -- 考试年度（如 2026 年真题）
                question_no TEXT NOT NULL DEFAULT '',     -- 卷内题号（如 一、单选第3题）
                knowledge_point_id TEXT REFERENCES knowledge_point(id),  -- 关联知识点（外键，可空）
                stem TEXT NOT NULL,                       -- 题干
                options_json TEXT NOT NULL,               -- 选项（[{"key":"A","text":"…"},…]）
                answer_keys_json TEXT NOT NULL,           -- 标准答案（["A"] 或多选 ["A","C"]）
                explanation TEXT NOT NULL DEFAULT '',     -- 解析（判分后才对学生开放）
                source_ref TEXT NOT NULL,                 -- 出处说明（年份卷次+授权文号，导入时自动附带）
                auth_id TEXT NOT NULL REFERENCES training_source_authorization(id),  -- 授权记录（外键，必填）
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','pending','approved','rejected')),
                                                          -- 草稿/待审/已审核上线/驳回
                review_note TEXT NOT NULL DEFAULT '',     -- 审核意见
                reviewed_by TEXT REFERENCES college_user(id),  -- 审核人（外键）
                reviewed_at TEXT,                         -- 审核时间
                created_by TEXT REFERENCES college_user(id),  -- 导入人（外键）
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_training_bank_question_cert
                ON training_bank_question(certificate_id, source_type, status);
            CREATE TABLE IF NOT EXISTS training_outline_upgrade (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                certificate_id TEXT NOT NULL REFERENCES certificate(id),  -- 证书（外键）
                from_version TEXT NOT NULL,               -- 现行考纲版本（如 2026）
                to_version TEXT NOT NULL,                 -- 目标考纲版本（如 2027）
                status TEXT NOT NULL DEFAULT 'working'
                    CHECK (status IN ('working','activated')),  -- 核对中 / 已激活切换
                started_by TEXT REFERENCES college_user(id),  -- 发起人（外键）
                started_at TEXT NOT NULL,
                activated_by TEXT REFERENCES college_user(id),  -- 激活人（外键）
                activated_at TEXT,                        -- 激活时间
                note TEXT NOT NULL DEFAULT ''             -- 备注（如大纲发布文号）
            );
            CREATE INDEX IF NOT EXISTS idx_training_outline_upgrade_cert
                ON training_outline_upgrade(certificate_id, status);
            CREATE TABLE IF NOT EXISTS training_trial_feedback (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                student_id TEXT NOT NULL REFERENCES student_info(id),  -- 反馈学生（外键）
                certificate_id TEXT REFERENCES certificate(id),  -- 关联证书（外键，可空）
                ref_type TEXT NOT NULL DEFAULT 'none'
                    CHECK (ref_type IN ('none','kp','attempt','question')),
                                                          -- 关联对象：无/知识点/作答/题库题
                ref_id TEXT NOT NULL DEFAULT '',          -- 关联对象 id（ref_type=none 时为空）
                category TEXT NOT NULL
                    CHECK (category IN ('bug','suggestion','content','other')),
                                                          -- 类别：缺陷/建议/内容纠错/其他
                content TEXT NOT NULL,                    -- 反馈内容（1~1000 字，服务层校验）
                status TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new','processing','resolved')),  -- 新反馈/处理中/已处理
                staff_note TEXT NOT NULL DEFAULT '',      -- 教师处理备注
                handled_by TEXT REFERENCES college_user(id),  -- 处理人（外键）
                handled_at TEXT,                          -- 处理时间
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_training_trial_feedback_student
                ON training_trial_feedback(student_id, created_at DESC);
        """)
        # 教学单元与实操案例（重设计方案 B1，全部 additive）。
        # 单元/案例只存 rule_code（R-001…R-024），不存 YAML 文件路径——执行源仍是
        # rules/*.yaml，图谱 TaxRule 存版本快照；将来 YAML 退役教学侧不用改。
        # 依据链（TaxRule→USES_METRIC→Metric、→Clause→Policy）实时查图谱；
        # 本库 Clause/Policy 为 0，凡引用条款一律展示「依据待专业复核」。
        db.executescript("""
            CREATE TABLE IF NOT EXISTS teaching_unit (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                certificate_id TEXT NOT NULL REFERENCES certificate(id),  -- 所属证书（外键）
                name TEXT NOT NULL,                       -- 单元名称（证书内唯一）
                stage TEXT NOT NULL DEFAULT 'voucher'
                    CHECK (stage IN ('voucher','data','conclusion')),
                                                          -- 三段标准：凭证层/数据层/结论层
                standard TEXT NOT NULL DEFAULT '',        -- 一句话标准（学生要能回答的问题）
                knowledge_point_ids_json TEXT NOT NULL DEFAULT '[]',  -- 关联知识点 id（JSON 数组）
                rule_codes_json TEXT NOT NULL DEFAULT '[]',  -- 关联规则（rule_code，JSON 数组）
                deliverables TEXT NOT NULL DEFAULT '',    -- 学生要交的证据说明
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','published','archived')),  -- 草稿/已发布/已归档
                sort_order INTEGER NOT NULL DEFAULT 0,    -- 排序
                created_by TEXT REFERENCES college_user(id),  -- 创建人（外键）
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(certificate_id, name)
            );
            CREATE INDEX IF NOT EXISTS idx_teaching_unit_cert
                ON teaching_unit(certificate_id, status, sort_order);
            CREATE TABLE IF NOT EXISTS teaching_case (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                certificate_id TEXT NOT NULL REFERENCES certificate(id),  -- 所属证书（外键）
                unit_id TEXT REFERENCES teaching_unit(id),  -- 所属教学单元（外键，可空=独立案例）
                name TEXT NOT NULL,                       -- 案例名称（证书内唯一）
                scenario TEXT NOT NULL DEFAULT '',        -- 背景说明（脱敏仿真情境）
                rule_codes_json TEXT NOT NULL DEFAULT '[]',  -- 绑定规则（rule_code，JSON 数组）
                materials_json TEXT NOT NULL DEFAULT '[]',   -- 材料包清单（[{"name":…,"note":…}]）
                standard_conclusion_json TEXT NOT NULL DEFAULT '[]',  -- 标准结论（提交后才对学生开放）
                desensitization_note TEXT NOT NULL DEFAULT '',  -- 脱敏说明
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','published','archived')),
                created_by TEXT REFERENCES college_user(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(certificate_id, name)
            );
            CREATE INDEX IF NOT EXISTS idx_teaching_case_cert
                ON teaching_case(certificate_id, status);
            CREATE INDEX IF NOT EXISTS idx_teaching_case_unit
                ON teaching_case(unit_id);
        """)
        # 考纲升版核对状态（第四条就绪：逐条核对升版在现行树上进行）。
        # ''=非核对树；pending=待核对 confirmed=沿用 updated=已按新纲修改
        # added=新版新增（激活时才对学生可见） removed=新版删除（激活时停用）。
        kp_columns = {row["name"] for row in db.execute("PRAGMA table_info(knowledge_point)")}
        if "review_status" not in kp_columns:
            db.execute("ALTER TABLE knowledge_point ADD COLUMN review_status TEXT NOT NULL DEFAULT ''")
        # 知识点-题目关联的停用标记（治理题源：停用不删行）。早期库建表时没有这一列，
        # 缺列时学生端知识点清单的 active=1 过滤会直接报 no such column，故在此补齐。
        link_columns = {row["name"] for row in db.execute("PRAGMA table_info(knowledge_point_link)")}
        if "active" not in link_columns:
            db.execute("ALTER TABLE knowledge_point_link ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        # 班级归属追溯（FR-U03：账号与班级归属可追溯）。整建制/单点改班各记一行，不改写历史。
        db.executescript("""
            CREATE TABLE IF NOT EXISTS roster_class_history (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                student_id TEXT NOT NULL REFERENCES student_info(id),  -- 学生（外键）
                from_class TEXT,                          -- 原班级（可空=建档未分班）
                to_class TEXT,                            -- 新班级（可空=移出分班）
                changed_by TEXT REFERENCES college_user(id),  -- 操作人（外键）
                created_at TEXT NOT NULL                  -- 变更时间（ISO 8601 文本）
            );
            CREATE INDEX IF NOT EXISTS idx_roster_class_history_student
                ON roster_class_history(student_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_roster_class_history_time
                ON roster_class_history(created_at);
        """)
        # 课程管理（FR-U04 v1）：课程绑定授课教师与班级，支持同课多班（course_class 多行）。
        # 停用不删行（课程与归属可追溯）；班级沿用 class_name 聚合键，与 student_info 对齐；
        # 课程级作业序列（training_task 挂课程）预留到 v2，本版不含作业外键。
        db.executescript("""
            CREATE TABLE IF NOT EXISTS course (
                id TEXT PRIMARY KEY,                      -- 主键：UUID 文本
                college TEXT NOT NULL,                    -- 所属院校（实训数据隔离键）
                name TEXT NOT NULL,                       -- 课程名称
                semester TEXT,                            -- 学年学期（如 2026-2027-1，可空）
                teacher_id TEXT REFERENCES college_user(id),  -- 授课教师（外键，可空）
                description TEXT,                         -- 课程说明（可空）
                active INTEGER NOT NULL DEFAULT 1,        -- 1=开设 0=停用（停用不删行）
                created_at TEXT NOT NULL,                 -- 建课时间（ISO 8601 文本）
                created_by TEXT REFERENCES college_user(id)   -- 建课操作人（外键）
            );
            CREATE TABLE IF NOT EXISTS course_class (
                course_id TEXT NOT NULL REFERENCES course(id),
                class_name TEXT NOT NULL,                 -- 绑定班级（与 student_info.class_name 同口径）
                PRIMARY KEY (course_id, class_name)
            );
            CREATE INDEX IF NOT EXISTS idx_course_college ON course(college, active);
            CREATE INDEX IF NOT EXISTS idx_course_teacher ON course(teacher_id);
        """)
        # 高校组织架构（FR-U01 第一期）：院系 / 学年学期 / 班级实体。
        # 与现有两层模型并存：student_info.class_name 文本名册不动，班级实体
        # 经 (college, name) 对齐在读人数；层级变更不改写既有数据归属。
        # 同院校下班级名在同学期内唯一（term_id 为空时由服务层校验，
        # SQLite UNIQUE 不约束 NULL），故此处只建查询索引。
        db.executescript("""
            CREATE TABLE IF NOT EXISTS college_department (
                id TEXT PRIMARY KEY,
                college TEXT NOT NULL,
                name TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(college, name)
            );
            CREATE TABLE IF NOT EXISTS college_term (
                id TEXT PRIMARY KEY,
                college TEXT NOT NULL,
                name TEXT NOT NULL,
                start_date TEXT,
                end_date TEXT,
                is_current INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(college, name)
            );
            CREATE TABLE IF NOT EXISTS college_class (
                id TEXT PRIMARY KEY,
                college TEXT NOT NULL,
                name TEXT NOT NULL,
                department_id TEXT REFERENCES college_department(id),
                term_id TEXT REFERENCES college_term(id),
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_college_class_lookup
                ON college_class(college, name);
        """)
        # 专业层级（FR-U01 待补）：院系 → 专业 → 班级。专业挂院系（可空），
        # 班级可选挂专业；同院校专业名唯一；既有库 additive 加列补挂 major_id。
        db.executescript("""
            CREATE TABLE IF NOT EXISTS college_major (
                id TEXT PRIMARY KEY,
                college TEXT NOT NULL,
                name TEXT NOT NULL,
                department_id TEXT REFERENCES college_department(id),
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(college, name)
            );
            CREATE INDEX IF NOT EXISTS idx_college_major_lookup
                ON college_major(college, name);
        """)
        class_columns = {row["name"] for row in db.execute("PRAGMA table_info(college_class)")}
        if "major_id" not in class_columns:
            db.execute(
                "ALTER TABLE college_class ADD COLUMN major_id TEXT REFERENCES college_major(id)")
        # 院校席位与配额（FR-U06）：教师/学生分席，院校按师生配额核销。
        # 无记录院校按默认档放行（不阻塞既有部署）；调整走 revision 乐观锁
        # 并逐次留痕（与 platform org_quota/members.set_quota 同模式）。
        db.executescript("""
            CREATE TABLE IF NOT EXISTS college_quota (
                college TEXT PRIMARY KEY,
                teacher_quota INTEGER NOT NULL,
                student_quota INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                updated_by TEXT REFERENCES college_user(id),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS college_quota_history (
                id TEXT PRIMARY KEY,
                college TEXT NOT NULL,
                old_teacher_quota INTEGER,
                old_student_quota INTEGER,
                new_teacher_quota INTEGER NOT NULL,
                new_student_quota INTEGER NOT NULL,
                changed_by TEXT REFERENCES college_user(id),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_college_quota_history_time
                ON college_quota_history(college, created_at);
        """)
        # 学期名册快照（FR-U01 第二期）：把"当前在读学生 → 班级"归属按学期
        # 冻结，供新学期重建班级后按旧学期回溯名册。重拍（同学期再快照）
        # 覆盖更新（快照是当时事实的重读；归属变更审计链另有 roster_class_history）。
        db.executescript("""
            CREATE TABLE IF NOT EXISTS college_roster_snapshot (
                id TEXT PRIMARY KEY,
                college TEXT NOT NULL,
                term_id TEXT NOT NULL REFERENCES college_term(id),
                student_id TEXT NOT NULL REFERENCES student_info(id),
                class_name TEXT NOT NULL,
                taken_by TEXT REFERENCES college_user(id),
                taken_at TEXT NOT NULL,
                UNIQUE(term_id, student_id)
            );
            CREATE INDEX IF NOT EXISTS idx_college_roster_snapshot_term
                ON college_roster_snapshot(term_id, class_name);
        """)
        from webapp import ai_keys
        ai_keys.migrate(db)
        from webapp import policy_graph
        policy_graph.migrate(db)
        from src import vat_reconciliation
        vat_reconciliation.initialize(db)
        from webapp import university_members
        university_members.migrate(db)
        from webapp import university_workspace
        university_workspace.migrate(db)
        if 'student_seats' not in {r['name'] for r in db.execute('PRAGMA table_info(invite_codes)')}:
            db.execute('ALTER TABLE invite_codes ADD COLUMN student_seats INTEGER NOT NULL DEFAULT 0')
        if 'code_secret' not in {r['name'] for r in db.execute('PRAGMA table_info(member_invitations)')}:
            db.execute('ALTER TABLE member_invitations ADD COLUMN code_secret TEXT')
