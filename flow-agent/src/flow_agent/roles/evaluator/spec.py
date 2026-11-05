"""RoleSpec, done predicate, and helpers for the EVALUATOR role."""

import re
from typing import Any, Literal, Optional
from urllib.parse import quote

from flow_agent.core.config import get_bot_logins
from flow_agent.core.untrusted import wrap_untrusted
from flow_agent.llm.protocol import LLMClient, Reply
from atobar_contracts.conductor_io import linked_prs_for_issue
from atobar_contracts.envelopes import Finding, Plan, Verdict, fingerprint, parse, render_hidden
from flow_agent.roles.base import RoleSpec, format_plan_section, format_wiki_section
from flow_agent.roles.evaluator.prompt import EVALUATOR_SYSTEM_PROMPT
from flow_agent.runtime.tools import allowlist_for

EVIDENCE_RE = re.compile(r"^(check_run:\d+|artifact:(sha256:)?[0-9a-f]{64})$")
FILE_EVIDENCE_RE = re.compile(
    r"^file:(?P<path>.+)#L(?P<start>\d+)(?:-L?(?P<end>\d+))?$"
)


class StaleShaError(Exception):
    """Raised when a verdict is generated or attempted to be posted on a stale Git SHA."""


def _parse_file_evidence(ev: str) -> Optional[tuple[str, int, Optional[int]]]:
    """Parses 'file:<repo_relative_path>#L<start>[-L<end>]' and validates path/line bounds."""
    m = FILE_EVIDENCE_RE.fullmatch(ev)
    if not m:
        return None
    path = m.group("path")
    if path.startswith("/") or path.endswith("/") or "\\" in path:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return None
    if any(seg in ("", ".", "..") or not seg.strip() for seg in path.split("/")):
        return None
    start = int(m.group("start"))
    if start < 1:
        return None
    raw_end = m.group("end")
    end: Optional[int] = int(raw_end) if raw_end is not None else None
    if end is not None and end < start:
        return None
    return (path, start, end)


def _sanitize_fence_text(text: str) -> str:
    """Neutralizes any embedded flow-* code fence inside finding fields so parse() stays unambiguous."""
    return re.sub(r"```+\s*flow-", "``` flow-", text or "")


def validate_evaluator_finding(finding: Finding) -> bool:
    """Validates that a finding cites concrete evidence (check_run id, screenshot URL, artifact sha256, or file:#L)."""
    import os

    ev = (finding.evidence or "").strip()
    if ev.startswith("https://") or ev.startswith("http://"):
        cp_base = (
            os.environ.get("CP_PUBLIC_URL")
            or os.environ.get("CONTROL_PANEL_URL")
            or os.environ.get("CP_URL")
            or ""
        ).strip().rstrip("/")
        if not cp_base or not ev.startswith(cp_base + "/") or any(c in ev for c in (" ", "\t", "\r", "\n", ")")):
            raise ValueError(
                f"Finding {finding.id} URL evidence must be hosted on {cp_base or 'CP_PUBLIC_URL'}: {ev!r}"
            )
        return True
    if ev.startswith("file:"):
        if _parse_file_evidence(ev) is not None:
            return True
        raise ValueError(
            f"Finding {finding.id} evidence must cite a valid repo-relative file:<path>#L<start>[-L<end>]: {ev!r}"
        )
    if not EVIDENCE_RE.fullmatch(ev):
        raise ValueError(
            f"Finding {finding.id} evidence must cite a check_run id or artifact sha256 (or file:<path>#L<line>): {ev!r}"
        )
    return True


def format_evaluator_review_summary(verdict: Verdict) -> str:
    """Renders a progressive-disclosure review summary with severity badges and SHA-pinned permalinks."""
    sha7 = verdict.head_sha[:7]
    if verdict.verdict == "approved" and not verdict.findings:
        return f"**ATOBAR Evaluator Review** approved `{sha7}` (Round {verdict.round} — 0 findings)."

    if not verdict.findings:
        return f"**ATOBAR Evaluator Review** requested changes on `{sha7}` (Round {verdict.round} — 0 findings)."

    header = (
        f"**ATOBAR Evaluator Review** found {len(verdict.findings)} issue(s) on `{sha7}` "
        f"(Round {verdict.round} — `{verdict.verdict}`)."
    )
    blocks: list[str] = [header]
    for f in verdict.findings:
        badge = "🔴" if f.severity == "blocker" else "🟡"
        fid = _sanitize_fence_text(f.id.strip())
        crit = _sanitize_fence_text(f.criterion.strip())
        msg = " ".join(_sanitize_fence_text(f.message or "Verification finding.").split())
        ev = _sanitize_fence_text((f.evidence or "").strip())
        safe_fp = fingerprint(f)
        parsed_file = _parse_file_evidence(ev)
        if parsed_file is not None:
            path, start, end = parsed_file
            line_anchor = f"L{start}-L{end}" if end and end > start else f"L{start}"
            encoded_path = "/".join(quote(seg, safe="") for seg in path.split("/"))
            permalink = f"https://github.com/{verdict.tenant}/blob/{verdict.head_sha}/{encoded_path}#{line_anchor}"
            evidence_line = f"- **Permalink:** [`{path}#{line_anchor}`]({permalink})"
        else:
            evidence_line = f"- **Evidence:** `{ev}`"

        blocks.append(
            f"- {badge} **[{fid}] ({crit})** — {msg}\n"
            f"  <details><summary>Learn more</summary>\n\n"
            f"  {evidence_line}\n"
            f"  - **Severity:** `{f.severity}`\n"
            f"  - **Fingerprint:** `{safe_fp}`\n\n"
            f"  </details>"
        )
    return _sanitize_fence_text("\n\n".join(blocks))


