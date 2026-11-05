from atobar_ops import state


def test_bookmark_diffing_only_prints_new_events(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    first_batch = [{"seq": 1, "kind": "lifecycle.start"}, {"seq": 2, "kind": "tool_call"}]
    new1 = state.diff_new_events("owner/repo", 43, first_batch)
    assert [e["seq"] for e in new1] == [1, 2]

    # Same events replayed: nothing new.
    new2 = state.diff_new_events("owner/repo", 43, first_batch)
    assert new2 == []

    # A superset with two more events: only the new tail is returned.
    second_batch = first_batch + [{"seq": 3, "kind": "model_call"}, {"seq": 4, "kind": "envelope"}]
    new3 = state.diff_new_events("owner/repo", 43, second_batch)
    assert [e["seq"] for e in new3] == [3, 4]


def test_bookmark_is_scoped_per_issue(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state.diff_new_events("owner/repo", 1, [{"seq": 5, "kind": "x"}])
    new = state.diff_new_events("owner/repo", 2, [{"seq": 1, "kind": "y"}])
    assert [e["seq"] for e in new] == [1]


def test_state_dir_is_created(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state.save("owner/repo", 1, {"a": 1})
    assert (tmp_path / ".atobar" / "ops-state").is_dir()
    assert state.load("owner/repo", 1) == {"a": 1}
