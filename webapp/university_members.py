"""School capacity, delegated student seats and invitation-backed membership."""

import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from fastapi import Cookie, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from webapp import email_auth, members
from webapp.access import AccessDenied, current_actor


def migrate(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS university_organizations (
            college TEXT PRIMARY KEY, org_id TEXT NOT NULL UNIQUE REFERENCES org_settings(org_id)
        );
        CREATE TABLE IF NOT EXISTS university_identity_links (
            user_id TEXT PRIMARY KEY REFERENCES users(id),
            staff_id TEXT UNIQUE REFERENCES college_user(id),
            student_id TEXT UNIQUE REFERENCES student_info(id),
            CHECK ((staff_id IS NULL) != (student_id IS NULL))
        );
        CREATE TABLE IF NOT EXISTS teacher_student_quotas (
            teacher_id TEXT PRIMARY KEY REFERENCES college_user(id), college TEXT NOT NULL,
            seats INTEGER NOT NULL CHECK(seats>=0), revision INTEGER NOT NULL DEFAULT 1,
            updated_by TEXT NOT NULL REFERENCES college_user(id), updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS student_teacher_memberships (
            student_id TEXT PRIMARY KEY REFERENCES student_info(id),
            teacher_id TEXT NOT NULL REFERENCES college_user(id), college TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_student_teacher_owner ON student_teacher_memberships(teacher_id);
        CREATE TABLE IF NOT EXISTS training_invitation_links (
            id TEXT PRIMARY KEY, college TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('teacher','student')),
            owner_id TEXT NOT NULL REFERENCES college_user(id), token_hash TEXT NOT NULL UNIQUE,
            code_secret TEXT, active INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
            used_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_school_teacher_invitation
            ON training_invitation_links(college) WHERE role='teacher';
        CREATE UNIQUE INDEX IF NOT EXISTS idx_teacher_student_invitation
            ON training_invitation_links(owner_id) WHERE role='student';
        CREATE TABLE IF NOT EXISTS university_capacity_events (
            id TEXT PRIMARY KEY, college TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id),
            staff_seats INTEGER NOT NULL, student_seats INTEGER NOT NULL, created_at TEXT NOT NULL,
            old_staff_seats INTEGER, old_student_seats INTEGER
        );
    """)
    columns = {r["name"] for r in db.execute("PRAGMA table_info(university_capacity_events)")}
    for column in ("old_staff_seats", "old_student_seats"):
        if column not in columns:
            db.execute(f"ALTER TABLE university_capacity_events ADD COLUMN {column} INTEGER")


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def staff_actor(db, actor, *, administrator=False):
    row = db.execute("SELECT * FROM college_user WHERE id=? AND active=1", (actor["id"],)).fetchone()
    if not row or row["college"] != actor["college"] or not identity_active(db, staff_id=row["id"]):
        raise AccessDenied("教师权限已变化，请重新登录。", 403)
    if administrator and row["role"] not in {"admin", "school_admin"}:
        raise AccessDenied("仅高校管理员可以分配额度或邀请教师。", 403)
    return dict(row)


def identity_active(db, *, staff_id=None, student_id=None):
    column, value = ("staff_id", staff_id) if staff_id else ("student_id", student_id)
    row = db.execute(
        f"""SELECT u.active FROM university_identity_links l JOIN users u ON u.id=l.user_id
        WHERE l.{column}=?""",
        (value,),
    ).fetchone()
    return row is None or bool(row["active"])


def identity_hash(db, fallback, *, staff_id=None, student_id=None):
    column, value = ("staff_id", staff_id) if staff_id else ("student_id", student_id)
    row = db.execute(
        f"""SELECT u.password_hash FROM university_identity_links l JOIN users u ON u.id=l.user_id
        WHERE l.{column}=? AND u.active=1""",
        (value,),
    ).fetchone()
    return row["password_hash"] if row else fallback


def revoke_identity(db, user_id):
    link = db.execute("SELECT * FROM university_identity_links WHERE user_id=?", (user_id,)).fetchone()
    if link:
        db.execute("DELETE FROM training_staff_sessions WHERE staff_id=?", (link["staff_id"],))
        db.execute("DELETE FROM training_student_sessions WHERE student_id=?", (link["student_id"],))


def set_identity_active(db, user_id, active):
    link = db.execute("SELECT * FROM university_identity_links WHERE user_id=?", (user_id,)).fetchone()
    if not link:
        return False
    from webapp.training_portal import require_teacher_slot, require_student_slot as school_slot

    if link["staff_id"]:
        row = db.execute("SELECT college,active FROM college_user WHERE id=?", (link["staff_id"],)).fetchone()
        if active and not row["active"]:
            require_teacher_slot(db, row["college"])
        db.execute("UPDATE college_user SET active=? WHERE id=?", (int(active), link["staff_id"]))
    else:
        row = db.execute("SELECT college,active FROM student_info WHERE id=?", (link["student_id"],)).fetchone()
        if active and not row["active"]:
            owner = db.execute(
                "SELECT teacher_id FROM student_teacher_memberships WHERE student_id=?", (link["student_id"],)
            ).fetchone()
            if owner:
                require_student_slot(db, owner[0], row["college"])
            else:
                school_slot(db, row["college"])
        db.execute("UPDATE student_info SET active=? WHERE id=?", (int(active), link["student_id"]))
    revoke_identity(db, user_id)
    return True


def sync_password(db, password_hash, *, user_id=None, staff_id=None, student_id=None):
    column, value = (
        ("user_id", user_id) if user_id else (("staff_id", staff_id) if staff_id else ("student_id", student_id))
    )
    link = db.execute(f"SELECT * FROM university_identity_links WHERE {column}=?", (value,)).fetchone()
    if not link:
        return
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, link["user_id"]))
    if link["staff_id"]:
        db.execute("UPDATE college_user SET password_hash=? WHERE id=?", (password_hash, link["staff_id"]))
    if link["student_id"]:
        db.execute("UPDATE student_info SET password_hash=? WHERE id=?", (password_hash, link["student_id"]))
    db.execute("DELETE FROM sessions WHERE user_id=?", (link["user_id"],))
    revoke_identity(db, link["user_id"])


def school_org(db, college):
    row = db.execute("SELECT org_id FROM university_organizations WHERE college=?", (college,)).fetchone()
    if row:
        return row["org_id"]
    org_id = "university-" + secrets.token_hex(8)
    db.execute(
        """INSERT INTO org_settings(org_id,org_type,display_name,updated_at)
        VALUES (?,'university',?,?)""",
        (org_id, college, now()),
    )
    db.execute("INSERT INTO university_organizations VALUES (?,?)", (college, org_id))
    return org_id


def provision_founder(db, user_id, org_id, staff_seats, student_seats=0):
    row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    staff_id = secrets.token_hex(16)
    db.execute("INSERT INTO university_organizations VALUES (?,?)", (org_id, org_id))
    db.execute(
        """INSERT INTO college_user
        (id,username,password_hash,display_name,college,email,role,active,created_at)
        VALUES (?,?,?,?,?,?,'admin',1,?)""",
        (staff_id, row["username"], row["password_hash"], row["display_name"], org_id, row["email"], now()),
    )
    db.execute("INSERT INTO university_identity_links VALUES (?,?,NULL)", (user_id, staff_id))
    db.execute(
        """INSERT INTO college_quota(college,teacher_quota,student_quota,revision,updated_at)
        VALUES (?,?,?,1,?)""",
        (org_id, staff_seats, student_seats, now()),
    )


def used(db, teacher_id):
    return db.execute(
        """SELECT count(*) FROM student_teacher_memberships m JOIN student_info s ON s.id=m.student_id
        WHERE m.teacher_id=? AND s.active=1""",
        (teacher_id,),
    ).fetchone()[0]


def school_state(db, college):
    from webapp.training_portal import college_quota_state

    state = college_quota_state(db, college)
    allocated = db.execute(
        "SELECT coalesce(sum(seats),0) FROM teacher_student_quotas WHERE college=?", (college,)
    ).fetchone()[0]
    unowned = db.execute(
        """SELECT count(*) FROM student_info s LEFT JOIN student_teacher_memberships m ON m.student_id=s.id
        WHERE s.college=? AND s.active=1 AND m.student_id IS NULL""",
        (college,),
    ).fetchone()[0]
    return {
        **state,
        "allocated": allocated,
        "unassigned_students": unowned,
        "unallocated": max(0, state["student_quota"] - allocated - unowned),
    }


def teacher_state(db, teacher_id):
    row = db.execute("SELECT seats,revision FROM teacher_student_quotas WHERE teacher_id=?", (teacher_id,)).fetchone()
    seats = row["seats"] if row else 0
    count = used(db, teacher_id)
    return {
        "seats": seats,
        "used": count,
        "remaining": max(0, seats - count),
        "revision": row["revision"] if row else 0,
    }


def require_student_slot(db, teacher_id, college):
    staff_actor(db, {"id": teacher_id, "college": college})
    if teacher_state(db, teacher_id)["remaining"] < 1:
        raise AccessDenied("教师名下学生席位已满，请联系高校管理员分配额度。", 409)
    state = school_state(db, college)
    if state["students_used"] >= state["student_quota"]:
        raise AccessDenied("学校学生席位已满，请联系平台顶级管理员。", 409)


def student_scope(db, actor, student_id):
    who = staff_actor(db, actor)
    row = db.execute(
        """SELECT s.id,m.teacher_id FROM student_info s
        LEFT JOIN student_teacher_memberships m ON m.student_id=s.id WHERE s.id=? AND s.college=?""",
        (student_id, who["college"]),
    ).fetchone()
    if not row or (who["role"] not in {"admin", "school_admin"} and row["teacher_id"] != who["id"]):
        raise AccessDenied("学生不存在或无权管理。")
    return row


def overview(store, actor):
    with store.connect() as db:
        who = staff_actor(db, actor)
        all_school = who["role"] in {"admin", "school_admin"}
        rows = db.execute(
            """SELECT id,display_name,role,active FROM college_user
            WHERE college=? AND (id=? OR ?=1) ORDER BY created_at,id""",
            (who["college"], who["id"], int(all_school)),
        ).fetchall()
        org = db.execute(
            """SELECT o.display_name FROM university_organizations u JOIN org_settings o ON o.org_id=u.org_id
            WHERE u.college=?""",
            (who["college"],),
        ).fetchone()
        return {
            "college": who["college"],
            "school_name": org[0] if org else who["college"],
            "administrator": all_school,
            "actor_id": who["id"],
            "quota": school_state(db, who["college"]) if all_school else None,
            "mine": teacher_state(db, who["id"]),
            "teachers": [{**dict(r), "quota": teacher_state(db, r["id"])} for r in rows],
            "unassigned_students": [
                dict(r)
                for r in db.execute(
                    """SELECT s.id,s.student_no,s.name FROM student_info s
                    LEFT JOIN student_teacher_memberships m ON m.student_id=s.id
                    WHERE s.college=? AND s.active=1 AND m.student_id IS NULL""",
                    (who["college"],),
                )
            ]
            if all_school
            else [],
        }


def allocate(store, actor, teacher_id, seats, revision):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        who = staff_actor(db, actor, administrator=True)
        target = db.execute(
            "SELECT id,active FROM college_user WHERE id=? AND college=?", (teacher_id, who["college"])
        ).fetchone()
        if not target:
            raise AccessDenied()
        old = teacher_state(db, teacher_id)
        if not target["active"] and seats > old["seats"]:
            raise AccessDenied("停用教师只能回收未使用的额度。", 422)
        if revision != old["revision"]:
            raise AccessDenied("额度已变化，请刷新后重试。", 409)
        if seats < old["used"]:
            raise AccessDenied("额度不能低于该教师已启用的学生人数。", 422)
        state = school_state(db, who["college"])
        if state["allocated"] - old["seats"] + seats + state["unassigned_students"] > state["student_quota"]:
            raise AccessDenied("学校待分配额度不足；学校总额度由平台顶级管理员调整。", 409)
        db.execute(
            """INSERT INTO teacher_student_quotas VALUES (?,?,?,?,?,?)
            ON CONFLICT(teacher_id) DO UPDATE SET seats=excluded.seats,revision=teacher_student_quotas.revision+1,
            updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (teacher_id, who["college"], seats, revision + 1, who["id"], now()),
        )
    return overview(store, actor)


def assign_legacy(store, actor, student_id, teacher_id):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        who = staff_actor(db, actor, administrator=True)
        student_scope(db, who, student_id)
        if db.execute("SELECT 1 FROM student_teacher_memberships WHERE student_id=?", (student_id,)).fetchone():
            raise AccessDenied("学生已有归属，不能在此重复分配。", 409)
        target = db.execute(
            "SELECT id FROM college_user WHERE id=? AND college=? AND active=1", (teacher_id, who["college"])
        ).fetchone()
        if not target:
            raise AccessDenied()
        q = teacher_state(db, teacher_id)
        # Assigning an existing student moves occupied capacity into a teacher's reserved pool.
        if q["remaining"] < 1:
            school = school_state(db, who["college"])
            db.execute(
                """INSERT INTO teacher_student_quotas VALUES (?,?,?,?,?,?) ON CONFLICT(teacher_id)
                DO UPDATE SET seats=seats+1,revision=revision+1,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (teacher_id, who["college"], q["seats"] + 1, q["revision"] + 1, who["id"], now()),
            )
            if school["allocated"] + school["unassigned_students"] > school["student_quota"]:
                raise AccessDenied("学校现有用量超额，请先由平台调整额度。", 409)
        db.execute("INSERT INTO student_teacher_memberships VALUES (?,?,?)", (student_id, teacher_id, who["college"]))
    return overview(store, actor)


def links(store, actor):
    with store.connect() as db:
        who = staff_actor(db, actor)
        rows = db.execute(
            """SELECT id,role,owner_id,code_secret,active,revision,used_count FROM training_invitation_links
            WHERE college=? AND ((role='teacher' AND ?=1) OR (role='student' AND owner_id=?)) ORDER BY role""",
            (who["college"], int(who["role"] in {"admin", "school_admin"}), who["id"]),
        ).fetchall()
        return {
            "links": [
                {k: r[k] for k in ("id", "role", "owner_id", "active", "revision", "used_count")}
                | {"path": "/p/" + r["code_secret"] if r["code_secret"] else None}
                for r in rows
            ]
        }


def mutate_link(store, actor, *, role=None, link_id=None, revision=None, active=None):
    from webapp.storage import _generate_invite_code, _hash_token

    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        who = staff_actor(db, actor, administrator=role == "teacher")
        if link_id:
            row = db.execute(
                "SELECT * FROM training_invitation_links WHERE id=? AND college=?", (link_id, who["college"])
            ).fetchone()
            if (
                not row
                or (row["role"] == "teacher" and who["role"] not in {"admin", "school_admin"})
                or (row["role"] == "student" and row["owner_id"] != who["id"])
            ):
                raise AccessDenied()
            if row["revision"] != revision:
                raise AccessDenied("链接已变化，请刷新后重试。", 409)
            if active is not None:
                db.execute(
                    "UPDATE training_invitation_links SET active=?,revision=revision+1,updated_at=? WHERE id=?",
                    (int(active), now(), link_id),
                )
            else:
                code = _generate_invite_code(32)
                db.execute(
                    "UPDATE training_invitation_links SET token_hash=?,code_secret=?,revision=revision+1,updated_at=? WHERE id=?",
                    (
                        _hash_token(code),
                        db.field_codec.seal(code, "training_invitation_links", "code_secret", link_id),
                        now(),
                        link_id,
                    ),
                )
            if row["role"] == "teacher":
                db.execute("UPDATE training_invitation_links SET owner_id=? WHERE id=?", (who["id"], link_id))
        else:
            if role not in {"teacher", "student"}:
                raise AccessDenied("无效邀请角色。", 422)
            school_org(db, who["college"])
            code = _generate_invite_code(32)
            new_id = secrets.token_hex(12)
            try:
                db.execute(
                    """INSERT INTO training_invitation_links
                    (id,college,role,owner_id,token_hash,code_secret,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        new_id,
                        who["college"],
                        role,
                        who["id"],
                        _hash_token(code),
                        db.field_codec.seal(code, "training_invitation_links", "code_secret", new_id),
                        now(),
                        now(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise AccessDenied("已有该邀请链接，请使用或刷新现有链接。", 409) from None
    return links(store, actor)


def resolve_link(db, code):
    from webapp.storage import _hash_token, _normalize_invite_code

    row = db.execute(
        "SELECT * FROM training_invitation_links WHERE token_hash=?", (_hash_token(_normalize_invite_code(code)),)
    ).fetchone()
    if row:
        staff = db.execute(
            "SELECT * FROM college_user WHERE id=? AND college=? AND active=1", (row["owner_id"], row["college"])
        ).fetchone()
        if not row["active"] or not staff or not identity_active(db, staff_id=staff["id"]):
            raise ValueError("邀请链接无效或已停用。")
        if row["role"] == "teacher" and staff["role"] not in {"admin", "school_admin"}:
            raise ValueError("邀请人的管理权限已变化。")
    return row


def preview(store, code):
    from webapp.storage import _hash_token, _normalize_invite_code

    with store.connect() as db:
        row = resolve_link(db, code)
        if row:
            org = db.execute(
                """SELECT o.display_name FROM university_organizations u JOIN org_settings o ON o.org_id=u.org_id
                WHERE u.college=?""",
                (row["college"],),
            ).fetchone()
            inviter = db.execute("SELECT display_name FROM college_user WHERE id=?", (row["owner_id"],)).fetchone()
            return {
                "organization": org[0] if org else row["college"],
                "role": row["role"],
                "teacher": inviter[0] if row["role"] == "student" else None,
                "university": True,
            }
        member = db.execute(
            """SELECT i.org_id,i.role,i.active,o.display_name,o.org_type FROM member_invitations i
            LEFT JOIN org_settings o ON o.org_id=i.org_id WHERE i.token_hash=?""",
            (_hash_token(_normalize_invite_code(code)),),
        ).fetchone()
        if not member or not member["active"]:
            raise ValueError("邀请链接无效或已停用。")
        if member["org_type"] == "enterprise" and member["role"] != "accountant":
            raise ValueError("该邀请角色不属于企业，请联系管理员更新链接。")
        return {
            "organization": member["display_name"] or member["org_id"],
            "role": member["role"],
            "teacher": None,
            "university": False,
        }


def redeem(store, body, browser):
    from webapp.storage import _validate_email, _validate_password, _passwords, _hash_token, _username_from_email

    address = _validate_email(body.email)
    if not address:
        raise ValueError("请填写有效邮箱。")
    with store.connect() as db:
        training = resolve_link(db, body.invite_code)
    if not training:
        preview(store, body.invite_code)
        if body.existing:
            return redeem_existing_employee(store, body)
        if not body.display_name.strip():
            raise ValueError("请填写姓名。")
        user, token = store.register_with_code(
            address, body.code, body.invite_code, body.password, browser_session=browser, display_name=body.display_name
        )
        return user, token
    if not body.existing:
        _validate_password(body.password, address)
    error = None
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        invite = resolve_link(db, body.invite_code)
        if not invite:
            raise ValueError("邀请链接已变化。")
        org_id = school_org(db, invite["college"])
        person = db.execute("SELECT * FROM users WHERE email=?", (address,)).fetchone()
        if body.existing:
            if not person or not person["active"]:
                raise ValueError("邮箱或密码不正确。")
            try:
                _passwords.verify(person["password_hash"], body.password)
            except (VerifyMismatchError, InvalidHashError):
                raise ValueError("邮箱或密码不正确。") from None
            if person["org_id"] != org_id or person["role"] != invite["role"]:
                raise ValueError("账号已属于其他机构或角色，不能通过邀请自动转移。")
            linked = db.execute("SELECT * FROM university_identity_links WHERE user_id=?", (person["id"],)).fetchone()
            if not linked:
                raise ValueError("现有账号尚未关联教学档案，请联系管理员核对。")
            if invite["role"] == "student":
                owner = db.execute(
                    "SELECT teacher_id FROM student_teacher_memberships WHERE student_id=?", (linked["student_id"],)
                ).fetchone()
                if not owner or owner[0] != invite["owner_id"]:
                    raise ValueError("该学生已有其他教师归属，请联系高校管理员处理。")
            user_id = person["id"]
        else:
            if person:
                raise ValueError("该邮箱已注册，请选择已有账号登录。")
            proof, error = email_auth.verify_code(db, address, "register", body.code, browser)
            if not error:
                from webapp.storage import _normalize_invite_code

                if proof["invite_hash"] and proof["invite_hash"] != _hash_token(
                    _normalize_invite_code(body.invite_code)
                ):
                    raise ValueError("邮箱验证对应的邀请链接已变化，请重新获取验证码。")
                name = body.display_name.strip()
                if not name:
                    raise ValueError("请填写姓名。")
                if invite["role"] == "teacher":
                    from webapp.training_portal import require_teacher_slot

                    require_teacher_slot(db, invite["college"])
                    if db.execute(
                        "SELECT 1 FROM college_user WHERE college=? AND lower(email)=?", (invite["college"], address)
                    ).fetchone():
                        raise ValueError("该邮箱已有教学档案，请联系高校管理员核对关联。")
                else:
                    require_student_slot(db, invite["owner_id"], invite["college"])
                    if not body.student_no.strip():
                        raise ValueError("请填写学号。")
                    if db.execute(
                        "SELECT 1 FROM student_info WHERE college=? AND student_no=?",
                        (invite["college"], body.student_no.strip()),
                    ).fetchone():
                        raise ValueError("该学号已有学生档案，请联系管理员核对，不能重复建档。")
                user_id = secrets.token_hex(12)
                username = _username_from_email(address, db)
                password_hash = _passwords.hash(body.password)
                db.execute(
                    """INSERT INTO users(id,username,password_hash,display_name,role,org_id,active,email,created_at)
                    VALUES (?,?,?,?,?,?,1,?,?)""",
                    (user_id, username, password_hash, name, invite["role"], org_id, address, now()),
                )
                profile_id = secrets.token_hex(16)
                if invite["role"] == "teacher":
                    if db.execute("SELECT 1 FROM college_user WHERE username=?", (username,)).fetchone():
                        username += secrets.token_hex(3)
                    db.execute(
                        """INSERT INTO college_user(id,username,password_hash,display_name,college,email,role,created_at)
                        VALUES (?,?,?,?,?,?,'teacher',?)""",
                        (profile_id, username, password_hash, name, invite["college"], address, now()),
                    )
                    db.execute("INSERT INTO university_identity_links VALUES (?,?,NULL)", (user_id, profile_id))
                else:
                    db.execute(
                        """INSERT INTO student_info(id,college,student_no,name,password_hash,email,created_at)
                        VALUES (?,?,?,?,?,?,?)""",
                        (profile_id, invite["college"], body.student_no.strip(), name, password_hash, address, now()),
                    )
                    db.execute("INSERT INTO university_identity_links VALUES (?,NULL,?)", (user_id, profile_id))
                    db.execute(
                        "INSERT INTO student_teacher_memberships VALUES (?,?,?)",
                        (profile_id, invite["owner_id"], invite["college"]),
                    )
                db.execute(
                    "UPDATE training_invitation_links SET used_count=used_count+1,updated_at=? WHERE id=?",
                    (now(), invite["id"]),
                )
                email_auth.consume(db, proof)
                members.log(
                    db,
                    {"id": user_id, "org_id": org_id},
                    "join_university",
                    "user",
                    user_id,
                    f"role={invite['role']};link={invite['id']}",
                )
        if not error:
            token = secrets.token_urlsafe(32)
            expires = (datetime.now(UTC) + timedelta(hours=12)).isoformat(timespec="seconds")
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (_hash_token(token), user_id, expires, now()))
    if error:
        raise ValueError(error)
    return store.get_user(user_id), token


def redeem_existing_employee(store, body):
    from webapp import invitations
    from webapp.storage import _hash_token, _normalize_invite_code, _passwords

    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        invite = invitations.resolve(
            db, _hash_token(_normalize_invite_code(body.invite_code)), body.email.strip().lower()
        )
        if not invite:
            raise ValueError("邀请链接无效。")
        person = db.execute("SELECT * FROM users WHERE email=? AND active=1", (body.email.strip().lower(),)).fetchone()
        try:
            if not person:
                raise ValueError()
            _passwords.verify(person["password_hash"], body.password)
        except (ValueError, VerifyMismatchError, InvalidHashError):
            raise ValueError("邮箱或密码不正确。") from None
        if person["org_id"] != invite["org_id"] or person["role"] != invite["role"]:
            raise ValueError("账号已属于其他机构或角色，不能通过邀请自动转移。")
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(UTC) + timedelta(hours=12)).isoformat(timespec="seconds")
        db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (_hash_token(token), person["id"], expires, now()))
    return store.get_user(person["id"]), token


def clear_identity_cookies(response, student_cookie="taxpearls_training_session"):
    for cookie in dict.fromkeys(
        ("taxpearls_session", "taxpearls_staff_session", "taxpearls_training_session", student_cookie)
    ):
        response.delete_cookie(cookie, path="/")


def teaching_page(store, request, *, staff=False, student_cookie="taxpearls_training_session"):
    """Old teaching bookmarks enter the existing application shell."""
    identity = session_identity(store, request)
    destination = "/#panel=examPanel" if identity and identity.get("education_role") else "/"
    return RedirectResponse(destination, headers={"Cache-Control": "no-store"})


def user_context(store, user):
    """Workspace selection is derived from the authenticated identity mapping."""
    with store.connect() as db:
        org = db.execute("SELECT org_type FROM org_settings WHERE org_id=?", (user["org_id"],)).fetchone()
        organization_type = org["org_type"] if org else None
        link = db.execute(
            "SELECT staff_id,student_id FROM university_identity_links WHERE user_id=?", (user["id"],)
        ).fetchone()
        if not link:
            return {**user, "workspace": None, "org_type": organization_type, "education_role": None}
        table, profile_id = (
            ("college_user", link["staff_id"]) if link["staff_id"] else ("student_info", link["student_id"])
        )
        profile = db.execute(f"SELECT * FROM {table} WHERE id=?", (profile_id,)).fetchone()
        if not profile or not profile["active"]:
            return None
    return {
        **user,
        "workspace": "/training-staff" if link["staff_id"] else "/training-certificates",
        "org_type": organization_type,
        "education_role": profile["role"] if link["staff_id"] else "student",
    }


def profile_context(profile, staff):
    # Legacy profiles retain their existing identity; login never merges accounts by name/email.
    return {
        "id": ("staff-" if staff else "student-") + profile["id"],
        "username": profile["username"] if staff else profile["student_no"],
        "display_name": profile["display_name"] if staff else profile["name"],
        "role": "teacher" if staff else "student",
        "org_id": profile["college"],
        "active": 1,
        "workspace": "/training-staff" if staff else "/training-certificates",
        "org_type": "university",
        "education_role": profile["role"] if staff else "student",
        "legacy_teaching": True,
    }


def session_identity(store, request):
    user = store.user_for_token(request.cookies.get("taxpearls_session"))
    if user:
        return user_context(store, user)
    from webapp.training_portal import staff_for_token, student_for_token

    staff = staff_for_token(store, request.cookies.get("taxpearls_staff_session"))
    student = student_for_token(store, request.cookies.get("taxpearls_training_session"))
    if bool(staff) == bool(student):
        return None
    return profile_context(staff or student, bool(staff))


def authenticate_legacy(store, identifier, password, remember=False):
    """Resolve older teaching credentials through the same login API, without role input."""
    from webapp.storage import _passwords
    from webapp.training_portal import login_staff, login_student

    normalized = identifier.strip().lower()
    verified = []
    with store.connect() as db:
        # Preserve the canonical account registry's email precedence, even for disabled users.
        if db.execute("SELECT 1 FROM users WHERE email=? OR username=?", (normalized, normalized)).fetchone():
            return None
        for staff, table, column in ((True, "college_user", "username"), (False, "student_info", "student_no")):
            rows = db.execute(
                f"SELECT * FROM {table} WHERE ({column}=? OR lower(email)=?) AND active=1",
                (identifier.strip(), normalized),
            ).fetchall()
            for row in rows:
                scope = {"staff_id" if staff else "student_id": row["id"]}
                if not identity_active(db, **scope):
                    continue
                try:
                    _passwords.verify(identity_hash(db, row["password_hash"], **scope), password)
                except (VerifyMismatchError, InvalidHashError):
                    continue
                verified.append((staff, dict(row)))
    if len(verified) > 1:
        raise ValueError("账号对应多个教学档案，请使用唯一邮箱或联系管理员核对。")
    if not verified:
        return None
    staff, profile = verified[0]
    result = (
        login_staff(store, profile["username"], password, remember=remember)
        if staff
        else login_student(store, profile["student_no"], password, college=profile["college"], remember=remember)
    )
    if not result:
        return None
    return {"staff": staff, "profile": profile, "user": profile_context(profile, staff), "token": result["token"]}


def logout_browser(store, request, response, student_cookie="taxpearls_training_session"):
    """Revoke this browser's cookies across entry points, preserving other devices."""
    from webapp.storage import _hash_token

    with store.connect() as db:
        for cookie, table in (
            ("taxpearls_session", "sessions"),
            ("taxpearls_staff_session", "training_staff_sessions"),
            ("taxpearls_training_session", "training_student_sessions"),
            (student_cookie, "training_student_sessions"),
        ):
            token = request.cookies.get(cookie)
            if token:
                db.execute(f"DELETE FROM {table} WHERE token_hash=?", (_hash_token(token),))
    clear_identity_cookies(response, student_cookie)
    response.delete_cookie("taxpearls_email_browser", path="/")


def attach_platform_session(
    store, response, profile_id, *, staff=False, remember=False, student_cookie="taxpearls_training_session"
):
    """Align main login with an authenticated teaching profile; never infer legacy links."""
    from webapp.storage import _hash_token, session_max_age
    import os

    clear_identity_cookies(response, student_cookie)
    column = "staff_id" if staff else "student_id"
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        person = db.execute(
            f"""SELECT u.id FROM university_identity_links l JOIN users u ON u.id=l.user_id
            WHERE l.{column}=? AND u.active=1""",
            (profile_id,),
        ).fetchone()
        if not person:
            return
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(UTC) + timedelta(seconds=session_max_age(remember))).isoformat(timespec="seconds")
        db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (_hash_token(token), person["id"], expires, now()))
    response.set_cookie(
        "taxpearls_session",
        token,
        max_age=session_max_age(remember),
        httponly=True,
        samesite="strict",
        secure=os.getenv("TAXPEARLS_COOKIE_SECURE") == "1",
        path="/",
    )


