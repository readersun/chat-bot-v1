# -*- coding: utf-8 -*-
"""
시험 묶음.

    python -m unittest discover tests        전체
    python -m unittest tests.test_ssh_policy  명령 등급만 (즉시 끝난다)

test_relay_flow 와 test_relay_guard 는 import 하는 순간 임시 DB 를 잡고
app 을 띄운다. (config 가 import 시점에 환경변수를 읽기 때문에 그 전에
DATABASE_PATH 를 바꿔 둔다) 그래서 둘을 한 프로세스에서 함께 돌리면 먼저
import 된 쪽의 DB 를 함께 쓴다. 같은 표를 각자 비우고 쓰므로 섞이지 않는다.
"""