def create_evaluator_verdict(
    tenant: str,
    pr: int,
    head_sha: str,
    round: int,
    verdict: Literal["approved", "changes_requested"],
    findings: Optional[list[Finding]] = None,
) -> Verdict:
    """Builds and validates an evaluator Verdict envelope."""
    findings_list = findings or []
    for f in findings_list:
        validate_evaluator_finding(f)
        f.fingerprint = fingerprint(f)

    if verdict not in ("approved", "changes_requested"):
        raise ValueError(f"Invalid evaluator verdict: {verdict!r}")

    return Verdict(
        tenant=tenant,
        pr=pr,
        head_sha=head_sha,
        round=round,
        verdict=verdict,
        findings=findings_list,
    )


def evaluator_done_predicate(reply: Reply) -> bool:
    """Returns True if the evaluator emitted a valid flow-verdict envelope with verified finding citations."""
    text = reply.text or ""
    if "<!-- atobar:flow-verdict" in text:
        return True
    if "```flow-verdict" not in text:
        return False
    bots = get_bot_logins()
    bot_login = bots.get("evaluator", "atobar-flow-agent[bot]")
    verdict = parse(text, author_login=bot_login, bots=bots)
    if not isinstance(verdict, Verdict):
        return False
    try:
        for f in verdict.findings:
            validate_evaluator_finding(f)
    except ValueError:
        return False
    return True


def format_evaluator_next_action(verdict: Verdict, max_review_rounds: int = 4) -> str:
    """Renders a deterministic state-aware 'Your Next Action' call-to-action footer for the Evaluator comment."""
    sha7 = verdict.head_sha[:7]
    if verdict.verdict == "approved":
        return (
            "### 👉 Your Next Action\n"
            "- **Merge when ready:** Once the `flow/arbiter` check passes, you can merge this PR "
            "(or let `auto_merge` complete it).\n"
            "- **Request a code change:** Comment `@atobar-flow-agent <your correction or refinement>` "
            "on this PR to dispatch the **Developer** on the current branch.\n"
            f"- **Run another verification check:** Comment `@atobar-flow-agent eval <what to verify>` "
            f"on this PR to re-run the **Evaluator** on `{sha7}`."
        )
    if verdict.round < max_review_rounds:
        return (
            "### 👉 Your Next Action\n"
            f"- **Automatic rework in progress (Round {verdict.round}/{max_review_rounds}):** "
            "The **Developer** is being dispatched automatically to address the findings above.\n"
            "- **Steer the fix:** Comment `@atobar-flow-agent <additional guidance or constraints>` "
            "on this PR (or Issue) and the next turn will incorporate your instructions.\n"
            f"- **Re-run verification:** Comment `@atobar-flow-agent eval <focus area>` "
            f"on this PR to re-evaluate `{sha7}`."
        )
    return (
        "### 👉 Your Next Action\n"
        f"- **Review round cap reached (Round {verdict.round}/{max_review_rounds}):** "
        "Automatic rework is paused for human review.\n"
        "- **Resume the loop:** Apply `flow:resume` on the Issue to reset the loop budget and start a fresh epoch "
        "(leave any `@atobar-flow-agent <instructions>` comments first so the resumed turn picks them up)."
    )


def build_evaluator_context(
    tenant: str,
    pr: int,
    head_sha: str,
    issue_text: str,
    pr_description: str = "",
    checks_summary: str = "",
    wiki: Optional[dict[str, Any]] = None,
    latest_plan: Optional[Plan] = None,
    human_comments: Optional[list[str]] = None,
) -> str:
    """Builds evaluation context with untrusted claims isolation, wiki, and plan ledger."""
    wrapped_issue = wrap_untrusted(issue_text, source="issue_specification")
    sections = [
        f"TENANT: {tenant}\nPULL REQUEST: #{pr}\nEVALUATED HEAD SHA: {head_sha}",
        f"ISSUE SPECIFICATION (ACCEPTANCE CRITERIA):\n{wrapped_issue}",
    ]

    if wiki:
        sections.append(format_wiki_section(wiki))

    if latest_plan:
        sections.append(format_plan_section(latest_plan))

    if pr_description:
        wrapped_claims = wrap_untrusted(pr_description, source="pr_description")
        sections.append(
            f"UNTRUSTED CLAIMS (PR DESCRIPTION & DEVELOPER NOTES):\n{wrapped_claims}\n"
            "(NOTE: Claims must be verified against code and test executions. DEV TRANSCRIPTS: NOT AVAILABLE)"
        )
    else:
        sections.append("DEV TRANSCRIPTS: NOT AVAILABLE (Independent verification)")

    if checks_summary:
        sections.append(f"CI CHECKS STATUS ON {head_sha}:\n{checks_summary}")

    if human_comments:
        joined_comments = "\n\n---\n\n".join(c.strip() for c in human_comments if c and c.strip())
        if joined_comments:
            sections.append(
                "HUMAN CLARIFICATIONS & STEERING COMMENTS (ISSUE & PR):\n"
                + wrap_untrusted(joined_comments, source="human_comment")
            )

    return "\n\n".join(sections)


