# -*- coding: utf-8 -*-
"""
인크루트 파서 회귀 테스트 — tests/fixtures 의 실제 페이지(2026-11-05 저장, 스크립트 제거)로 검증합니다.
사이트가 개편되면: 새 페이지를 fixtures 에 저장하고 parse_jobs 를 고친 뒤 이 테스트로 확인하세요.
실행: python -m unittest discover -s tests -v   (리포 루트에서)
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DRY_RUN"] = "1"

import incruit_bot  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


class ParserTest(unittest.TestCase):
    def test_list_page(self):
        jobs, rows, bad = incruit_bot.parse_jobs(fixture("incruit_list.html"))
        self.assertEqual((len(jobs), rows, bad), (38, 38, 0))
        for j in jobs:
            self.assertRegex(j["id"], r"^\d{10,}$")
            self.assertTrue(j["title"] and j["company"] and j["location"])
            self.assertEqual(j["link"], "https://job.incruit.com/jobdb_info/jobpost.asp?job=" + j["id"])
            self.assertNotIn("(", j["reg_time"])
        first = jobs[0]     # 최근 등록순 → 가장 최근 공고가 맨 위
        self.assertEqual(first["id"], "2611040003351")
        self.assertEqual(first["reg_time"], "20시간전 등록")

    def test_validate(self):
        kind, (jobs, _, _) = incruit_bot.validate_page(200, fixture("incruit_list.html"))
        self.assertEqual((kind, len(jobs)), ("ok", 38))
        # 진짜 0건(빈 페이지)은 정상 + 행 수 0(부분 구조변경 오경보 방지)
        self.assertEqual(incruit_bot.validate_page(200, fixture("incruit_empty.html")), ("ok", ([], 0, 0)))
        self.assertEqual(incruit_bot.validate_page(403, "x")[0], "blocked")
        self.assertEqual(incruit_bot.validate_page(200, "<html>wifi login</html>")[0], "bad")
        changed = fixture("incruit_list.html").replace("c_row", "x_row")
        self.assertEqual(incruit_bot.validate_page(200, changed)[0], "empty")   # 구조 변경 의심

    def test_fallback_selectors(self):
        base, _, _ = incruit_bot.parse_jobs(fixture("incruit_list.html"))
        # 행의 jobno 속성이 사라져도 상세 링크의 job= 번호로 읽음
        jobs, _, bad = incruit_bot.parse_jobs(fixture("incruit_list.html").replace("jobno=", "data-x="))
        self.assertEqual(bad, 0)
        self.assertEqual([j["id"] for j in jobs], [j["id"] for j in base])

    def test_page_url(self):
        u = incruit_bot.page_url(2)
        self.assertTrue(u.startswith(incruit_bot.TARGET_URL + "&"))
        self.assertIn("&page=2", u)
        self.assertIn("sortfield=reg", u)
        self.assertIn("sortorder=1", u)

    def test_collect_stops_at_known(self):
        page1 = fixture("incruit_list.html")
        empty = fixture("incruit_empty.html")

        class FakeNet(object):
            label = "direct"

            def __init__(self):
                self.calls = []

            def get(self, url, validate, budget=90):
                self.calls.append(url)
                text = page1 if "page=1&" in url else empty
                return validate(200, text)[1], "direct"

        jobs, _, _ = incruit_bot.parse_jobs(page1)
        n = FakeNet()
        out, rows, bad = incruit_bot.collect_jobs(n, {}, {jobs[3]["id"]})
        self.assertEqual(len(n.calls), 1)
        self.assertEqual(len(out), 38)
        n2 = FakeNet()
        out2, rows2, bad2 = incruit_bot.collect_jobs(n2, {}, {"없는-id"})
        self.assertEqual(len(n2.calls), 2)          # 1페이지가 모두 신규 → 2페이지(빈 페이지)에서 멈춤
        self.assertEqual((len(out2), rows2, bad2), (38, 38, 0))

    def test_message_format_unchanged(self):
        job = {"id": "1", "company": "A&B", "location": "서울 외", "title": "<신입>",
               "link": "https://job.incruit.com/jobdb_info/jobpost.asp?job=1", "deadline": "~11.11 (수)",
               "reg_time": "6일전 등록"}
        self.assertEqual(incruit_bot.build_message(job),
                         "<b>A&amp;B - &lt;신입&gt;</b>\n\n• A&amp;B(서울 외)\n"
                         "• <a href='https://job.incruit.com/jobdb_info/jobpost.asp?job=1'><b>&lt;신입&gt;</b></a>\n"
                         "• ~11.11 (수)\n\n본 공고는 6일전 등록됐어요")


if __name__ == "__main__":
    unittest.main(verbosity=2)
