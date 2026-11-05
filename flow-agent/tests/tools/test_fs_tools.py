import pytest

from flow_agent.tools.fs_tools import EditDict, multi_edit, read_file, write_file


def test_multi_edit_success(tmp_path):
    file_path = tmp_path / "test.py"
    file_path.write_text("def hello():\n    return 'hello'\n", encoding='utf-8')

    edits: list[EditDict] = [{"old_string": "return 'hello'", "new_string": "return 'world'"}]
    res = multi_edit(str(file_path), edits)
    assert "SUCCESS" in res
    assert "return 'world'" in file_path.read_text(encoding='utf-8')

def test_multi_edit_multiple_success(tmp_path):
    file_path = tmp_path / "test.py"
    file_path.write_text("def hello():\n    return 'hello'\n\ndef bye():\n    return 'bye'\n", encoding='utf-8')

    edits: list[EditDict] = [
        {"old_string": "return 'hello'", "new_string": "return 'world'"},
        {"old_string": "return 'bye'", "new_string": "return 'goodbye'"}
    ]
    res = multi_edit(str(file_path), edits)
    assert "SUCCESS" in res
    content = file_path.read_text(encoding='utf-8')
    assert "return 'world'" in content
    assert "return 'goodbye'" in content

def test_multi_edit_not_found(tmp_path):
    file_path = tmp_path / "test.py"
    file_path.write_text("def hello():\n    return 'hello'\n", encoding='utf-8')

    edits: list[EditDict] = [{"old_string": "return 'world'", "new_string": "return 'hello'"}]
    with pytest.raises(Exception, match="old_string not found in file"):
        multi_edit(str(file_path), edits)
    # Ensure atomic save (no modification)
    assert file_path.read_text(encoding='utf-8') == "def hello():\n    return 'hello'\n"

def test_multi_edit_not_unique(tmp_path):
    file_path = tmp_path / "test.py"
    file_path.write_text("def hello():\n    return 'hello'\ndef hello2():\n    return 'hello'\n", encoding='utf-8')

    edits: list[EditDict] = [{"old_string": "return 'hello'", "new_string": "return 'world'"}]
    with pytest.raises(Exception, match="old_string is not unique"):
        multi_edit(str(file_path), edits)

def test_read_file_windowing(tmp_path):
    file_path = tmp_path / "test.txt"
    lines = [f"Line {i}\n" for i in range(1, 101)]
    file_path.write_text("".join(lines), encoding='utf-8')

    # Read first 10 lines
    content = read_file(str(file_path), offset_line=1, limit_lines=10)
    assert "Line 1\n" in content
    assert "Line 10\n" in content
    assert "Line 11\n" not in content
    assert "<notice> File truncated." in content

    # Read next 10 lines
    content2 = read_file(str(file_path), offset_line=11, limit_lines=10)
    assert "Line 11\n" in content2
    assert "Line 20\n" in content2
    assert "Line 10\n" not in content2
    assert "<notice> File truncated." in content2

    # Read to end
    content3 = read_file(str(file_path), offset_line=91, limit_lines=20)
    assert "Line 100\n" in content3
    assert "<notice> File truncated." not in content3

def test_write_file_new(tmp_path):
    file_path = tmp_path / "new_dir" / "new_file.txt"
    res = write_file(str(file_path), "hello world")
    assert "SUCCESS" in res
    assert file_path.read_text(encoding='utf-8') == "hello world"

def test_write_file_existing(tmp_path):
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello", encoding='utf-8')
    with pytest.raises(Exception, match="Edit Failed: File already exists"):
        write_file(str(file_path), "world")


def test_j44_wp2_realpath_containment_and_overflow_allowlist(tmp_path, monkeypatch):
    import os
    import uuid
    from flow_agent.tools.search_tools import search_codebase

    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_file = outside / "secret.txt"
    secret_file.write_text("TOP_SECRET", encoding="utf-8")

    # Symlink inside workspace pointing outside
    sym = ws / "linked_secret.txt"
    sym.symlink_to(secret_file)

    monkeypatch.setenv("WORKSPACE_DIR", str(ws))

    for bad_path in ("../outside/secret.txt", str(secret_file), str(sym), "/proc/self/environ"):
        with pytest.raises(PermissionError, match="escapes workspace boundary"):
            read_file(bad_path)
        with pytest.raises(PermissionError, match="escapes workspace boundary"):
            write_file(bad_path, "x")
        with pytest.raises(PermissionError, match="escapes workspace boundary"):
            search_codebase("TOP_SECRET", directory=bad_path)

    # Read-only /tmp/overflow_*.log is allowed ONLY for overflow logs registered by the current run,
    # and never for another run's /tmp/overflow_*.log or for write_file/multi_edit
    from flow_agent.tools.fs_tools import register_overflow_path

    other_ov_path = f"/tmp/overflow_other_{uuid.uuid4().hex[:8]}.log"
    ov_path = f"/tmp/overflow_test_{uuid.uuid4().hex[:8]}.log"
    try:
        with open(other_ov_path, "w", encoding="utf-8") as fh:
            fh.write("other run secret log\n")
        with pytest.raises(PermissionError, match="escapes workspace boundary"):
            read_file(other_ov_path)
        with pytest.raises(PermissionError, match="escapes workspace boundary"):
            search_codebase("secret", directory=other_ov_path)

        register_overflow_path(ov_path)
        with open(ov_path, "w", encoding="utf-8") as fh:
            fh.write("overflow line 1\n")
        assert "overflow line 1" in read_file(ov_path)
        with pytest.raises(PermissionError, match="escapes workspace boundary"):
            write_file(ov_path, "overwrite")
        with pytest.raises(PermissionError, match="escapes workspace boundary"):
            multi_edit(ov_path, [{"old_string": "overflow", "new_string": "hacked"}])
    finally:
        for p in (other_ov_path, ov_path):
            if os.path.exists(p):
                os.remove(p)
