"""Teacher-only rule-driven case creation; no provider or real-data input."""
from __future__ import annotations

from typing import Literal

from fastapi import Cookie, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from hashlib import sha256
from pydantic import BaseModel, ConfigDict, Field

from src import exercise_generator, exercise_materials
from src.models import Company, Dataset


class GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str = Field(pattern=r"^R-\d{3}$")
    expected_version: str = Field(min_length=1,max_length=32)
    seed: int = Field(default=1,ge=0,le=2147483647,strict=True)
    level: Literal["near","normal","obvious"] = "normal"
    year: int = Field(default=2026,ge=2000,le=2099,strict=True)


def register(app, store_provider, user_for_session, allow, audit_for_user, audit_rules, save_audit, answer_payload, cookie_name):
    def teacher(session):
        user=user_for_session(session)
        allow(user,"teacher")
        return user

    def period_rules(year):
        data=Dataset(Company("仿真", "TEST", "仿真", str(year)),[],{}, {})
        return audit_rules(data,store_provider().enabled_rule_ids())

    def original(audit_id,user):
        entry=audit_for_user(audit_id,user)
        try:
            metadata=store_provider().get_generated_exercise(audit_id,user)
            if not metadata:
                raise HTTPException(404,"生成案例不存在。")
            answer=sorted(f.rule.id for f in entry["findings"] if f.hit)
            rules=[f.rule for f in entry["findings"] if f.rule.id in metadata["rule_versions"]]
            if (metadata["standard_answer"] != answer
                    or exercise_generator.case_digest(entry["dataset"],rules) != metadata["case_sha256"]):
                raise ValueError("出题记录与冻结答案不一致，请核查备份。")
            return {"audit_id":audit_id,"company_name":entry["dataset"].company.name,
                    "metadata":metadata,"answers":[answer_payload(f,audit_id) for f in entry["findings"]]}
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from None

    @app.get("/api/exercises/rules")
    def catalog(year: int = Query(default=2026,ge=2000,le=2099),
                session: str | None = Cookie(default=None,alias=cookie_name)):
        teacher(session)
        return JSONResponse({"year":year,"rules":[{"id":r.id,"name":r.name,"version":r.version,
                                                   "inputs":r.inputs,"logic":r.logic} for r in period_rules(year)]},
                            headers={"Cache-Control":"private, no-store"})

    @app.post("/api/exercises")
    def create(body: GenerateBody,session: str | None = Cookie(default=None,alias=cookie_name)):
        user=teacher(session);rules=period_rules(body.year)
        selected=next((r for r in rules if r.id==body.rule_id),None)
        if not selected:
            raise HTTPException(422,"所选规则未启用或不属于本期规则，未生成案例。")
        if selected.version != body.expected_version:
            raise HTTPException(409,"规则版本已变化，请刷新出题规则后重试。")
        try:
            exercise=exercise_generator.generate(rules,body.rule_id,body.seed,body.level,body.year)
            result=save_audit(exercise.dataset,user,frozen_rules=rules,exercise_metadata=exercise.metadata)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None
        store_provider().log(user,"generate_exercise","audit",result["audit_id"],
                             f"requested={body.rule_id};version={selected.version};seed={body.seed};level={body.level}")
        return JSONResponse(original(result["audit_id"],user),headers={"Cache-Control":"private, no-store"})

    @app.get("/api/exercises/{audit_id}")
    def detail(audit_id: str,session: str | None = Cookie(default=None,alias=cookie_name)):
        user=teacher(session)
        result=original(audit_id,user)
        store_provider().log(user,"view_exercise_answer","audit",audit_id)
        return JSONResponse(result,headers={"Cache-Control":"private, no-store"})

    def download(audit_id, user, assignment_id=None):
        try:
            dataset = store_provider().get_generated_material(audit_id, user, assignment_id)
            if dataset is None:
                raise HTTPException(404, "仿真材料不存在或作业不可访问。")
            content = exercise_materials.export(dataset)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        store_provider().log(user, "download_exercise_material", "audit", audit_id)
        return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Cache-Control":"private, no-store", "X-Content-Type-Options":"nosniff",
                                 "X-Material-SHA256":sha256(content).hexdigest(),
                                 "Content-Disposition":'attachment; filename="exercise-material.xlsx"'})

    @app.get("/api/exercises/{audit_id}/materials")
    def teacher_material(audit_id: str, session: str | None = Cookie(default=None, alias=cookie_name)):
        return download(audit_id, teacher(session))

    @app.get("/api/assignments/{assignment_id}/materials")
    def student_material(assignment_id: str, session: str | None = Cookie(default=None, alias=cookie_name)):
        user = user_for_session(session)
        allow(user, "teacher", "student")
        assignment = store_provider().get_assignment_for_user(assignment_id,user)
        if not assignment:
            raise HTTPException(404, "作业不存在。")
        return download(assignment["audit_id"], user, assignment_id)
