"""Saved exec output is bounded, sanitized, and read without rerunning work."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from sunaba.output_store import OutputStore
from sunaba.tools.exec import sandbox_exec
from sunaba.tools.output import read_output


def test_pages_and_tail():
    store = OutputStore()
    key = store.put("cid", "\n".join(str(i) for i in range(20)))
    page = store.read("cid", key, offset=7, limit=3)
    assert page["output"] == "7\n8\n9"
    assert page["shown"] == 3
    assert page["total_lines"] == 20
    assert page["next_offset"] == 10
    assert page["truncated"] and page["has_more"]
    tail = store.read("cid", key, tail_lines=2)
    assert tail["output"] == "18\n19"
    assert tail["next_offset"] is None
    assert tail["offset"] == 18
    assert not tail["has_more"]


def test_expiry_and_container_binding(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("sunaba.output_store.time.monotonic", lambda: clock[0])
    store = OutputStore(ttl=5)
    key = store.put("cid", "text")
    assert store.read("other", key)["status"] == "error"
    clock[0] = 15.0
    assert store.read("cid", key)["status"] == "error"


def test_size_count_eviction_and_oversize():
    store = OutputStore(max_bytes=6, max_entry_bytes=4, max_entries=2)
    first = store.put("cid", "aaa")
    second = store.put("cid", "bbb")
    assert store.put("cid", "12345") is None
    assert store.read("cid", first)["status"] == "ok"
    third = store.put("cid", "ccc")
    assert store.read("cid", first)["status"] == "error"
    assert store.read("cid", second)["status"] == "ok"
    assert store.read("cid", third)["status"] == "ok"
    count_store = OutputStore(max_entries=1)
    old = count_store.put("cid", "")
    count_store.put("cid", "")
    assert count_store.read("cid", old)["status"] == "error"


@pytest.mark.parametrize("kwargs", [
    {"offset": -1}, {"limit": 0}, {"limit": 1001},
    {"tail_lines": 0}, {"tail_lines": 1001}, {"tail_lines": 1, "offset": 1},
])
def test_invalid_ranges(kwargs):
    store = OutputStore()
    assert store.read("cid", "unknown", **kwargs)["status"] == "error"


def test_empty_and_past_end():
    store = OutputStore()
    key = store.put("cid", "")
    assert store.read("cid", key)["shown"] == 0
    key = store.put("cid", "a\nb")
    page = store.read("cid", key, offset=20)
    assert page["output"] == ""
    assert page["next_offset"] is None


@pytest.mark.parametrize("page_limit", [1, 100])
def test_exec_snapshot_reads_without_another_docker_call(page_limit):
    store = OutputStore()
    container = MagicMock()
    output = "\n".join(["same"] * 20 + ["GITHUB_TOKEN=secret", "last"])
    container.exec_run.return_value = (0, (output.encode(), b""))
    docker = MagicMock()
    docker.containers.get.return_value = container
    with (
        patch("sunaba.tools.exec._docker", return_value=docker),
        patch("sunaba.tools.exec.OUTPUT_STORE", store),
        patch("sunaba.tools.output.OUTPUT_STORE", store),
        patch("sunaba.tools.exec.capture_health.check_capture", return_value=None),
        patch("sunaba.tools.exec.journal_record_exec"),
        patch("sunaba.tools.exec.journal_record_exec_start"),
    ):
        result = json.loads(sandbox_exec("cid", argv=["dummy"], limit=page_limit))
        calls = container.exec_run.call_count
        assert calls > 0
        middle = json.loads(read_output("cid", result["output_id"], offset=10, limit=3))
        tail = json.loads(read_output("cid", result["output_id"], tail_lines=2))
        assert middle["output"] == "same\nsame\nsame"  # No repeated-line compression.
        assert tail["output"] == "GITHUB_TOKEN=***\nlast"
        assert "secret" not in tail["output"]
        assert container.exec_run.call_count == calls
        assert "read_output" in result["resource"]


def test_oversized_exec_is_explicit_not_a_false_retrieval_promise():
    store = OutputStore(max_entry_bytes=2)
    container = MagicMock()
    container.exec_run.return_value = (0, (b"a\nb\nc", b""))
    docker = MagicMock()
    docker.containers.get.return_value = container
    with (
        patch("sunaba.tools.exec._docker", return_value=docker),
        patch("sunaba.tools.exec.OUTPUT_STORE", store),
        patch("sunaba.tools.exec.capture_health.check_capture", return_value=None),
        patch("sunaba.tools.exec.journal_record_exec"),
        patch("sunaba.tools.exec.journal_record_exec_start"),
    ):
        result = json.loads(sandbox_exec("cid", argv=["dummy"], limit=1))
    assert "output_unavailable" in result
    assert "output_id" not in result
    assert "resource" not in result
