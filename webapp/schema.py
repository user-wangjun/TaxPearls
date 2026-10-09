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
            ("logo_mime", "TEXT"), ("logo_bytes", "BLOB"), ("logo_updated_at", "TEXT")
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
        mistake_book.migrate(db)
        members.migrate(db)
        from webapp import material_batches
        material_batches.migrate(db)
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
        attempt_columns = {row["name"] for row in db.execute(
            "PRAGMA table_info(training_self_practice_attempts)")}
        if "content_version_id" not in attempt_columns:
            db.execute(
                "ALTER TABLE training_self_practice_attempts"
                " ADD COLUMN content_version_id TEXT REFERENCES training_content_version(id)")
