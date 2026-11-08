"""评测器的两道闸门（收尾补的安全测试）。

1. `reset()` 会通过 API 删光目标 Core 的所有任务 —— live 模式必须显式 `--allow-reset`；
2. `_settings()` 必须能从 `.env` 读 key（曾经用 os.getenv 覆盖成空，导致 live 永远跑不起来）。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from evals.bootstrap import ensure_paths

ensure_paths()

from evals.dataset import load_dataset  # noqa: E402
from evals.runner import Runner  # noqa: E402
from evals.sandbox import CoreSandbox, ResetNotAllowed  # noqa: E402
from fixtures.fake_core import FakeCore  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DATASET = REPO / "evals" / "datasets" / "v1"


def _core(n: int = 3) -> FakeCore:
    core = FakeCore()
    for i in range(n):
        core.tasks[f"t_{i:03d}"] = {
            "id": f"t_{i:03d}",
            "title": f"任务{i}",
            "status": "TODO",
            "priority": "NORMAL",
            "version": 0,
        }
    return core


def test_live_reset_is_refused_when_core_has_tasks() -> None:
    core = _core(3)
    box = CoreSandbox(core, label="live")
    with pytest.raises(ResetNotAllowed) as ei:
        asyncio.run(box.reset())
    assert "allow-reset" in str(ei.value)
    assert len(core.tasks) == 3, "拒绝之后一条都不能删"


def test_live_reset_with_allow_reset_clears_and_snapshots() -> None:
    core = _core(3)
    snap = REPO / ".tmp" / "eval-test-snapshots" / uuid.uuid4().hex
    box = CoreSandbox(core, label="live", allow_reset=True, snapshot_dir=snap)
    asyncio.run(box.reset())
    assert core.tasks == {}
    files = list(snap.glob("pre-reset-*.json"))
    assert len(files) == 1
    assert "任务0" in files[0].read_text(encoding="utf-8")


def test_fake_core_reset_needs_no_permission() -> None:
    core = _core(2)
    asyncio.run(CoreSandbox(core, label="fake").reset())
    assert core.tasks == {}


def test_runner_refuses_live_without_allow_reset() -> None:
    runner = Runner(
        load_dataset(DATASET),
        core="live",
        run_root=REPO / ".tmp" / f"runner-guard-{uuid.uuid4().hex[:6]}",
    )
    with pytest.raises(ResetNotAllowed):
        asyncio.run(runner.run([]))


def test_runner_settings_falls_back_to_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：os.getenv 为空时必须用 Settings 从 .env 读到的 key，而不是覆盖成空。"""

    class Stub:
        deepseek_api_key = "sk-from-dotenv"
        deepseek_model = "deepseek-chat"
        deepseek_base_url = "https://api.deepseek.com/v1"

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("evals.runner.get_settings", lambda: Stub())

    runner = Runner(
        load_dataset(DATASET),
        llm="live",
        core="fake",
        run_root=REPO / ".tmp" / f"runner-settings-{uuid.uuid4().hex[:6]}",
    )
    settings = runner._settings(REPO / ".tmp" / "case-x")
    assert settings.deepseek_api_key == "sk-from-dotenv"
