# -*- coding: utf-8 -*-
"""组会助手 · UI / 热键 / 自检纯逻辑单元测试（无显示器断言级）

运行：
    python tests/test_ui_hotkeys.py
    python -m unittest tests.test_ui_hotkeys -v     # 如果 tests 是包
    python -m unittest discover -s tests -p "test_ui_hotkeys.py"

注意：本文件不创建 Tk 窗口（CI/无桌面环境不会卡死）；真实窗口渲染用
``python ui_overlay.py --selftest`` 单独手动跑。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import hotkeys  # noqa: E402
import ui_overlay  # noqa: E402
from contracts import (KIND_ANSWER, KIND_HINT, KIND_MINUTES, KIND_SLIDE, KIND_SUMMARY,
                       LANE_DEEP, LANE_FAST, BrainEvent, EventBus, Status,
                       TranscriptSegment, fmt_ts)  # noqa: E402


# ================================================================ 纯函数（无 Tk）
class TestFormatters(unittest.TestCase):
    def test_format_transcript_line(self):
        self.assertEqual(ui_overlay.format_transcript_line(754, "你好"), "[12:34] 你好")
        self.assertEqual(ui_overlay.format_transcript_line(0, "  你好  "), "[00:00] 你好")
        self.assertEqual(ui_overlay.format_transcript_line(-5, "x"), "[00:00] x")
        self.assertEqual(fmt_ts(65.9), "01:05")

    def test_rtf_text(self):
        self.assertEqual(ui_overlay.rtf_text(0.0), "RTF -")
        self.assertEqual(ui_overlay.rtf_text(0.13), "RTF 0.13")
        self.assertEqual(ui_overlay.rtf_text(1.239), "RTF 1.24")
        self.assertEqual(ui_overlay.rtf_text(None), "RTF -")

    def test_status_text(self):
        self.assertEqual(ui_overlay.status_text(False, False, False), "待机")
        self.assertEqual(ui_overlay.status_text(False, True, True), "已暂停")
        self.assertEqual(ui_overlay.status_text(True, False, True), "模型加载中")
        self.assertEqual(ui_overlay.status_text(True, True, True), "聆听中")

    def test_display_width_and_truncate(self):
        self.assertEqual(ui_overlay.display_width("abc"), 3)
        self.assertEqual(ui_overlay.display_width("中文"), 4)
        self.assertEqual(ui_overlay.display_width("中文ab"), 6)
        self.assertEqual(ui_overlay.truncate_text("hello world", 5), "hell…")
        self.assertEqual(ui_overlay.truncate_text("abcdef", 20), "abcdef")
        self.assertEqual(ui_overlay.truncate_text("中文中文中文", 4), "中…")
        self.assertEqual(ui_overlay.truncate_text("", 10), "")

    def test_short_device(self):
        self.assertEqual(ui_overlay.short_device(""), "默认设备")
        self.assertEqual(ui_overlay.short_device(None), "默认设备")
        # 去括号形式：把驱动细节接到设备名后面
        self.assertEqual(ui_overlay.short_device("扬声器 (Realtek(R) Audio)", limit=40),
                         "扬声器·Realtek(R) Audio")
        self.assertEqual(ui_overlay.short_device("Speakers (X)", limit=40), "Speakers·X")
        # 默认 limit=18 会截断
        self.assertTrue(ui_overlay.short_device("扬声器 (Realtek(R) Audio)").endswith("…"))
        long_name = "扬声器·Razer BlackShark V2 HS 2.4"
        result = ui_overlay.short_device(long_name, limit=18)
        self.assertLessEqual(ui_overlay.display_width(result), 18)
        self.assertTrue(result.endswith("…"))


class TestCardStyles(unittest.TestCase):
    def test_title_for(self):
        self.assertEqual(ui_overlay.title_for(KIND_ANSWER)[0], "应答思路")
        self.assertEqual(ui_overlay.title_for(KIND_HINT)[0], "要点")
        self.assertEqual(ui_overlay.title_for(KIND_SLIDE)[0], "屏幕解读")
        self.assertEqual(ui_overlay.title_for(KIND_SUMMARY)[0], "阶段小结")
        self.assertEqual(ui_overlay.title_for(KIND_MINUTES)[0], "最终纪要")
        self.assertEqual(ui_overlay.title_for("unknown")[0], "建议")
        self.assertEqual(ui_overlay.title_for(KIND_ANSWER)[1], ui_overlay.C_WARM)

    def test_card_style_deep_answer_more_prominent(self):
        answer = ui_overlay.card_style(KIND_ANSWER, LANE_DEEP)
        hint = ui_overlay.card_style(KIND_HINT, LANE_FAST)
        self.assertEqual(answer["title"], "应答思路")
        self.assertGreater(answer["title_size"], hint["title_size"])
        self.assertGreater(answer["body_size"], hint["body_size"])
        self.assertGreater(answer["accent"], hint["accent"])

    def test_lane_badge(self):
        self.assertEqual(ui_overlay.lane_badge(LANE_DEEP), "深度")
        self.assertEqual(ui_overlay.lane_badge(LANE_FAST), "快速")
        self.assertEqual(ui_overlay.lane_badge(""), "")


class TestMonitorAnchor(unittest.TestCase):
    def test_primary_top_right(self):
        # 主屏 2560×1440、工作区高 1380、窗口 565×975、边距 20（125% 缩放后的物理像素）
        x, y = ui_overlay.anchor_top_right((0, 0, 2560, 1380), (565, 975), 20)
        self.assertEqual((x, y), (1975, 20))

    def test_secondary_negative_and_positive(self):
        # 副屏在主屏右侧
        x, y = ui_overlay.anchor_top_right((2560, 0, 4693, 1273), (565, 975), 20)
        self.assertEqual((x, y), (4108, 20))
        # 副屏在主屏左侧（负坐标）
        x, y = ui_overlay.anchor_top_right((-1920, 0, 0, 1080), (400, 780), 16)
        self.assertEqual((x, y), (-416, 16))

    def test_clamp_inside_workarea(self):
        # 窗口比工作区还高/宽时被夹到左上角
        x, y = ui_overlay.anchor_top_right((0, 0, 500, 400), (800, 1000), 16)
        self.assertEqual(x, 0)
        self.assertEqual(y, 0)

    def test_pick_monitor(self):
        monitors = [
            {"monitor": (0, 0, 2560, 1440), "work": (0, 0, 2560, 1380), "primary": True},
            {"monitor": (2560, 0, 4693, 1333), "work": (2560, 0, 4693, 1273), "primary": False},
        ]
        self.assertTrue(ui_overlay.pick_monitor(monitors, "primary")["primary"])
        self.assertFalse(ui_overlay.pick_monitor(monitors, "secondary")["primary"])
        only_primary = [monitors[0]]
        # 没有副屏时回退主屏
        self.assertTrue(ui_overlay.pick_monitor(only_primary, "secondary")["primary"])
        self.assertIsNone(ui_overlay.pick_monitor([], "primary"))


class TestTodoExtraction(unittest.TestCase):
    def test_checkbox_prefix(self):
        text = "阶段小结：进展正常。\n□ 补做消融实验\n□ 整理人工评估 protocol"
        self.assertEqual(ui_overlay.extract_todos(text),
                         ["补做消融实验", "整理人工评估 protocol"])

    def test_markdown_checkbox(self):
        text = "- [ ] 重划分数据集\n- [x] 已完成的不要紧\n[ ] 写周报"
        todos = ui_overlay.extract_todos(text)
        self.assertIn("重划分数据集", todos)
        self.assertIn("写周报", todos)

    def test_section_marker(self):
        text = "本阶段结论……\n待办：\n1. 定评估协议\n2. 补 baseline 对照\n\n结束。"
        todos = ui_overlay.extract_todos(text)
        self.assertEqual(todos[0], "定评估协议")
        self.assertIn("补 baseline 对照", todos)

    def test_inline_after_marker(self):
        text = "TODO: 先修 dataloader"
        self.assertIn("先修 dataloader", ui_overlay.extract_todos(text))

    def test_dedup_and_empty(self):
        text = "□ 同一件事\n□ 同一件事\n"
        self.assertEqual(ui_overlay.extract_todos(text), ["同一件事"])
        self.assertEqual(ui_overlay.extract_todos(""), [])
        self.assertEqual(ui_overlay.extract_todos("普通段落，没有待办。"), [])


# ================================================================ 卡片状态机
class TestCardStore(unittest.TestCase):
    def _ev(self, rid="r1", **kw):
        params = {"request_id": rid, "kind": KIND_ANSWER, "lane": LANE_DEEP}
        params.update(kw)
        return BrainEvent(**params)

    def test_streaming_append_same_card(self):
        store = ui_overlay.CardStore()
        card, is_new = store.put(self._ev(delta="应答"))
        self.assertTrue(is_new)
        self.assertEqual(card.text, "应答")
        card2, is_new2 = store.put(self._ev(delta="思路"))
        self.assertFalse(is_new2)
        self.assertIs(card, card2)
        self.assertEqual(card.text, "应答思路")
        self.assertEqual(len(store), 1)

    def test_done_and_full_text(self):
        store = ui_overlay.CardStore()
        store.put(self._ev(delta="半句"))
        card, _ = store.put(self._ev(done=True, full_text="完整的一段回答文本"))
        self.assertTrue(card.done)
        self.assertEqual(card.text, "完整的一段回答文本")
        self.assertEqual(card.display_title, "应答思路")
        self.assertFalse(card.is_minutes_kind)

    def test_error(self):
        store = ui_overlay.CardStore()
        card, _ = store.put(self._ev(request_id="err", kind=KIND_HINT, lane=LANE_FAST,
                                     delta="", error="请求超时", done=True))
        self.assertTrue(card.done)
        self.assertEqual(card.error, "请求超时")

    def test_minutes_kind_routing(self):
        store = ui_overlay.CardStore()
        card, _ = store.put(self._ev(request_id="s1", kind=KIND_SUMMARY, lane=LANE_DEEP))
        self.assertTrue(card.is_minutes_kind)
        self.assertEqual(card.display_title, "阶段小结")

    def test_title_override_from_event(self):
        store = ui_overlay.CardStore()
        card, _ = store.put(self._ev(title="老师提问"))
        self.assertEqual(card.display_title, "老师提问")

    def test_order_and_eviction(self):
        store = ui_overlay.CardStore(max_cards=3)
        for i in range(5):
            store.put(self._ev(request_id=f"r{i}", delta=f"第{i}条"))
        self.assertEqual(len(store), 3)
        self.assertNotIn("r0", store)
        self.assertIn("r4", store)
        self.assertEqual([c.request_id for c in store.cards()], ["r2", "r3", "r4"])

    def test_done_before_delta(self):
        store = ui_overlay.CardStore()
        card, _ = store.put(self._ev(request_id="x", done=True, full_text="全文"))
        self.assertTrue(card.done)
        self.assertEqual(card.text, "全文")

    def test_empty_request_id_still_works(self):
        store = ui_overlay.CardStore()
        card, _ = store.put(self._ev(request_id="", delta="x"))
        self.assertTrue(card.request_id)
        self.assertEqual(len(store), 1)


# ================================================================ 提问停顿合并
class TestQuestionMerge(unittest.TestCase):
    def setUp(self):
        import threading
        from main import App
        self.app = App.__new__(App)
        self.app.cfg = {"app": {"user_names": ["小明"]},
                        "brain": {"question_merge_s": 2.0, "question_keywords": ["吗", "？"]}}
        self.app.brain = object()
        self.app.session = type("Session", (), {"recent_text": lambda _s: "最近讨论"})()
        self.app._question_lock = threading.RLock()
        self.app._pending_question = ""
        self.app._pending_question_deadline = 0.0
        self.app._last_answer = 0.0
        self.app._last_seg_ts = 0.0
        self.app._last_partial_text = ""
        self.app._last_term = 0.0
        self.app._notify = lambda *_a, **_kw: None
        self.calls = []
        self.app._enqueue_answer_pair = lambda payload, context, **kw: self.calls.append((payload, kw))

    @staticmethod
    def seg(text):
        return TranscriptSegment(seg_id=1, t_start=0, t_end=1, text=text)

    def test_continuation_resets_deadline_and_answers_once(self):
        self.app._after_transcript(self.seg("这个方案的收益大吗？"), now=100.0)
        self.assertFalse(self.app._flush_pending_question(now=101.0))
        self.app._after_transcript(self.seg("有没有控制计算量的对照？"), now=101.2)
        self.assertFalse(self.app._flush_pending_question(now=103.0))
        self.assertTrue(self.app._flush_pending_question(now=103.21))
        self.assertFalse(self.app._flush_pending_question(now=104.0))
        self.assertEqual(len(self.calls), 1)
        self.assertIn("收益大吗", self.calls[0][0])
        self.assertIn("控制计算量", self.calls[0][0])

    def test_non_question_continuation_is_merged(self):
        self.app._after_transcript(self.seg("这个结果可信吗？"), now=100.0)
        self.app._after_transcript(self.seg("尤其是人工评价那一部分。"), now=100.8)
        self.app._flush_pending_question(now=102.81)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("人工评价", self.calls[0][0])

    def test_name_trigger_is_immediate_and_cancels_pending(self):
        self.app._after_transcript(self.seg("这个指标合理吗？"), now=100.0)
        self.app._after_transcript(self.seg("小明，你来说一下。"), now=100.5)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.app._pending_question, "")
        self.assertTrue(self.calls[0][1]["force_script"])

    def test_zero_window_keeps_immediate_behavior(self):
        self.app.cfg["brain"]["question_merge_s"] = 0
        self.app._after_transcript(self.seg("这个方案可行吗？"), now=100.0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.app._pending_question, "")


# ================================================================ 热键解析
class TestHotkeyParsing(unittest.TestCase):
    def test_normalize_valid(self):
        self.assertEqual(hotkeys.normalize_hotkey("F8"), "f8")
        self.assertEqual(hotkeys.normalize_hotkey("Ctrl+F10"), "ctrl+f10")
        self.assertEqual(hotkeys.normalize_hotkey("control + f9"), "ctrl+f9")
        self.assertEqual(hotkeys.normalize_hotkey("Shift+Alt+F6"), "shift+alt+f6")
        self.assertEqual(hotkeys.normalize_hotkey("ctrl+shift+F12"), "ctrl+shift+f12")
        self.assertEqual(hotkeys.normalize_hotkey("space"), "space")
        self.assertEqual(hotkeys.normalize_hotkey("esc"), "esc")
        self.assertEqual(hotkeys.normalize_hotkey("escape"), "esc")
        self.assertEqual(hotkeys.normalize_hotkey("f24"), "f24")

    def test_modifier_order_canonical(self):
        self.assertEqual(hotkeys.normalize_hotkey("alt+ctrl+f8"), "ctrl+alt+f8")
        self.assertEqual(hotkeys.normalize_hotkey("win+shift+f8"), "shift+windows+f8")

    def test_normalize_invalid(self):
        for bad in ("", "   ", None, 123, "ctrl+", "+f8", "ctrl+ctrl+f1", "hyper+f1",
                    "f25", "f0", "ctrl+unknownkey"):
            with self.assertRaises(ValueError, msg=f"应拒绝 {bad!r}"):
                hotkeys.normalize_hotkey(bad)

    def test_parse_bindings_from_config(self):
        cfg = {"hotkeys": {"toggle_listen": "f8", "capture_slide": "Ctrl+F9",
                           "summary": "", "quit": "ctrl+ctrl+f1"}}
        parsed = hotkeys.parse_bindings(cfg)
        self.assertEqual(parsed, {"toggle_listen": "f8", "capture_slide": "ctrl+f9"})

    def test_command_mapping(self):
        self.assertEqual(hotkeys.command_for_config_key("capture_slide"), "screenshot")
        self.assertEqual(hotkeys.command_for_config_key("toggle_visible"), "hide")
        self.assertEqual(hotkeys.command_for_config_key("export_minutes"), "export")
        self.assertEqual(hotkeys.command_for_config_key("toggle_listen"), "toggle_listen")
        self.assertEqual(hotkeys.command_for_config_key("未知"), "未知")

    def test_build_bindings(self):
        cfg = {"hotkeys": {"toggle_listen": "f8", "capture_slide": "f9",
                           "toggle_visible": "f7", "freeze": "f3"}}
        fired: list[str] = []
        binds = hotkeys.build_bindings(cfg, fired.append)
        self.assertEqual(set(binds), {"f8", "f9", "f7", "f3"})
        binds["f8"]()
        binds["f9"]()
        binds["f7"]()
        binds["f3"]()
        self.assertEqual(fired, ["toggle_listen", "screenshot", "hide", "freeze"])

    def test_manager_start_without_keyboard_does_not_raise(self):
        # 只验证构造 + stop 幂等；不真正注册（避免 CI 无键盘权限时不稳定）
        manager = hotkeys.HotkeyManager({"f8": lambda: None})
        self.assertFalse(manager.is_running)
        self.assertEqual(manager.registered, {})
        manager.stop()
        manager.stop()

    def test_manager_bad_bindings_all_fail_gracefully(self):
        manager = hotkeys.HotkeyManager({"not+a+valid+key": lambda: None, "ctrl+ctrl+f1": lambda: None})
        ok = manager.start()
        self.assertFalse(ok)
        self.assertEqual(len(manager.failed), 2)
        manager.stop()


# ================================================================ 事件总线
class TestEventBusContract(unittest.TestCase):
    def test_emit_and_consume(self):
        bus = EventBus()
        seg = TranscriptSegment(seg_id=1, t_start=1.0, t_end=2.0, text="你好")
        bus.emit_transcript(seg)
        self.assertIs(bus.transcripts.get_nowait(), seg)
        ev = BrainEvent(request_id="a", kind=KIND_HINT, lane=LANE_FAST, delta="x")
        bus.emit_brain(ev)
        self.assertEqual(bus.brain_events.get_nowait().request_id, "a")
        bus.emit_status(Status(listening=True))
        self.assertTrue(bus.status.get_nowait().listening)
        bus.command("screenshot", zoom=2)
        cmd = bus.commands.get_nowait()
        self.assertEqual(cmd["cmd"], "screenshot")
        self.assertEqual(cmd["zoom"], 2)
        self.assertFalse(bus.stopping)
        bus.request_stop()
        self.assertTrue(bus.stopping)

    def test_transcript_display_text(self):
        seg = TranscriptSegment(seg_id=1, t_start=0, t_end=1, text="繁体", text_fixed="简体")
        self.assertEqual(seg.display_text, "简体")
        seg2 = TranscriptSegment(seg_id=2, t_start=0, t_end=1, text="原文")
        self.assertEqual(seg2.display_text, "原文")


# ================================================================ 自检工具（tools/selfcheck.py）
class TestSelfcheckHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib
        cls.selfcheck = importlib.import_module("tools.selfcheck")

    def test_display_width_pad(self):
        sc = self.selfcheck
        self.assertEqual(sc.display_width("中文"), 4)
        self.assertEqual(sc.pad_display("中文", 6), "中文  ")
        self.assertEqual(sc.display_width(sc.pad_display("abc", 5)), 5)
        self.assertEqual(sc.truncate_display("中文中文", 4), "中…")
        self.assertEqual(sc.truncate_display("短", 10), "短")

    def test_render_table_contains_status_and_names(self):
        sc = self.selfcheck
        results = [
            sc.Result("ok", "Python 解释器", "3.14.5"),
            sc.Result("warn", "知识库", "索引为空"),
            sc.Result("fail", "依赖", "缺少 numpy"),
        ]
        table = sc.render_table(results)
        self.assertIn("✅", table)
        self.assertIn("⚠️", table)
        self.assertIn("❌", table)
        self.assertIn("Python 解释器", table)
        self.assertIn("知识库", table)
        # 每行都有统一缩进对齐（2 个空格分列）
        for line in table.splitlines()[2:]:
            self.assertIn("  ", line)

    def test_result_line(self):
        sc = self.selfcheck
        result = sc.Result("ok", "磁盘空间", "剩余 100GB", 1.2)
        line = result.line()
        self.assertIn("✅", line)
        self.assertIn("磁盘空间", line)
        self.assertIn("[1.2s]", line)

    def test_root_points_to_project(self):
        sc = self.selfcheck
        self.assertTrue((sc._ROOT / "config.yaml").exists() or
                        (sc._ROOT / "config.example.yaml").exists())
        self.assertTrue((sc._ROOT / "contracts.py").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
