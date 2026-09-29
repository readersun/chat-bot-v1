#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
providers
=========

Claude 호출을 추상화한다. 채팅 로직은 이 인터페이스에만 의존하고,
실제 호출 방식(CLI / API)은 관리자 설정(settings)으로 교체된다.

    ClaudeProvider          인터페이스
      ├── ClaudeCliProvider   claude -p  (현재 기본, 실제 동작)
      └── ClaudeApiProvider   Anthropic Messages API (stdlib urllib 만 사용)

호출부가 알아야 하는 것은 다음 세 가지뿐이다.

    provider.supports_resume()   세션 resume 을 provider 가 직접 지원하는가
    provider.send(...)           질문/이미지/직전 대화를 넘기고 답을 받는다
    provider.describe() / test() 관리자 화면의 상태 표시 / 연결 테스트

resume 을 지원하지 않는 provider 를 골라도 채팅은 그대로 동작한다.
호출부가 DB 의 최근 대화를 history 로 넘겨주기 때문이다.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request

# 오류 메시지에 자격증명이 섞여 나가지 않도록 한 번 걸러낸다.
_SECRET_PAT = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{6,}|Bearer\s+[A-Za-z0-9._\-]{8,}|"
    r"x-api-key[\"'\s:=]+[A-Za-z0-9._\-]{8,})", re.IGNORECASE)


def redact(text):
    if not text:
        return ""
    return _SECRET_PAT.sub("[REDACTED]", str(text))


def result(ok, text, session_id=None, returncode=0):
    return {"ok": ok, "text": text, "session_id": session_id, "returncode": returncode}


