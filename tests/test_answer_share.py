# -*- coding: utf-8 -*-
"""离线验证跨进程 AI 答案共享缓存。"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "yuketang"))

from answer_share import AnswerShareCache, problem_signature  # noqa: E402
import yuketang as yk  # noqa: E402


PROBLEM = {
    "problemType": 1,
    "body": "1 + 1 = ?",
    "options": [{"key": "A", "value": "1"}, {"key": "B", "value": "2"}],
}


class ShareCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="answer_share_test_")
        self.path = os.path.join(self.tmp, "cache.sqlite3")
        self.cache = AnswerShareCache(self.path, lease_seconds=1.0)
        self.key = self.cache.make_key("lesson", "problem", PROBLEM, "model|high")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_leader_wait_hit(self):
        status, lease = self.cache.acquire(self.key, "a")
        self.assertEqual(status, "leader")
        status2, _ = self.cache.acquire(self.key, "b")
        self.assertEqual(status2, "wait")
        self.assertTrue(lease.publish(["B"], "AI 作答", 1))
        status3, payload = self.cache.acquire(self.key, "c")
        self.assertEqual(status3, "hit")
        self.assertEqual(payload["result"], ["B"])

    def test_many_processes_only_one_leader(self):
        cache = AnswerShareCache(self.path, lease_seconds=2.0)
        cache._init_db()
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(name):
            barrier.wait()
            status, payload = cache.acquire(self.key, name)
            with lock:
                results.append((status, payload, name))

        threads = [threading.Thread(target=worker, args=(f"a{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        leaders = [item for item in results if item[0] == "leader"]
        self.assertEqual(len(leaders), 1)
        self.assertTrue(all(item[0] in ("leader", "wait") for item in results))
        leaders[0][1].publish(["B"], "AI 作答", 1)

    def test_lease_expiry_takeover(self):
        cache = AnswerShareCache(self.path, lease_seconds=0.5)
        status, lease = cache.acquire(self.key, "a")
        self.assertEqual(status, "leader")
        lease.stop()
        time.sleep(0.8)
        status2, lease2 = cache.acquire(self.key, "b")
        self.assertEqual(status2, "leader")
        lease2.fail("故意失败")

    def test_failed_then_retry(self):
        status, lease = self.cache.acquire(self.key, "a")
        lease.fail("模型失败", retry_after=0.1)
        time.sleep(0.2)
        status2, lease2 = self.cache.acquire(self.key, "b")
        self.assertEqual(status2, "leader")
        self.assertTrue(lease2.publish(["A"], "AI 作答", 1))

    def test_different_body_hash_not_shared(self):
        key2 = self.cache.make_key(
            "lesson", "problem",
            {"problemType": 1, "body": "2 + 2 = ?", "options": []}, "model|high")
        self.assertNotEqual(self.key, key2)
        s1, l1 = self.cache.acquire(self.key, "a")
        s2, l2 = self.cache.acquire(key2, "b")
        self.assertEqual((s1, s2), ("leader", "leader"))
        l1.fail("x")
        l2.fail("x")

    def test_problem_signature_stable(self):
        self.assertEqual(problem_signature(PROBLEM), problem_signature(dict(PROBLEM)))

    def test_signature_ignores_image_signature_query(self):
        p1 = {"problemType": 1, "body": '看图<img src="https://cdn/a.png?sign=111">',
              "options": []}
        p2 = {"problemType": 1, "body": '看图<img src="https://cdn/a.png?sign=222">',
              "options": []}
        p3 = {"problemType": 1, "body": '看图<img src="https://cdn/b.png?sign=111">',
              "options": []}
        self.assertEqual(problem_signature(p1), problem_signature(p2))
        self.assertNotEqual(problem_signature(p1), problem_signature(p3))

    def test_subjective_result_can_be_cached(self):
        status, lease = self.cache.acquire(self.key, "a")
        self.assertEqual(status, "leader")
        self.assertTrue(lease.publish(
            {"content": "主观题答案", "pics": [{"pic": "", "thumb": ""}]},
            "AI 作答", 5))
        status2, payload = self.cache.acquire(self.key, "b")
        self.assertEqual(status2, "hit")
        self.assertEqual(payload["result"]["content"], "主观题答案")


class ShareAnswerFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="answer_share_flow_")
        self.cache = AnswerShareCache(os.path.join(self.tmp, "cache.sqlite3"),
                                      lease_seconds=2.0)
        self.calls = 0
        self.calls_lock = threading.Lock()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _obj(self, name):
        obj = yk.yuketang.__new__(yk.yuketang)
        obj.name = name
        obj.lessonId = "lesson"
        obj.answer_share_enabled = True
        obj.answer_share_cache = self.cache
        obj.answer_cancelled = False
        obj._answer_cancelled = False
        obj.answer_timeout = 120
        obj.answer_base_url = "https://api.deepseek.com/v1"
        obj.answer_model = "deepseek-flash"
        obj.answer_reasoning_effort = "high"
        obj._problem_deadlines = {"p1": time.time() + 5}
        return obj

    def test_answer_two_accounts_one_ai_call(self):
        calls = []

        def fake_ai(*args, **kwargs):
            calls.append(1)
            time.sleep(0.2)
            return ["B"], "AI 作答"

        def make(name):
            obj = self._obj(name)
            obj.msgmgr = type("M", (), {"sendMsg": lambda self, *a, **k: None})()
            obj.problems = {"p1": PROBLEM}
            obj._problem_deadlines = {"p1": time.time() + 5}
            obj._answer_results = {}
            obj._answer_running = set()
            obj._answer_submitted = set()
            obj._answer_submitting = set()
            obj._answer_retry_events = {}
            obj._reopen_answer_attempts = set()
            obj._answer_state_lock = threading.Lock()
            obj._answer_cancelled = False
            obj.answer_source = "auto"
            obj.answer_ai_enabled = True
            obj.answer_ai_enabled_getter = None
            obj.answer_api_key = "key"
            obj.answer_base_url = "https://api.deepseek.com/v1"
            obj.answer_model = "deepseek-flash"
            obj.answer_reasoning_effort = "high"
            obj.answer_send_images = False
            obj.answer_note = ""
            obj._hint_ai_shown = False
            obj._submit_cached = lambda pid: None
            return obj

        objs = [make("a"), make("b")]
        barrier = threading.Barrier(2)

        def worker(obj):
            barrier.wait()
            obj.answer("p1")

        with mock.patch.object(yk, "ai_solve_problem", fake_ai):
            threads = [threading.Thread(target=worker, args=(obj,)) for obj in objs]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(obj._answer_results["p1"]["result"] == ["B"] for obj in objs))

    def test_two_accounts_one_ai_call(self):
        def compute():
            with self.calls_lock:
                self.calls += 1
            time.sleep(0.2)
            return ["B"], "AI 作答"

        obj1, obj2 = self._obj("a"), self._obj("b")
        results = []
        barrier = threading.Barrier(2)

        def worker(obj):
            barrier.wait()
            results.append(obj._answer_with_share(
                "p1", PROBLEM, time.time() + 5, compute))

        threads = [threading.Thread(target=worker, args=(obj,)) for obj in (obj1, obj2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result[0] == ["B"] for result in results))


if __name__ == "__main__":
    unittest.main()
