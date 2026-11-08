"""SP5 静态契约测试。

SP5 是原生 HTML/JS，没有构建步骤，所以先用"读文件 + 断言 DOM id / API 字符串"
覆盖那些**不需要浏览器就能判定**的契约；真正交互留给 `e2e.js`/Playwright。

现状（2026-11）：
- 任务板、详情、删除确认、忙闲查询是有的；
- PRD 要求的对话流、SSE、Trace 抽屉、diff 三组弹窗**没有**。
因此后半部分用 xfail 把缺口钉住；补齐后 strict xfail 会失败，提醒改成正式断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "main" / "resources" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")
API = (STATIC / "api.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# 已实现：任务板 / 确认 / API 外壳
# ---------------------------------------------------------------------


def test_static_assets_exist() -> None:
    for name in ("index.html", "app.js", "api.js", "app.css"):
        assert (STATIC / name).is_file(), name


def test_no_external_cdn_or_framework() -> None:
    """PRD NFR-3：零依赖，不引 React/Vue/Tailwind CDN。"""
    assert "react" not in HTML.lower()
    assert "vue" not in HTML.lower()
    assert "tailwind" not in HTML.lower()
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
# 未实现：对话 / SSE / Trace / diff
# ---------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="SP5 FR-1/FR-2：当前页面没有对话区/输入框")
def test_has_chat_panel() -> None:
    assert 'id="chat"' in HTML or 'class="chat"' in HTML
    assert 'id="composer"' in HTML or 'id="message-input"' in HTML


@pytest.mark.xfail(strict=True, reason="SP5 FR-3：当前没有 SSE / EventSource / /chat / /stream")
def test_has_sse_stream() -> None:
    joined = APP + API
    assert "EventSource" in joined
    assert "/chat" in joined and "/stream" in joined


@pytest.mark.xfail(strict=True, reason="SP5 FR-8：当前没有 Trace 抽屉")
def test_has_trace_drawer() -> None:
    assert 'id="trace"' in HTML
    assert "traceId" in APP and "时间线" in APP


@pytest.mark.xfail(strict=True, reason="SP5 FR-2：当前没有 Enter 发送 / 中止按钮")
def test_enter_to_send_and_abort() -> None:
    assert "Enter" in APP
    assert "abort" in APP.lower() or "中止" in APP


@pytest.mark.xfail(strict=True, reason="SP5 FR-5：确认弹窗没有 moved/kept/cannot_fit 三组 diff")
def test_confirm_diff_has_three_groups() -> None:
    assert "moved" in APP
    assert "kept" in APP
    assert "cannotFit" in APP or "cannot_fit" in APP


@pytest.mark.xfail(strict=True, reason="SP5 FR-1：PRD 要求左任务板/中对话/右 Trace 三栏，当前是两栏")
def test_three_column_layout() -> None:
    assert 'class="layout"' in HTML
    # 三栏应出现三个列定义；当前 .layout 只有两列
    layout = CSS.split(".layout", 1)[1].split("}", 1)[0]
    assert layout.count("1fr") + layout.count("340px") >= 3


@pytest.mark.xfail(strict=True, reason="SP5 FR-10：空状态没有 3 个可点击示例指令")
def test_empty_state_has_three_examples() -> None:
    lowered = APP.lower()
    assert "示例" in APP or "example" in lowered
    assert lowered.count("example") >= 3 or APP.count("示例") >= 3
