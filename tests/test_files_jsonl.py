"""验证字符串知识以 JSONL 格式持久化且内容无损。"""

from __future__ import annotations

import json

from jd_knowledge.storage.files import ArtifactStore


async def test_string_records_are_jsonl_and_preserve_original_content(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    await store.ensure()
    user_id = "user-001"
    workspace_id = "workspace_11111111-1111-5111-8111-111111111111"
    task_id = "a" * 32
    record = {
        "content": "  Exact\ntext  ",
        "content_hash": "sha256:0123456789abcdef0123456789abcdef",
        "created_at": "2026-08-04T06:00:00+00:00",
        "modified_at": "2026-08-04T06:00:00+00:00",
    }

    await store.stage_text(task_id, record["content"])
    prepared, target = await store.prepare_string_record(
        task_id=task_id,
        user_id=user_id,
        workspace_id=workspace_id,
        record=record,
    )
    store.publish_file(prepared, target)

    path = store.strings_target(user_id, workspace_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == record


async def test_string_record_can_be_removed_and_restored_without_touching_siblings(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    await store.ensure()
    user_id = "user-001"
    workspace_id = "workspace_11111111-1111-5111-8111-111111111111"
    target = store.strings_target(user_id, workspace_id)
    target.parent.mkdir(parents=True)
    removed = {"content": "remove", "content_hash": "sha256:" + "a" * 32, "size_bytes": 6}
    sibling = {"content": "keep", "content_hash": "sha256:" + "b" * 32, "size_bytes": 4}
    target.write_text(
        "".join(json.dumps(value, separators=(",", ":")) + "\n" for value in [removed, sibling]),
        encoding="utf-8",
    )

    assert await store.read_string_record(user_id, workspace_id, removed["content_hash"]) == removed
    await store.remove_string_record(user_id, workspace_id, removed["content_hash"])
    assert await store.read_string_record(user_id, workspace_id, removed["content_hash"]) is None
    assert await store.read_string_record(user_id, workspace_id, sibling["content_hash"]) == sibling

    await store.restore_string_record(user_id, workspace_id, removed)
    await store.restore_string_record(user_id, workspace_id, removed)
    records = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert records.count(removed) == 1
    assert sibling in records


async def test_workspace_directory_can_be_atomically_moved_to_recycle(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    await store.ensure()
    workspace = store.workspace_dir("owner", "workspace_delete")
    workspace.mkdir(parents=True)
    (workspace / "marker.txt").write_text("durable", encoding="utf-8")

    recycled = await store.move_workspace_to_recycle("operation-1", "owner", "workspace_delete")

    assert recycled is not None
    assert not workspace.exists()
    assert (recycled / "marker.txt").read_text(encoding="utf-8") == "durable"
    assert await store.move_workspace_to_recycle("operation-2", "owner", "workspace_empty") is None
