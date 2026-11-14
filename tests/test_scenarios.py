# -*- coding: utf-8 -*-
"""组会/面试双场景、通用 provider 与隐私默认值的离线回归。"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml

from brain import Brain
from config import load_config
from contracts import EventBus, KIND_FREE_QA, LANE_DEEP
from scenarios import (INTERVIEW, MEETING, export_prefix, is_interview,
                       normalize_scenario, scenario_key, scenario_label)
from session_store import SessionStore


class TestScenarios(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.example.yaml")

    def test_aliases_and_labels(self):
        for value in ("meeting", "group_meeting", "组会", "会议"):
            self.assertEqual(normalize_scenario(value), MEETING)
        for value in ("interview", "面试"):
            self.assertEqual(normalize_scenario(value), INTERVIEW)
        self.assertEqual(normalize_scenario("unknown"), MEETING)
        self.assertEqual(normalize_scenario("unknown", INTERVIEW), INTERVIEW)
        self.cfg["app"]["scenario"] = "interview"
        self.assertEqual(scenario_key(self.cfg), INTERVIEW)
        self.assertEqual(scenario_label(self.cfg), "面试")
        self.assertEqual(export_prefix(self.cfg), "面试")
        self.assertTrue(is_interview(self.cfg))

    def test_prompts_are_distinct_and_generic(self):
        meeting = Brain(self.cfg, EventBus(), None)
        meeting_prompt = meeting._system_prompt()
        self.cfg["app"]["scenario"] = "interview"
        interview = Brain(self.cfg, EventBus(), None)
        interview_prompt = interview._system_prompt()
        self.assertNotEqual(meeting_prompt, interview_prompt)
        self.assertIn("研究组会", meeting_prompt)
        self.assertIn("面试", interview_prompt)
        self.assertIn("真实依据", interview_prompt)
        for prompt in (meeting_prompt, interview_prompt):
            self.assertNotIn("知识库", prompt)
            self.assertNotIn("项目速览", prompt)
        meeting.close()
        interview.close()

    def test_free_qa_uses_custom_provider_and_model(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["llm"]["free_qa_provider"] = "custom"
        cfg["llm"]["free_qa_model"] = "custom-model"
        cfg["_providers"]["custom"] = {"base_url": "https://example.invalid/v1", "api_key": "test"}
        brain = Brain(cfg, EventBus(), None)
        first = brain._chains(KIND_FREE_QA, LANE_DEEP)[0]
        self.assertEqual((first["provider"], first["model"]), ("custom", "custom-model"))
        brain.close()

    def test_free_qa_defaults_to_primary(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["llm"].pop("free_qa_provider", None)
        cfg["llm"].pop("free_qa_model", None)
        brain = Brain(cfg, EventBus(), None)
        first = brain._chains(KIND_FREE_QA, LANE_DEEP)[0]
        self.assertEqual((first["provider"], first["model"]),
                         (cfg["llm"]["primary"]["provider"], cfg["llm"]["primary"]["model"]))
        brain.close()

    def test_arbitrary_api_key_environment_variable(self):
        payload = {"llm": {"providers": {"custom": {
            "type": "openai", "base_url": "https://example.invalid/v1",
            "api_key_env": "MC_TEST_PROVIDER_KEY"}}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
            with patch.dict(os.environ, {"MC_TEST_PROVIDER_KEY": "not-a-real-secret"}, clear=False):
                cfg = load_config(path)
        self.assertEqual(cfg["_providers"]["custom"]["api_key"], "not-a-real-secret")
        self.assertNotIn("custom", cfg["_missing_keys"])

    def test_interview_export_name_and_default_audio_privacy(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["app"]["scenario"] = "interview"
        with tempfile.TemporaryDirectory() as tmp:
            cfg["_dirs"] = dict(cfg.get("_dirs") or {}, exports=tmp)
            store = SessionStore(cfg)
            self.assertTrue(store.title.startswith("面试_"))
            output = Path(store.export_markdown())
            self.assertTrue(output.name.startswith("面试_"))
            self.assertTrue(output.is_file())
        self.assertFalse(self.cfg["audio"]["record_audio"])
        self.assertEqual(self.cfg["app"]["user_names"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