# ---------------------------------------------------------------------------
# 인터페이스
# ---------------------------------------------------------------------------
class ClaudeProvider(object):
    name = "base"
    label = "Base"

    def __init__(self, cfg):
        self.cfg = cfg

    def supports_resume(self):
        raise NotImplementedError

    def send(self, question, images=(), history=(), resume_id=None, new_session_id=None):
        """
        question : 사용자 질문 (str)
        images   : [(original_name, absolute_path, mime_type), ...]
        history  : [{"role": "user"|"assistant", "content": str}, ...]
                   provider 가 resume 을 못 쓸 때 문맥으로 사용한다.
        반환     : dict(ok, text, session_id, returncode)
        """
        raise NotImplementedError

    def describe(self):
        """실행 비용이 거의 없는 상태 정보. 관리자 System 화면용."""
        raise NotImplementedError

    def test(self):
        """실제로 짧은 요청을 보내 연결/인증을 확인한다."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Claude Code CLI
# ---------------------------------------------------------------------------
class ClaudeCliProvider(ClaudeProvider):
    name = "cli"
    label = "Claude CLI"

    def supports_resume(self):
        return bool(self.cfg.get("use_resume", True))

    # -- 내부 ---------------------------------------------------------------
    def _binary(self):
        return self.cfg.get("cli_path") or "claude"

    def resolved_path(self):
        """설정된 값이 이름만일 때 PATH 에서 찾아준다. 표시용."""
        binary = self._binary()
        if os.path.sep in binary or (os.altsep and os.altsep in binary):
            return binary if os.path.exists(binary) else None
        return shutil.which(binary)

    def _run(self, args, timeout=None):
        return subprocess.run(
            args,
            cwd=self.cfg.get("workdir") or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout or self.cfg.get("timeout", 180),
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def build_prompt(self, question, images=(), history=()):
        parts = []
        if history:
            lines = ["이전 대화:", ""]
            for m in history:
                lines.append("USER:" if m["role"] == "user" else "ASSISTANT:")
                lines.append(m["content"])
                lines.append("")
            parts.append("\n".join(lines))
        if images:
            lines = ["첨부된 이미지 파일 %d개:" % len(images)]
            for i, img in enumerate(images, 1):
                lines.append("%d. %s -> %s" % (i, img[0], img[1].replace("\\", "/")))
            lines.append("")
            lines.append("위 이미지 파일을 Read 도구로 열어서 확인한 뒤 답해줘.")
            parts.append("\n".join(lines))
        parts.append("사용자 질문:\n\n" + question)
        return "\n\n".join(parts)

    def _call(self, prompt, resume_id=None, new_session_id=None, extra_dirs=()):
        # 주의: --add-dir 는 가변 인자(<directories...>)라서 바로 뒤에 플래그가 와야 한다.
        #       그렇지 않으면 마지막 프롬프트까지 디렉터리로 먹어버린다.
        args = [self._binary()] + list(self.cfg.get("extra_args") or []) + ["-p"]
        for d in extra_dirs:
            if d:
                args += ["--add-dir", d]
        args += ["--output-format", "json"]
        if resume_id:
            args += ["--resume", resume_id]
        elif new_session_id:
            args += ["--session-id", new_session_id]
        args.append(prompt)

        timeout = self.cfg.get("timeout", 180)
        try:
            proc = self._run(args)
        except FileNotFoundError:
            return result(False, "Claude CLI 를 찾을 수 없습니다: %s\n"
                                 "`which claude` 로 경로를 확인한 뒤 관리자 > Claude 설정에서 "
                                 "CLI 경로를 수정하세요." % self._binary(), returncode=-1)
        except NotADirectoryError:
            return result(False, "Working Directory 경로가 올바르지 않습니다: %s"
                          % self.cfg.get("workdir"), returncode=-1)
        except PermissionError as exc:
            return result(False, "Claude CLI 실행 권한이 없습니다: %s" % exc, returncode=-1)
        except subprocess.TimeoutExpired:
            return result(False, "시간 초과(%d초)되어 Claude 실행을 중단했습니다." % timeout,
                          returncode=-1)
        except OSError as exc:
            return result(False, "Claude 실행 중 오류가 발생했습니다: %s" % exc, returncode=-1)

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        if proc.returncode != 0:
            msg = "Claude 실행 실패 (exit code %d)" % proc.returncode
            if stderr:
                msg += "\n\n" + stderr
            elif stdout:
                msg += "\n\n" + stdout
            return result(False, redact(msg), returncode=proc.returncode)

        try:
            data = json.loads(stdout)
        except ValueError:
            if not stdout:
                return result(False, "Claude 가 빈 응답을 반환했습니다."
                              + (("\n\n" + redact(stderr)) if stderr else ""))
            return result(True, stdout)

        text = data.get("result")
        if not isinstance(text, str):
            text = json.dumps(data, ensure_ascii=False)[:2000]
        if data.get("is_error"):
            return result(False, redact(text) or "Claude 가 오류를 반환했습니다.",
                          session_id=data.get("session_id"))
        if not text.strip():
            return result(False, "Claude 가 빈 응답을 반환했습니다.",
                          session_id=data.get("session_id"))
        return result(True, text, session_id=data.get("session_id"))

    # -- 인터페이스 ---------------------------------------------------------
    def send(self, question, images=(), history=(), resume_id=None, new_session_id=None):
        prompt = self.build_prompt(question, images, history)
        # 업로드 루트를 항상 허용해 준다. (이미지가 없어도 인자 모양을 바꾸지 않는다)
        extra_dirs = [self.cfg.get("upload_dir")] if self.cfg.get("upload_dir") else []
        return self._call(prompt, resume_id=resume_id, new_session_id=new_session_id,
                          extra_dirs=extra_dirs)

    def version(self):
        try:
            proc = self._run([self._binary(), "--version"], timeout=20)
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        return (proc.stdout or "").strip().splitlines()[0] if proc.stdout else None

    def describe(self):
        path = self.resolved_path()
        ver = self.version() if path else None
        return {
            "provider": self.name,
            "label": self.label,
            "cli_path": self._binary(),
            "resolved_path": path,
            "found": bool(path),
            "version": ver,
            "workdir": self.cfg.get("workdir") or "(서버 작업 디렉터리)",
            "timeout": self.cfg.get("timeout"),
            "max_concurrent": self.cfg.get("max_concurrent"),
            "supports_resume": self.supports_resume(),
        }

    def test(self):
        info = self.describe()
        if not info["found"]:
            return {"ok": False, "info": info,
                    "error": "Claude CLI 실행 파일을 찾을 수 없습니다: %s" % self._binary()}
        res = self._call("Respond only with OK")
        info["authenticated"] = bool(res["ok"])
        if res["ok"]:
            return {"ok": True, "info": info, "reply": res["text"].strip()[:200]}
        return {"ok": False, "info": info, "error": redact(res["text"])}


# ---------------------------------------------------------------------------
# Anthropic Messages API
# ---------------------------------------------------------------------------
class ClaudeApiProvider(ClaudeProvider):
    name = "api"
    label = "Claude API"

    # API 에는 CLI 같은 세션 resume 이 없다. 호출부가 history 를 넘겨준다.
    def supports_resume(self):
        return False

    def _headers(self):
        return {
            "x-api-key": self.cfg.get("api_key") or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def _content_blocks(self, question, images):
        blocks = []
        for img in images:
            name, path = img[0], img[1]
            mime = img[2] if len(img) > 2 else "image/png"
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
            except OSError as exc:
                blocks.append({"type": "text",
                               "text": "(첨부 %s 를 읽지 못했습니다: %s)" % (name, exc)})
                continue
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime,
                           "data": base64.b64encode(raw).decode("ascii")},
            })
        blocks.append({"type": "text", "text": question})
        return blocks

    def _post(self, payload, timeout=None):
        if not self.cfg.get("api_key"):
            return None, "API Key 가 설정되지 않았습니다. 관리자 > Claude 설정에서 입력하세요."
        url = self.cfg.get("api_base_url", "").rstrip("/") + "/v1/messages"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(
                    req, timeout=timeout or self.cfg.get("timeout", 180)) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
                detail = body.get("error", {}).get("message") or json.dumps(body)[:500]
            except Exception:
                detail = exc.reason
            return None, redact("API 오류 (HTTP %s): %s" % (exc.code, detail))
        except urllib.error.URLError as exc:
            return None, redact("API 서버에 연결할 수 없습니다: %s" % exc.reason)
        except Exception as exc:  # 타임아웃, JSON 파싱 등
            return None, redact("API 호출 실패: %s" % exc)

    def send(self, question, images=(), history=(), resume_id=None, new_session_id=None):
        messages = []
        for m in history or ():
            role = "user" if m["role"] == "user" else "assistant"
            messages.append({"role": role, "content": m["content"]})
        messages.append({"role": "user", "content": self._content_blocks(question, images)})

        data, err = self._post({
            "model": self.cfg.get("api_model"),
            "max_tokens": 8192,
            "messages": messages,
        })
        if err:
            return result(False, err, returncode=-1)
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        if not text.strip():
            return result(False, "Claude 가 빈 응답을 반환했습니다.")
        # API 는 세션 개념이 없으므로 session_id 를 돌려주지 않는다.
        return result(True, text)

    def describe(self):
        return {
            "provider": self.name,
            "label": self.label,
            "api_base_url": self.cfg.get("api_base_url"),
            "api_model": self.cfg.get("api_model"),
            "has_api_key": bool(self.cfg.get("api_key")),
            "timeout": self.cfg.get("timeout"),
            "max_concurrent": self.cfg.get("max_concurrent"),
            "supports_resume": False,
        }

    def test(self):
        info = self.describe()
        if not info["has_api_key"]:
            return {"ok": False, "info": info, "error": "API Key 가 설정되지 않았습니다."}
        data, err = self._post({
            "model": self.cfg.get("api_model"),
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Respond only with OK"}],
        }, timeout=30)
        if err:
            info["authenticated"] = False
            return {"ok": False, "info": info, "error": err}
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        info["authenticated"] = True
        return {"ok": True, "info": info, "reply": text.strip()[:200]}


# ---------------------------------------------------------------------------
# 선택
# ---------------------------------------------------------------------------
PROVIDERS = {p.name: p for p in (ClaudeCliProvider, ClaudeApiProvider)}


def build(cfg):
    """settings_store.snapshot() 결과로 provider 인스턴스를 만든다."""
    cls = PROVIDERS.get((cfg.get("provider") or "cli").lower(), ClaudeCliProvider)
    return cls(cfg)
