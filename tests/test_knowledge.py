import json
from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import patch, Mock

from fastapi import HTTPException
from src import engine, loader
from src.settings import AISettings
from webapp.knowledge import build_graph, ask_graph

ROOT = Path(__file__).resolve().parents[1]


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.rules = engine.load_rules(ROOT / "rules")
        self.graph = build_graph(self.rules)

    def test_graph_references_and_audit_evidence(self):
        ids = {n["id"] for n in self.graph["nodes"]}
        self.assertTrue(all(e["source"] in ids and e["target"] in ids for e in self.graph["edges"]))
        self.assertFalse(any(n["kind"] == "risk" for n in self.graph["nodes"]))
        data = loader.load(ROOT / "samples/样例企业-审计材料.xlsx")
        findings = engine.run(self.rules, data)
        graph = build_graph([], {"id": "example", "dataset": data, "findings": findings})
        self.assertEqual(len([n for n in graph["nodes"] if n["kind"] == "risk"]), len(findings))
        revenue = next(n for n in graph["nodes"] if n["label"] == "营业收入")
        self.assertEqual(revenue["value"], str(data.get("营业收入")))
        self.assertTrue(any(n["kind"] == "source" for n in graph["nodes"]))

    def test_model_citations_and_config(self):
        settings = AISettings(enabled=True, api_key="synthetic-test-key")
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "answer": "请核对收入口径。", "citations": ["R-001", "invented"]})}}]}
        opener = Mock()
        opener.open.return_value = BytesIO(json.dumps(response).encode())
        with patch("webapp.knowledge.AISettings.from_env", return_value=settings), patch("webapp.knowledge.urllib.request.build_opener", return_value=opener):
            result = ask_graph(self.graph, "R-001", "解释当前规则")
            self.assertEqual([n["id"] for n in result["citations"]], ["R-001"])
            payload = json.loads(opener.open.call_args.args[0].data)
            self.assertEqual(payload["model"], settings.effective_model)
            self.assertIn("R-001", payload["messages"][1]["content"])
        with patch("webapp.knowledge.AISettings.from_env", return_value=AISettings()):
            with self.assertRaises(HTTPException) as error:
                ask_graph(self.graph, "R-001", "解释")
            self.assertEqual(error.exception.status_code, 503)

    def test_reject_unreferenced_model_answer(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"content": '{"answer":"unsupported","citations":["invented"]}'}}]}
        opener = Mock()
        opener.open.return_value = BytesIO(json.dumps(response).encode())
        with patch("webapp.knowledge.AISettings.from_env", return_value=AISettings(enabled=True, api_key="test")), patch("webapp.knowledge.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(HTTPException) as error:
                ask_graph(self.graph, "R-001", "解释")
            self.assertEqual(error.exception.status_code, 502)