def post_evaluator_verdict(
    gh: Any,
    tenant: str,
    pr: int,
    verdict: Verdict,
    current_pr_head_sha: str,
    max_review_rounds: int = 4,
) -> bool:
    """Posts the flow-verdict envelope comment and flow/eval check run if head_sha is current."""
    if verdict.head_sha != current_pr_head_sha:
        raise StaleShaError(
            f"Cannot post verdict on stale SHA {verdict.head_sha} (current PR head is {current_pr_head_sha})"
        )

    for f in verdict.findings:
        validate_evaluator_finding(f)
        f.fingerprint = fingerprint(f)

    import os
    run_id = os.environ.get("FLOW_RUN_ID")
    if run_id and not verdict.run_id:
        verdict.run_id = run_id

    # 1. Post full review where the human acts (the PR when one exists) and a one-line link + hidden envelope on the Issue
    summary_md = format_evaluator_review_summary(verdict)
    next_action_md = format_evaluator_next_action(verdict, max_review_rounds=max_review_rounds)
    envelope_md = render_hidden(verdict)
    md_lines: list[str] = []
    try:
        from flow_agent.tools.git_tools import workspace_dir
        from flow_agent.tools.vision_tools import (
            load_recorded_screenshots,
            render_screenshot_with_provenance,
        )

        screens = load_recorded_screenshots(workspace_dir())
        for s in screens:
            url = (s.get("url") or "").strip()
            if url:
                md_lines.append(render_screenshot_with_provenance(s))
    except Exception:
        pass

    open_pr_nums = linked_prs_for_issue(gh, tenant, pr, current_pr_head_sha)
    if open_pr_nums:
        pr_parts: list[str] = [summary_md]
        if md_lines:
            pr_parts.append(
                f"### 📸 Evaluator Verification Screenshots (`{current_pr_head_sha[:7]}`)\n\n"
                + "\n\n".join(md_lines)
            )
        pr_parts.append(next_action_md)
        pr_comment_body = "\n\n".join(pr_parts)
        for target_pr_num in open_pr_nums:
            try:
                gh.post_pr_comment(
                    repo=tenant,
                    pr_number=int(target_pr_num),
                    body=pr_comment_body,
                )
            except Exception:
                pass
        sha7 = verdict.head_sha[:7]
        status_word = (
            "approved"
            if verdict.verdict == "approved"
            else f"requested changes ({len(verdict.findings)} finding(s))"
        )
        pr_links = ", ".join(f"#{n}" for n in open_pr_nums)
        issue_line = (
            f"**ATOBAR Evaluator Review** (Round {verdict.round}): {status_word} on `{sha7}` "
            f"— see full review on {pr_links}."
        )
        comment_body = f"{issue_line}\n\n{envelope_md}"
    else:
        parts: list[str] = [summary_md]
        if md_lines:
            parts.append("### 📸 Evaluator Verification Screenshots\n\n" + "\n\n".join(md_lines))
        parts.append(next_action_md)
        parts.append(envelope_md)
        comment_body = "\n\n".join(parts)

    res = gh.post_pr_comment(repo=tenant, pr_number=pr, body=comment_body)
    comment_id = res.get("id") if isinstance(res, dict) and isinstance(res.get("id"), int) else (
        res if isinstance(res, int) else None
    )
    try:
        setattr(gh, "_last_verdict_comment_id", comment_id)
        setattr(gh, "_last_verdict_comment_body", comment_body)
    except Exception:
        pass

    # 2. Post flow/eval check run
    conclusion = "success" if verdict.verdict == "approved" else "failure"
    summary = (
        f"Evaluator round {verdict.round}: {verdict.verdict}. "
        f"Findings count: {len(verdict.findings)}"
    )
    gh.create_check_run(
        repo=tenant,
        name="flow/eval",
        head_sha=verdict.head_sha,
        conclusion=conclusion,
        output={
            "title": f"flow/eval: {verdict.verdict}",
            "summary": summary,
        },
    )
    return True


def get_evaluator_spec(
    llm: LLMClient,
    model_name: Optional[str] = None,
    custom_prompt: Optional[str] = None,
) -> RoleSpec:
    """Returns the RoleSpec configuring the EVALUATOR role."""
    return RoleSpec(
        name="evaluator",
        system_prompt=custom_prompt or EVALUATOR_SYSTEM_PROMPT,
        tools=allowlist_for("evaluator"),
        llm=llm,
        done=evaluator_done_predicate,
        model_name=model_name,
    )
