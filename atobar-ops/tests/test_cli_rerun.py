import argparse

from atobar_ops import cli, resolve


def _args(**kw):
    defaults = dict(
        repo="arsenyspb/ollama-monitor",
        issue=43,
        write=False,
        keep_pr=[],
        label="flow:feature",
        timeout=5,
        json=False,
        verbose=False,
        max_chars=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _fake_resolved(prs):
    return resolve.Resolved(
        repo="arsenyspb/ollama-monitor",
        issue=43,
        tenant={"id": "tenant-1"},
        telemetry=None,
        instances=[],
        prs=prs,
        run_id=None,
    )


def test_rerun_dry_run_does_not_call_gh_run(monkeypatch, capsys):
    pr = resolve.PRInfo(number=49, state="OPEN", head_ref="flow/issue-43", title="feat: thing")
    monkeypatch.setattr(cli.resolve, "resolve", lambda *a, **k: _fake_resolved([pr]))
    monkeypatch.setattr(
        cli.gh,
        "json_or_empty",
        lambda args, **k: [{"id": 1}, {"id": 2}] if "comments" in args[1] else [{"name": "flow:feature"}],
    )

    def boom(*a, **k):
        raise AssertionError("dry-run must not mutate GitHub")

    monkeypatch.setattr(cli.gh, "run", boom)
    monkeypatch.setattr(cli.db, "prune_plan", lambda tid, prs: {"loop_rounds": 2, "arbitration_decisions": 1})

    rc = cli.cmd_rerun(_args(write=False))
    out = capsys.readouterr().out

    assert rc == cli.EXIT_OK
    assert "[DRY-RUN]" in out
    assert "delete 2 issue comment(s)" in out
    assert "close+archive PR #49" in out
    assert "flow/issue-43" in out
    assert "prune supabase rows for issue #43" in out
    assert "re-apply label: flow:feature" in out


def test_rerun_dry_run_with_no_prs(monkeypatch, capsys):
    monkeypatch.setattr(cli.resolve, "resolve", lambda *a, **k: _fake_resolved([]))
    monkeypatch.setattr(cli.gh, "json_or_empty", lambda args, **k: [])

    def boom(*a, **k):
        raise AssertionError("dry-run must not mutate GitHub")

    monkeypatch.setattr(cli.gh, "run", boom)

    rc = cli.cmd_rerun(_args(write=False))
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "close+archive PRs: none" in out


def test_rerun_keep_pr_excludes_from_plan(monkeypatch, capsys):
    pr_keep = resolve.PRInfo(number=49, state="OPEN", head_ref="flow/issue-43", title="keep me")
    pr_close = resolve.PRInfo(number=50, state="OPEN", head_ref="flow/issue-43-b", title="close me")
    monkeypatch.setattr(cli.resolve, "resolve", lambda *a, **k: _fake_resolved([pr_keep, pr_close]))
    monkeypatch.setattr(cli.gh, "json_or_empty", lambda args, **k: [])
    monkeypatch.setattr(cli.db, "prune_plan", lambda tid, prs: {"loop_rounds": 0, "arbitration_decisions": 0})

    rc = cli.cmd_rerun(_args(write=False, keep_pr=[49]))
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "PR #50" in out
    assert "PR #49" not in out


def test_wp5_rerun_no_relabel_and_flow_wip_cleanup_without_watch(monkeypatch, capsys):
    import subprocess

    pr = resolve.PRInfo(number=49, state="OPEN", head_ref="flow/issue-43", title="feat: thing")
    monkeypatch.setattr(cli.resolve, "resolve", lambda *a, **k: _fake_resolved([pr]))

    def fake_json_or_empty(args, **k):
        if "matching-refs/heads/flow-wip/" in args[1]:
            return [
                {"ref": "refs/heads/flow-wip/developer/43"},
                {"ref": "refs/heads/flow-wip/developer/99"},
            ]
        if "comments" in args[1]:
            return []
        return [{"name": "flow:feature"}]

    deleted_refs = []
    added_labels = []

    def fake_gh_run(args, **k):
        if args[:2] == ["api", "-X"] and "refs/heads/" in args[-1]:
            deleted_refs.append(args[-1])
        if "--add-label" in args:
            added_labels.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.gh, "json_or_empty", fake_json_or_empty)
    monkeypatch.setattr(cli.gh, "run", fake_gh_run)
    monkeypatch.setattr(cli.db, "prune_plan", lambda tid, prs: {"loop_rounds": 1, "arbitration_decisions": 0})
    monkeypatch.setattr(cli.db, "prune_execute", lambda tid, prs: {"loop_rounds": 1, "arbitration_decisions": 0})
    monkeypatch.setattr(cli, "cmd_watch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not watch on --no-relabel")))

    rc = cli.cmd_rerun(_args(write=True, no_relabel=True))
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "relabeled=none (--no-relabel)" in out
    assert not added_labels
    assert any(r.endswith("flow-wip/developer/43") for r in deleted_refs)
    assert not any(r.endswith("flow-wip/developer/99") for r in deleted_refs)


def test_wp5_jumbo_new_auto_allocates_next_number_and_reads_stdin(tmp_path, monkeypatch, capsys):
    import io
    import subprocess

    monkeypatch.chdir(tmp_path)
    (tmp_path / "issues").mkdir()
    (tmp_path / "issues" / "J45-local.md").write_text("# J45\n", encoding="utf-8")
    (tmp_path / "JUMBO_STATUS.md").write_text("- J46: In status\n", encoding="utf-8")

    # Simulate initial list having J47 closed, and post-create check showing issue #360 beat our #361 for J48!
    list_calls = {"n": 0}
    gh_runs = []

    def fake_json_or_empty(args, *, timeout=20):
        list_calls["n"] += 1
        if list_calls["n"] == 1:
            return [{"number": 359, "title": "J47: Closed jumbo", "url": "https://x/359"}]
        return [
            {"number": 359, "title": "J47: Closed jumbo", "url": "https://x/359"},
            {"number": 360, "title": "J48: Winner jumbo", "url": "https://x/360"},
            {"number": 361, "title": "J48: Stdin Auto Spec", "url": "https://x/361"},
        ]

    def fake_gh_run(args, *, timeout=30):
        gh_runs.append(args)
        return subprocess.CompletedProcess(
            args, 0, stdout="https://github.com/atobar-flow/atobar-flow/issues/361\n", stderr=""
        )

    monkeypatch.setattr(cli.gh, "json_or_empty", fake_json_or_empty)
    monkeypatch.setattr(cli.gh, "run", fake_gh_run)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("# J47 (stdin-auto): Guessed Heading\n\nBody from heredoc.\n"))

    args = argparse.Namespace(
        number="auto",
        slug="stdin-auto",
        title="J48: Stdin Auto Spec",
        context=None,
        from_file="-",
        timeout=30,
        json=False,
        verbose=False,
        max_chars=None,
    )
    rc = cli.cmd_jumbo_new(args)
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "re-allocated J48 -> J49" in out
    assert not (tmp_path / "issues" / "J48-stdin-auto.md").exists()
    created = tmp_path / "issues" / "J49-stdin-auto.md"
    assert created.is_file()
    assert created.read_text(encoding="utf-8") == "# J49 (stdin-auto): Guessed Heading\n\nBody from heredoc.\n"
    status_line = (tmp_path / "JUMBO_STATUS.md").read_text(encoding="utf-8").splitlines()[0]
    assert status_line.startswith("- J49: Stdin Auto Spec (")
    assert "J49: J48:" not in status_line
    assert any(c[:3] == ["issue", "edit", "361"] and "J49: Stdin Auto Spec" in c for c in gh_runs)
