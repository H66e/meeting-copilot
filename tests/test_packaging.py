"""Windows 打包路径、隐私边界与模型下载策略的离线回归。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr_engine import AsrEngine


class TestPackaging(unittest.TestCase):
    def test_frozen_paths_separate_user_data_from_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "portable" / "MeetingCopilot.exe"
            resources = Path(tmp) / "_internal"
            code = (
                "import sys;"
                f"sys.frozen=True;sys.executable={str(exe)!r};"
                f"sys._MEIPASS={str(resources)!r};"
                "import config;print(f'{config.PROJECT_DIR}|{config.RESOURCE_DIR}')"
            )
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            project_dir, resource_dir = result.stdout.strip().split("|")
            # Windows 可能返回 8.3 短路径；比较实际位置，而非字符串表示。
            self.assertEqual(Path(project_dir).resolve(), exe.parent.resolve())
            self.assertEqual(Path(resource_dir).resolve(), resources.resolve())

    def test_version_is_consistent(self):
        version = (ROOT / "packaging" / "version.txt").read_text(encoding="utf-8").strip()
        cfg = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["app"]["version"], version)
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")

    def test_spec_only_bundles_public_configuration(self):
        spec = (ROOT / "packaging" / "meeting_copilot.spec").read_text(encoding="utf-8")
        self.assertIn("config.example.yaml", spec)
        self.assertNotIn('ROOT / "config.yaml"', spec)
        self.assertIn('"tools.selfcheck"', spec)
        self.assertIn('"ui_split"', spec)
        self.assertTrue((ROOT / "assets" / "app.ico").is_file())

    def test_missing_cached_model_can_download(self):
        calls = []

        class FakeModel:
            def __init__(self, ref, **kwargs):
                calls.append((ref, kwargs))
                if kwargs["local_files_only"]:
                    raise FileNotFoundError("no cached model")

        fake_module = types.ModuleType("faster_whisper")
        fake_module.WhisperModel = FakeModel
        cfg = {"asr": {"model": "small", "allow_download": True, "cpu_threads": 1}}
        eng = AsrEngine(cfg)
        eng._prompt_exact_tokens = lambda: 0
        eng._max_new_tokens = lambda _audio: 1
        with patch("asr_engine.resolve_model_ref", return_value="Systran/faster-whisper-small"):
            with patch.dict(sys.modules, {"faster_whisper": fake_module}):
                eng.load()
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][1]["local_files_only"])
        self.assertFalse(calls[1][1]["local_files_only"])
        self.assertEqual(calls[1][0], "Systran/faster-whisper-small")
        self.assertTrue(eng.is_ready)

    def test_offline_mode_never_downloads(self):
        calls = []

        class MissingModel:
            def __init__(self, ref, **kwargs):
                calls.append((ref, kwargs))
                raise FileNotFoundError("no cached model")

        fake_module = types.ModuleType("faster_whisper")
        fake_module.WhisperModel = MissingModel
        with patch.dict(os.environ, {"MC_OFFLINE": "1"}):
            eng = AsrEngine({"asr": {"model": "small", "allow_download": True}})
            with patch("asr_engine.resolve_model_ref", return_value="Systran/faster-whisper-small"):
                with patch.dict(sys.modules, {"faster_whisper": fake_module}):
                    with self.assertRaises(RuntimeError):
                        eng.load()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1]["local_files_only"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
