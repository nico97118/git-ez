# SPDX-FileCopyrightText: 2026 git-ez contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Persistent graph state and failed-task recovery through the bundled saver."""

from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from git_ez._vendor.langgraph_sqlite import SqliteSaver

pytestmark = pytest.mark.integration


class Counter(TypedDict):
    count: int


def test_reopened_checkpoint_resumes_failed_task_without_replaying_completed_work(
    tmp_path: Path,
) -> None:
    database = str(tmp_path / "checkpoints.sqlite")
    calls: list[str] = []

    def first(state: Counter) -> Counter:
        calls.append("first")
        return {"count": state["count"] + 1}

    def second(state: Counter) -> Counter:
        calls.append("second")
        if calls.count("second") == 1:
            raise RuntimeError("Temporary failure")
        return {"count": state["count"] + 1}

    builder = StateGraph(Counter)
    builder.add_node("first", first)
    builder.add_node("second", second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    config = {"configurable": {"thread_id": "persistent-test"}}
    with SqliteSaver.from_conn_string(database) as saver:
        graph = builder.compile(checkpointer=saver)
        with pytest.raises(RuntimeError, match="Temporary failure"):
            graph.invoke({"count": 0}, config)
        assert graph.get_state(config).values == {"count": 1}
        checkpoint = saver.get_tuple(config)
        assert checkpoint is not None
        assert checkpoint.pending_writes

    with SqliteSaver.from_conn_string(database) as saver:
        graph = builder.compile(checkpointer=saver)
        assert graph.get_state(config).next == ("second",)
        assert graph.invoke(None, config) == {"count": 2}
        assert len(list(saver.list(config))) >= 3
    assert calls == ["first", "second", "second"]