def attach_session(store, response, user, *, remember=False):
    from webapp.storage import _hash_token, session_max_age

    response.delete_cookie("taxpearls_staff_session", path="/")
    response.delete_cookie("taxpearls_training_session", path="/")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        link = db.execute("SELECT * FROM university_identity_links WHERE user_id=?", (user["id"],)).fetchone()
        current = db.execute("SELECT active FROM users WHERE id=?", (user["id"],)).fetchone()
        if not current or not current[0]:
            return None
        if not link:
            return None
        if link["staff_id"]:
            table, column, cookie, destination = (
                "training_staff_sessions",
                "staff_id",
                "taxpearls_staff_session",
                "/training-staff",
            )
            valid = db.execute("SELECT active FROM college_user WHERE id=?", (link["staff_id"],)).fetchone()
        else:
            table, column, cookie, destination = (
                "training_student_sessions",
                "student_id",
                "taxpearls_training_session",
                "/training-certificates",
            )
            valid = db.execute("SELECT active FROM student_info WHERE id=?", (link["student_id"],)).fetchone()
        if not valid or not valid[0]:
            return None
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(UTC) + timedelta(seconds=session_max_age(remember))).isoformat(timespec="seconds")
        db.execute(f"INSERT INTO {table} VALUES (?,?,?,?)", (_hash_token(token), link[column], expires, now()))
    import os

    response.set_cookie(
        cookie,
        token,
        max_age=session_max_age(remember),
        httponly=True,
        samesite="strict",
        secure=os.getenv("TAXPEARLS_COOKIE_SECURE") == "1",
        path="/",
    )
    response.headers["X-TaxPearls-Workspace"] = destination
    return destination


class AllocationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seats: int = Field(ge=0, le=100000, strict=True)
    revision: int = Field(ge=0, strict=True)


class LinkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(pattern="^(teacher|student)$")


class LinkChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)
    active: StrictBool | None = None


class AssignBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    teacher_id: str = Field(min_length=1, max_length=64)


class CapacityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    teacher_quota: int = Field(ge=1, le=100000, strict=True)
    student_quota: int = Field(ge=0, le=100000, strict=True)
    revision: int = Field(ge=0, strict=True)


class JoinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invite_code: str = Field(min_length=1, max_length=128)
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=128)
    code: str = Field(default="", max_length=16)
    display_name: str = Field(default="", max_length=100)
    student_no: str = Field(default="", max_length=80)
    existing: StrictBool = False
    accepted: StrictBool


def register(app, store_provider, user_for_session):
    from webapp.training_portal import STAFF_COOKIE, staff_for_token
    from webapp.login_guard import RateLimiter

    limiter = RateLimiter({"ip": (30, 60), "email": (10, 900)})
    preview_limiter = RateLimiter({"ip": (120, 60)})

    @app.get("/api/training/workspace/template/{kind}")
    def teaching_template(kind: str):
        from webapp.training_portal import STATIC_DIR

        if kind not in {"staff", "student"}:
            raise HTTPException(404, "页面不存在。")
        filename = "training-staff.html" if kind == "staff" else "training-portal.html"
        return FileResponse(STATIC_DIR / filename, media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/training-workspace.js")
    def teaching_workspace_script():
        from webapp.training_portal import STATIC_DIR

        return FileResponse(STATIC_DIR / "training-workspace.js", media_type="text/javascript")

    def actor(session):
        who = staff_for_token(store_provider(), session)
        if not who:
            raise HTTPException(401, "请先登录教师工作台。")
        return who

    @app.get("/api/training/membership")
    def membership(session: str | None = Cookie(default=None, alias=STAFF_COOKIE)):
        return overview(store_provider(), actor(session))

    @app.post("/api/training/membership/session")
    def bridge(session: str | None = Cookie(default=None, alias="taxpearls_session")):
        response = JSONResponse({"ok": True})
        if not attach_session(store_provider(), response, user_for_session(session)):
            raise HTTPException(403, "账号未关联有效教学身份。")
        return response

    @app.put("/api/training/membership/teachers/{teacher_id}/quota")
    def allocation(
        teacher_id: str, body: AllocationBody, session: str | None = Cookie(default=None, alias=STAFF_COOKIE)
    ):
        return allocate(store_provider(), actor(session), teacher_id, body.seats, body.revision)

    @app.put("/api/training/membership/students/{student_id}/teacher")
    def assignment(student_id: str, body: AssignBody, session: str | None = Cookie(default=None, alias=STAFF_COOKIE)):
        return assign_legacy(store_provider(), actor(session), student_id, body.teacher_id)

    @app.get("/api/training/membership/links")
    def list_links(session: str | None = Cookie(default=None, alias=STAFF_COOKIE)):
        return links(store_provider(), actor(session))

    @app.get("/api/training/membership/students")
    def own_students(session: str | None = Cookie(default=None, alias=STAFF_COOKIE)):
        who = actor(session)
        from webapp.training_portal import list_students

        return {"students": list_students(store_provider(), who["college"], teacher_id=who["id"])}

    @app.post("/api/training/membership/links")
    def create_link(body: LinkBody, session: str | None = Cookie(default=None, alias=STAFF_COOKIE)):
        return mutate_link(store_provider(), actor(session), role=body.role)

    @app.put("/api/training/membership/links/{link_id}")
    def update_link(link_id: str, body: LinkChange, session: str | None = Cookie(default=None, alias=STAFF_COOKIE)):
        return mutate_link(
            store_provider(), actor(session), link_id=link_id, revision=body.revision, active=body.active
        )

    @app.get("/api/universities")
    def schools(session: str | None = Cookie(default=None, alias="taxpearls_session")):
        who = user_for_session(session)
        with store_provider().connect() as db:
            current_actor(db, who, {"platform_admin"})
            colleges = [r[0] for r in db.execute("SELECT DISTINCT college FROM college_user ORDER BY college")]
            output = []
            for college in colleges:
                org = db.execute(
                    """SELECT o.display_name FROM university_organizations u JOIN org_settings o
                    ON o.org_id=u.org_id WHERE u.college=?""",
                    (college,),
                ).fetchone()
                output.append({**school_state(db, college), "name": org[0] if org else college})
            return output

    @app.put("/api/universities/{college}/capacity")
    def capacity(
        college: str, body: CapacityBody, session: str | None = Cookie(default=None, alias="taxpearls_session")
    ):
        who = user_for_session(session)
        with store_provider().connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_actor(db, who, {"platform_admin"})
            if not db.execute("SELECT 1 FROM college_user WHERE college=?", (college,)).fetchone():
                raise AccessDenied()
            old = school_state(db, college)
            if old["revision"] != body.revision:
                raise AccessDenied("学校额度已变化，请刷新后重试。", 409)
            if body.teacher_quota < old["teachers_used"] or body.student_quota < max(
                old["students_used"], old["allocated"] + old["unassigned_students"]
            ):
                raise AccessDenied("额度不能低于当前教职工、已分配额度及待认领学生占用。", 422)
            db.execute(
                """INSERT INTO college_quota(college,teacher_quota,student_quota,revision,updated_at)
                VALUES (?,?,?,?,?) ON CONFLICT(college) DO UPDATE SET teacher_quota=excluded.teacher_quota,
                student_quota=excluded.student_quota,revision=college_quota.revision+1,updated_at=excluded.updated_at""",
                (college, body.teacher_quota, body.student_quota, body.revision + 1, now()),
            )
            db.execute(
                "INSERT INTO university_capacity_events VALUES (?,?,?,?,?,?,?,?)",
                (
                    secrets.token_hex(12),
                    college,
                    who["id"],
                    body.teacher_quota,
                    body.student_quota,
                    now(),
                    old["teacher_quota"] if old["configured"] else None,
                    old["student_quota"] if old["configured"] else None,
                ),
            )
            return school_state(db, college)

    @app.get("/api/join/{code}")
    def join_preview(code: str, request: Request):
        if len(code) > 128 or not preview_limiter.allow(ip=request.client.host if request.client else "unknown"):
            raise HTTPException(429, "请求过于频繁，请稍后重试。")
        try:
            return JSONResponse(
                preview(store_provider(), code), headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/join")
    def join(body: JoinBody, request: Request):
        if not body.accepted:
            raise HTTPException(422, "请确认加入所显示的机构及角色。")
        if not limiter.allow(ip=request.client.host if request.client else "unknown", email=body.email.strip().lower()):
            raise HTTPException(429, "请求过于频繁，请稍后重试。")
        try:
            user, token = redeem(store_provider(), body, request.cookies.get("taxpearls_email_browser", ""))
        except (ValueError, sqlite3.IntegrityError) as exc:
            detail = "账号或学号已存在，请联系管理员核对。" if isinstance(exc, sqlite3.IntegrityError) else str(exc)
            raise HTTPException(422, detail) from None
        import os

        response = JSONResponse({"user": user})
        response.set_cookie(
            "taxpearls_session",
            token,
            max_age=12 * 3600,
            httponly=True,
            samesite="strict",
            secure=os.getenv("TAXPEARLS_COOKIE_SECURE") == "1",
            path="/",
        )
        destination = attach_session(store_provider(), response, user) or "/"
        # Build JSON after deciding the workspace; preserve all cookies.
        result = JSONResponse({"destination": destination})
        for key, value in response.raw_headers:
            if key == b"set-cookie":
                result.raw_headers.append((key, value))
        return result

    @app.get("/member-links.js")
    def link_script():
        from webapp.app import STATIC_DIR

        return FileResponse(STATIC_DIR / "member-links.js", media_type="text/javascript")

    @app.get("/member-links.css")
    def link_style():
        from webapp.app import STATIC_DIR

        return FileResponse(STATIC_DIR / "member-links.css", media_type="text/css")

    @app.get("/join.js")
    def join_script():
        from webapp.app import STATIC_DIR

        return FileResponse(STATIC_DIR / "join.js", media_type="text/javascript")
