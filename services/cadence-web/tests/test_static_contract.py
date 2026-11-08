"""SP5 静态契约测试。

SP5 是原生 HTML/JS，没有构建步骤，所以先用"读文件 + 断言 DOM id / API 字符串"
覆盖那些**不需要浏览器就能判定**的契约；真正交互留给 `e2e.js` / `l5_manual.js`。

L5 起对话区、SSE、Trace 抽屉、diff 三组、三栏布局和空状态示例都已落地，
原先的 7 条 xfail 已转为正式断言。
"""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "main" / "resources" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")
API = (STATIC / "api.js").read_text(encoding="utf-8")
CHAT = (STATIC / "chat.js").read_text(encoding="utf-8")
TRACE = (STATIC / "trace.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# 任务板 / 确认 / API 外壳
# ---------------------------------------------------------------------


def test_static_assets_exist() -> None:
    for name in ("index.html", "app.js", "api.js", "chat.js", "trace.js", "app.css"):
        assert (STATIC / name).is_file(), name


def test_no_external_cdn_or_framework() -> None:
    """PRD NFR-3：零依赖，不引 React/Vue/Tailwind CDN。"""
    for text in (HTML, CHAT, TRACE):
        lowered = text.lower()
        assert "react" not in lowered
        assert "vue" not in lowered
        assert "tailwind" not in lowered
    cleaned = HTML.replace("http://www.w3.org/2000/svg", "").replace("https://www.w3.org", "")
    assert "http://" not in cleaned and "https://" not in cleaned


def test_board_has_three_columns() -> None:
    assert "const COLUMNS" in APP
    for label in ("待办", "进行中", "已完成"):
        assert label in APP
    assert "column" in APP and "board" in HTML


def test_confirm_dialog_exists() -> None:
    assert 'id="confirm-dialog"' in HTML
    assert 'id="confirm-ok"' in HTML
    assert "data-close-confirm" in HTML


def test_delete_is_two_phase_in_api() -> None:
    assert "deleteTaskWithToken" in API
    assert "confirmToken" in API
    assert "e.status === 403" in API


def test_api_envelope_handling() -> None:
    assert "code === 0" in API
    assert "ApiError" in API
    assert "204" in API


def test_demo_mode_exists() -> None:
    assert "DEMO_FLAG" in API
    assert "?demo" in API or "has('demo')" in API


def test_board_crud_endpoints_present() -> None:
    for path in ("/api/v1/tasks", "/api/v1/calendar/busy", "/api/v1/tasks:batch"):
        assert path in API, path


def test_e2e_script_exists() -> None:
    e2e = Path(__file__).resolve().parents[1] / "e2e.js"
    assert e2e.is_file()
    assert "Runtime.evaluate" in e2e.read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# L5：对话 / SSE / Trace / diff / 三栏 / 空状态
# ---------------------------------------------------------------------


def test_has_chat_panel() -> None:
    assert 'id="chat"' in HTML
    assert 'id="composer"' in HTML and 'id="message-input"' in HTML
    assert "chat-log" in HTML


def test_has_sse_stream() -> None:
    joined = CHAT + API
    assert "EventSource" in joined
    assert "/chat" in joined and "/stream" in joined
    assert "Last-Event-ID" in CHAT or "lastEventId" in CHAT


def test_has_trace_drawer() -> None:
    assert 'id="trace"' in HTML
    assert "traceId" in TRACE and "时间线" in TRACE
    assert "trace-timeline" in TRACE and "trace-timeline" in CSS


def test_enter_to_send_and_abort() -> None:
    assert "Enter" in CHAT
    assert "中止" in HTML or "中止" in CHAT
    assert "abort" in CHAT.lower()
    assert "btn-abort" in HTML


def test_confirm_diff_has_three_groups() -> None:
    assert "moved" in CHAT
    assert "kept" in CHAT
    assert "cannotFit" in CHAT or "cannot_fit" in CHAT
    assert "confirm-actions" in CHAT


def test_three_column_layout() -> None:
    assert 'class="layout"' in HTML
    layout = CSS.split(".layout", 1)[1].split("}", 1)[0]
    assert layout.count("1fr") + layout.count("340px") >= 3


def test_empty_state_has_three_examples() -> None:
    assert CHAT.count("示例") >= 3
    assert HTML.count("示例") >= 3
    assert "chat-examples" in HTML and "chat-examples" in CSS


def test_agent_url_is_configured_not_hardcoded() -> None:
    """不硬编码 localhost:18000：?agent= / localStorage / meta / window 都能配。"""
    assert "localhost:18000" not in CHAT
    assert "cadence-agent-url" in HTML
    assert "?agent" in CHAT or "params.get('agent')" in CHAT
    assert "CadenceChat" in CHAT and "agentBaseUrl" in CHAT


def test_chat_renders_seven_events() -> None:
    for event in ("token", "tool_call", "tool_result", "clarify", "confirm", "done", "error"):
        assert event in CHAT, event


def test_suggestions_three_shapes_are_mapped() -> None:
    # {hint,reason} / {tool,args} / {field,reason} 统一取标题与副标题
    assert "s.hint ||" in CHAT
    assert "s.tool ||" in CHAT
    assert "s.field ||" in CHAT
    assert "s.reason ||" in CHAT and "JSON.stringify" in CHAT


def test_trace_timeline_kinds() -> None:
    for kind in ("LLM", "TOOL", "GUARD", "DEGRADE"):
        assert kind in TRACE, kind
    assert "durationMs" in TRACE and "tokensIn" in TRACE and "detail" in TRACE
