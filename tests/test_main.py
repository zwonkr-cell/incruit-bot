# -*- coding: utf-8 -*-
"""
main() 동작 테스트 — 전송 기록·시간 상한·장애 시 하트비트·불필요한 state 변화 방지.
네트워크/텔레그램은 가짜로 바꿔 끼우고 임시 폴더에서 실행합니다(인터넷 불필요).
실행: python -m unittest discover -s tests -v   (리포 루트에서)
"""
import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DRY_RUN", "1")   # 다른 테스트와 같은 프로세스에서 import 될 때를 대비(아래에서 끔)

import incruit_bot as bot  # noqa: E402
from netkit import FetchError  # noqa: E402


def make_jobs(n):
    # 목록은 최신순(앞이 최신) → 봇은 뒤(오래된 것)부터 보냄
    return [{"id": str(9000 + i), "company": "회사%d" % i, "location": "서울", "title": "공고%d" % i,
             "link": "https://example.com/%d" % i, "deadline": "~12/01", "reg_time": "1시간 전 등록"}
            for i in range(n)]


class FakeNet(object):
    def __init__(self, *a, **k):
        self.label = "direct"

    def finalize(self):
        pass

    def summary(self):
        return "[net] fake"


class _Time(object):
    """time 모듈 대신: sleep 은 건너뛰고 monotonic 은 그대로."""
    monotonic = staticmethod(time.monotonic)

    @staticmethod
    def sleep(s):
        pass


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.saved = {k: getattr(bot, k) for k in
                      ("DRY_RUN", "Net", "collect_jobs", "send_telegram", "send_plain", "log_to_sheet",
                       "time", "SEND_BUDGET_SEC")}
        bot.DRY_RUN = False
        bot.Net = FakeNet
        bot.time = _Time
        bot.log_to_sheet = lambda p: None
        self.plain = []
        bot.send_plain = self.plain.append

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(bot, k, v)
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_state(self, state):
        with open(bot.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)

    def read_state(self):
        with open(bot.STATE_FILE, encoding="utf-8") as f:
            return json.load(f)

    def read_ids(self):
        with open(bot.DB_FILE, encoding="utf-8") as f:
            return f.read().split()

    def seed(self, jobs, state=None):
        with open(bot.DB_FILE, "w", encoding="utf-8") as f:
            f.write("1\n2\n3")
        self.write_state(state if state is not None else
                         {"last_activity_at": bot._now_iso(), "consec_err": {}, "transport": "direct"})
        bot.collect_jobs = lambda net, state, processed: (jobs, len(jobs), 0)

    # ── 보내자마자 기록: 다음 공고를 보내는 시점에 앞 공고가 이미 파일에 있어야 함
    def test_sent_ids_saved_immediately(self):
        jobs = make_jobs(3)
        self.seed(jobs)
        on_disk = []

        def send(msg):
            on_disk.append(self.read_ids())
            return True
        bot.send_telegram = send
        self.assertEqual(bot.main(), 0)
        oldest, middle = jobs[2]["id"], jobs[1]["id"]
        self.assertNotIn(oldest, on_disk[0])          # 첫 전송 전엔 아직 없음
        self.assertIn(oldest, on_disk[1])             # 두 번째 전송 시점엔 첫 공고가 이미 저장됨
        self.assertIn(middle, on_disk[2])
        self.assertEqual(self.read_ids()[:3], [jobs[0]["id"], jobs[1]["id"], jobs[2]["id"]])

    # ── 실행이 도중에 끊겨도(KeyboardInterrupt = 러너의 취소 신호) 보낸 공고는 남음
    def test_interrupt_keeps_sent(self):
        jobs = make_jobs(3)
        self.seed(jobs)
        calls = []

        def send(msg):
            calls.append(msg)
            if len(calls) == 2:
                raise KeyboardInterrupt()
            return True
        bot.send_telegram = send
        with self.assertRaises(KeyboardInterrupt):
            bot.main()
        self.assertIn(jobs[2]["id"], self.read_ids())
        self.assertNotIn(jobs[1]["id"], self.read_ids())

    # ── 텔레그램 장애: 연속 실패 MAX_SEND_FAILS 회면 멈추고 나머지는 다음 실행으로
    def test_stop_after_consecutive_failures(self):
        jobs = make_jobs(5)
        self.seed(jobs)
        calls = []
        bot.send_telegram = lambda msg: calls.append(msg) or False
        bot.main()
        self.assertEqual(len(calls), bot.MAX_SEND_FAILS)
        self.assertEqual(self.read_ids(), ["1", "2", "3"])

    # ── 실패 1번 뒤 성공하면 계속 보냄(한 건만 문제인 경우)
    def test_single_failure_continues(self):
        jobs = make_jobs(3)
        self.seed(jobs)
        results = iter([False, True, True])
        bot.send_telegram = lambda msg: next(results)
        bot.main()
        ids = self.read_ids()
        self.assertNotIn(jobs[2]["id"], ids)
        self.assertIn(jobs[1]["id"], ids)
        self.assertIn(jobs[0]["id"], ids)

    # ── 전송 시간 상한을 넘기면 새 전송을 시작하지 않음
    def test_send_budget(self):
        jobs = make_jobs(3)
        self.seed(jobs)
        bot.SEND_BUDGET_SEC = -1
        calls = []
        bot.send_telegram = lambda msg: calls.append(msg) or True
        bot.main()
        self.assertEqual(calls, [])
        self.assertEqual(self.read_ids(), ["1", "2", "3"])

    # ── 접속 장애가 이어지면: 오류 노티는 없지만(정책) 24시간 하트비트는 감. state 에 오류 카운터를 쌓지 않음
    def test_outage_heartbeat_and_no_state_churn(self):
        old = (datetime.now(bot.KST) - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
        self.seed([], {"last_activity_at": old, "consec_err": {}, "transport": "direct"})

        def boom(net, state, processed):
            raise FetchError("network", "인크루트 접속 실패(직접+프록시 모두) - 사이트 접근 불가")
        bot.collect_jobs = boom
        bot.main()
        self.assertEqual(len(self.plain), 1)
        self.assertIn("최근 24시간 내 새로 스크래핑된 채용공고가 없어요", self.plain[0])
        st = self.read_state()
        self.assertEqual(st["consec_err"], {})
        self.assertNotEqual(st["last_activity_at"], old)
        # 바로 다음 실패 실행: 노티도 state 변화도 없음
        before = self.read_state()
        bot.main()
        self.assertEqual(len(self.plain), 1)
        self.assertEqual(self.read_state(), before)

    # ── 연결 방식은 종류만 저장(프록시 주소가 바뀌어도 state 그대로)
    def test_transport_kind_only(self):
        self.seed([], {"last_activity_at": bot._now_iso(), "consec_err": {}, "transport": "proxy:1.1.1.1:80"})

        class ProxyNet(FakeNet):
            def __init__(self, *a, **k):
                self.label = "proxy:2.2.2.2:8080"
        bot.Net = ProxyNet
        bot.main()
        self.assertEqual(self.read_state()["transport"], "proxy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
