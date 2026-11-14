# -*- coding: utf-8 -*-
"""Supported live-assistant scenarios.

Scenario affects prompts and export names only. Local transcription, answer history,
free-form Q&A and optional knowledge memory behave the same in both modes.
"""
from __future__ import annotations

from config import get

MEETING = "meeting"
INTERVIEW = "interview"
SCENARIOS = (MEETING, INTERVIEW)
_LABELS = {MEETING: "组会", INTERVIEW: "面试"}
_ALIASES = {
    "meeting": MEETING, "group_meeting": MEETING, "group-meeting": MEETING,
    "组会": MEETING, "会议": MEETING,
    "interview": INTERVIEW, "面试": INTERVIEW,
}


def normalize_scenario(value: object, default: str = MEETING) -> str:
    key = str(value or "").strip().lower()
    return _ALIASES.get(key, default if default in SCENARIOS else MEETING)


def scenario_key(cfg: dict) -> str:
    return normalize_scenario(get(cfg, "app.scenario", MEETING))


def scenario_label(cfg: dict) -> str:
    return _LABELS[scenario_key(cfg)]


def export_prefix(cfg: dict) -> str:
    return scenario_label(cfg)


def is_interview(cfg: dict) -> bool:
    return scenario_key(cfg) == INTERVIEW
