"""验证结构化任务日志同时写入文件与终端。"""

from __future__ import annotations

import logging

from medrag_nexus.services.task_log import TaskLogStore


async def test_log_store_writes_same_structured_event_to_file_and_terminal(tmp_path, caplog, monkeypatch) -> None:
    async def inline_to_thread(operation, *args, **kwargs):
        return operation(*args, **kwargs)

    monkeypatch.setattr("medrag_nexus.services.task_log.asyncio.to_thread", inline_to_thread)
    store = TaskLogStore(tmp_path)
    await store.ensure()
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    await store.write_retrieval(
        "INFO",
        "收到用户检索问题",
        user_id="user-001",
        query="第一行\n第二行",
    )

    log_text = next(store.retrieval_dir.glob("*.log")).read_text(encoding="utf-8")
    assert "收到用户检索问题" in log_text
    assert "query=第一行 第二行" in log_text
    assert "[RETRIEVAL] 收到用户检索问题" in caplog.text
    assert "query=第一行 第二行" in caplog.text
