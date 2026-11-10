# -*- coding: utf-8 -*-
"""离线验证答题延长/提交重试逻辑，不访问雨课堂或大模型。"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "yuketang"))

import yuketang as yk  # noqa: E402


class Msg:
    def __init__(self):
        self.messages = []

    def sendMsg(self, text, user=None):
        self.messages.append(str(text))


class FakeEvent:
    def __init__(self):
        self.waits = []
        self.is_set_flag = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.is_set_flag

    def clear(self):
        self.is_set_flag = False

    def set(self):
        self.is_set_flag = True


def make_obj():
    obj = yk.yuketang.__new__(yk.yuketang)
    obj.name = "test"
    obj.msgmgr = Msg()
    obj.lessonId = "lesson"
    obj.cookie = "cookie"
    obj.Authorization = ""
    obj.problems = {}
    obj.problemid = ""
    obj._problem_deadlines = {}
    obj._problem_deadline_extended = set()
    obj._answer_results = {}
    obj._answer_running = set()
    obj._answer_submitted = set()
    obj._answer_submitting = set()
    obj._answer_retry_events = {}
    obj._reopen_answer_attempts = set()
    obj._answer_state_lock = threading.Lock()
    obj._answer_tasks = set()
    obj._answer_cancelled = False
    obj.answer_source = "auto"
    obj.answer_ai_enabled = True
    obj.answer_ai_enabled_getter = None
    obj.answer_share_enabled = False
    obj.answer_share_cache = None
    obj.answer_api_key = "key"
    obj.answer_base_url = "https://api.deepseek.com/v1"
    obj.answer_model = "deepseek-flash"
    obj.answer_timeout = 120
    obj.answer_reasoning_effort = "high"
    obj.answer_send_images = False
    obj.answer_note = ""
    obj._hint_ai_shown = False
    return obj


def cache(obj, pid="p1", result=None):
    obj._answer_results[pid] = {
        "result": result or ["A"],
        "problem_type": 1,
        "type_name": "单选题",
        "source": "AI 作答",
        "dropped": 0,
    }


class AnswerSubmitTest(unittest.TestCase):
    def test_retry_then_success(self):
        obj = make_obj()
        cache(obj)
        event = FakeEvent()
        obj._answer_retry_events["p1"] = event
        obj._post_answer_once = mock.Mock(side_effect=[
            ("finished", "LESSON_PROBLEM_FINISHED", None),
            ("ok", "OK", None),
        ])
        obj._submit_cached("p1")
        self.assertEqual(obj._post_answer_once.call_count, 2)
        self.assertEqual(event.waits, [yk.ANSWER_SUBMIT_RETRY_DELAY])
        self.assertIn("p1", obj._answer_submitted)
        self.assertTrue(any("答题成功" in m for m in obj.msgmgr.messages))

    def test_three_finished_stops(self):
        obj = make_obj()
        cache(obj)
        event = FakeEvent()
        obj._answer_retry_events["p1"] = event
        obj._post_answer_once = mock.Mock(side_effect=[
            ("finished", "LESSON_PROBLEM_FINISHED", None),
            ("finished", "LESSON_PROBLEM_FINISHED", None),
            ("finished", "LESSON_PROBLEM_FINISHED", None),
        ])
        obj._submit_cached("p1")
        self.assertEqual(obj._post_answer_once.call_count, 3)
        self.assertEqual(event.waits, [yk.ANSWER_SUBMIT_RETRY_DELAY] * 2)
        self.assertNotIn("p1", obj._answer_submitted)
        self.assertTrue(any("已提交 3 次" in m for m in obj.msgmgr.messages))

    def test_already_answered_is_terminal(self):
        obj = make_obj()
        cache(obj)
        event = FakeEvent()
        obj._answer_retry_events["p1"] = event
        obj._post_answer_once = mock.Mock(
            return_value=("already", "LESSON_PROBLEM_ALREADY_ANSWERED", None))
        obj._submit_cached("p1")
        self.assertEqual(obj._post_answer_once.call_count, 1)
        self.assertEqual(event.waits, [])
        self.assertIn("p1", obj._answer_submitted)
        self.assertTrue(any("已经作答过" in m for m in obj.msgmgr.messages))

    def test_event_wakes_retry_immediately(self):
        obj = make_obj()
        cache(obj)
        event = threading.Event()
        event.set()                       # 写相当于延长事件先到
        obj._answer_retry_events["p1"] = event
        obj._post_answer_once = mock.Mock(side_effect=[
            ("finished", "LESSON_PROBLEM_FINISHED", None),
            ("ok", "OK", None),
        ])
        started = time.time()
        obj._submit_cached("p1")
        self.assertLess(time.time() - started, 1.0)
        self.assertEqual(obj._post_answer_once.call_count, 2)


class EventAndDeadlineTest(unittest.TestCase):
    def test_extendtime_updates_deadline_and_resubmits_cache(self):
        obj = make_obj()
        now = time.time()
        # 有效死线 60 秒，现在相当于已经过了 35 秒
        obj._problem_deadlines["p1"] = now + 25
        cache(obj)
        obj._schedule_cached_submit = mock.Mock()
        obj._schedule_answer = mock.Mock()
        event = FakeEvent()
        obj._answer_retry_events["p1"] = event
        obj._handle_extendtime({"prob": "p1", "extend": 30})
        # 服务端死线 = now - 5；max(now, now-5) + 30 = now + 30
        self.assertAlmostEqual(obj._problem_deadlines["p1"], now + 30, delta=2)
        self.assertTrue(event.is_set_flag)
        obj._schedule_cached_submit.assert_called_once_with("p1")
        obj._schedule_answer.assert_not_called()

        # 第二次延长：已经不再含 +30，直接在服务端死线上加
        obj._handle_extendtime({"prob": "p1", "extend": 30})
        self.assertAlmostEqual(obj._problem_deadlines["p1"], now + 60, delta=2)

    def test_extendtime_without_limit_is_ignored(self):
        obj = make_obj()
        obj._schedule_cached_submit = mock.Mock()
        obj._schedule_answer = mock.Mock()
        obj._handle_extendtime({"prob": "p1", "extend": 30})
        self.assertNotIn("p1", obj._problem_deadlines)
        obj._schedule_cached_submit.assert_not_called()
        obj._schedule_answer.assert_not_called()

    def test_unlockproblem_new_and_cached(self):
        obj = make_obj()
        now = time.time()
        obj._schedule_answer = mock.Mock()
        obj._schedule_cached_submit = mock.Mock()
        obj._handle_unlockproblem(
            {"prob": "p1", "limit": 30, "sendTime": int(now)})
        obj._schedule_answer.assert_called_once_with("p1")
        self.assertAlmostEqual(obj._problem_deadlines["p1"], now + 60, delta=3)

        obj2 = make_obj()
        obj2._schedule_answer = mock.Mock()
        obj2._schedule_cached_submit = mock.Mock()
        obj2._problem_deadlines["p1"] = time.time() + 60
        cache(obj2)
        obj2._handle_unlockproblem({"prob": "p1", "limit": 30, "sendTime": int(now)})
        obj2._schedule_cached_submit.assert_called_once_with("p1")
        obj2._schedule_answer.assert_not_called()

    def test_extendtime_reopens_ai_once(self):
        obj = make_obj()
        obj._problem_deadlines["p1"] = time.time() + 25
        obj._schedule_cached_submit = mock.Mock()
        obj._schedule_answer = mock.Mock()
        obj._handle_extendtime({"prob": "p1", "extend": 30})
        obj._handle_extendtime({"prob": "p1", "extend": 30})
        self.assertEqual(obj._schedule_answer.call_count, 1)
        obj._schedule_cached_submit.assert_not_called()

    def test_answer_passes_inflight_grace_to_ai(self):
        obj = make_obj()
        now = time.time()
        obj._problem_deadlines["p1"] = now + 60
        obj.problems["p1"] = {
            "problemType": 1,
            "body": "test",
            "options": [{"key": "A", "value": ""}, {"key": "B", "value": ""}],
        }
        captured = {}

        def fake_ai(problem, api_key, base_url, model, timeout, images=None,
                    deadline=None, reasoning_effort=None):
            captured["deadline"] = deadline
            return ["A"], "AI 作答"

        obj._submit_cached = mock.Mock()
        with mock.patch.object(yk, "ai_solve_problem", fake_ai):
            obj.answer("p1")
        self.assertIsNotNone(captured["deadline"])
        self.assertAlmostEqual(captured["deadline"], now + 90, delta=2)
        self.assertIn("p1", obj._answer_results)
        self.assertEqual(obj._answer_results["p1"]["result"], ["A"])
        obj._submit_cached.assert_called_once_with("p1")


if __name__ == "__main__":
    unittest.main()
