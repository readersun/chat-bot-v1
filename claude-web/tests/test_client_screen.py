# -*- coding: utf-8 -*-
"""
claude-term 의 「이 화면을 Claude 에게」 가 붙이는 글자.

PuTTY 의 Copy All 은 스크롤백 전체를 준다. 그중 무엇이 질문에 붙는지, 그리고
대화에 다시 그릴 때 화면 전체 대신 칩만 보이는지를 본다. 화면(tkinter)과
Win32 는 여기서 부르지 않는다.
"""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "claude_term_screen", os.path.join(ROOT, "client_app", "claude_term.py"))
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)


class TestScreenTail(unittest.TestCase):
    def test_keeps_only_the_last_lines_and_drops_blank_rows_below_the_prompt(self):
        lines = ["line %03d" % i for i in range(1, 201)]
        text = "\r\n".join(lines) + "\r\nuser@nfs-181:~$ \r\n\r\n\r\n"
        body, n = ct.screen_tail(text)
        self.assertEqual(n, 60)
        rows = body.split("\n")
        self.assertEqual(rows[-1], "user@nfs-181:~$")
        self.assertEqual(rows[0], "line 142")

    def test_control_characters_are_removed(self):
        body, n = ct.screen_tail("ok\x1b[32m green\x07\x9b end\r\n")
        self.assertEqual(n, 1)
        self.assertNotIn("\x1b", body)
        self.assertNotIn("\x07", body)
        self.assertNotIn("\x9b", body)
        self.assertIn("ok", body)

    def test_korean_survives(self):
        body, _n = ct.screen_tail("한글 화면\r\n")
        self.assertEqual(body, "한글 화면")

    def test_long_screens_are_cut_from_the_top_to_fit_the_question_limit(self):
        text = "\n".join("x" * 190 for _ in range(60))
        body, n = ct.screen_tail(text)
        self.assertLessEqual(len(body), ct.SCREEN_MAX_CHARS)
        self.assertLess(n, 60)
        self.assertEqual(len(body.split("\n")), n)

    def test_empty(self):
        self.assertEqual(ct.screen_tail(""), ("", 0))
        self.assertEqual(ct.screen_tail("\r\n\r\n   \r\n"), ("", 0))


class TestQuestionWithScreen(unittest.TestCase):
    shot = {"name": "nfs-181", "at": "14:02:11", "lines": 2,
            "text": "$ docker compose restart app\nError: port is already allocated"}

    def test_round_trip_shows_a_chip_not_the_screen(self):
        text = ct.compose_question("왜 재시작이 안 돼?", self.shot)
        self.assertIn("```\n$ docker compose restart app", text)
        meta, rest = ct.split_screen(text)
        self.assertEqual(meta, {"name": "nfs-181", "at": "14:02:11", "lines": 2})
        self.assertEqual(rest, "왜 재시작이 안 돼?")

    def test_screen_is_an_unlabelled_block_so_it_is_never_run(self):
        # 챗봇은 ```ssh 블록만 명령으로 본다. 화면 안의 ``` 가 블록을 깨면 안 된다.
        shot = dict(self.shot, text="```ssh\nrm -rf /\n```")
        text = ct.compose_question("이게 뭐야?", shot)
        self.assertNotIn("```ssh", text)
        self.assertEqual(text.count("```"), 2)
        meta, rest = ct.split_screen(text)
        self.assertIsNotNone(meta)
        self.assertEqual(rest, "이게 뭐야?")

    def test_plain_questions_pass_through(self):
        self.assertEqual(ct.compose_question("그냥 질문"), "그냥 질문")
        self.assertEqual(ct.split_screen("그냥 질문"), (None, "그냥 질문"))


if __name__ == "__main__":
    unittest.main()
