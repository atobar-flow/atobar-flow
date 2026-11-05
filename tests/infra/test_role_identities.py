from atobar_contracts.constants import get_bot_logins
from atobar_contracts.envelopes import Verdict, parse, render

VALID_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_ac1_bot_logins_configuration():
    logins = get_bot_logins()
    assert logins["developer"] == "atobar-flow-agent[bot]"
    assert logins["evaluator"] == "atobar-flow-agent[bot]"
    assert logins["arbitrator"] == "atobar-flow-agent[bot]"


def test_ac1_protocol_parse_binds_to_configured_bots():
    bots = get_bot_logins()
    verdict = Verdict(
        tenant="owner/repo",
        pr=1,
        head_sha=VALID_SHA,
        round=1,
        verdict="approved",
        findings=[],
    )
    rendered = render(verdict)

    # Valid author
    parsed = parse(rendered, author_login="atobar-flow-agent[bot]", bots=bots)
    assert parsed == verdict

    # Forged author (e.g. an attacker or unrelated bot)
    forged = parse(rendered, author_login="some-other[bot]", bots=bots)
    assert forged is None
