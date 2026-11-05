from typing import Any

from flow_engine.tenancy.manifest import TenantManifest
from flow_engine.tenancy.onboard import register


class _Q:
    def __init__(self, db: "_DB", table: str) -> None:
        self.db, self.table = db, table

    def upsert(self, row: Any, on_conflict: str = "") -> "_Q":
        self.db.upserts.setdefault(self.table, []).append(row)
        return self

    def select(self, *_: Any) -> "_Q":
        return self

    def eq(self, *_: Any) -> "_Q":
        return self

    def limit(self, *_: Any) -> "_Q":
        return self

    def delete(self) -> "_Q":
        return self

    @property
    def not_(self) -> "_Q":
        return self

    def in_(self, *_: Any) -> "_Q":
        return self

    def execute(self) -> Any:
        class R:
            data = [{"id": "t-1"}]

        return R()


class _DB:
    def __init__(self) -> None:
        self.upserts: dict[str, list[Any]] = {}

    def table(self, name: str) -> _Q:
        return _Q(self, name)


def _manifest(**arb: Any) -> TenantManifest:
    return TenantManifest.model_validate(
        {
            "repo": "arsenyspb/ollama-monitor",
            "owner_github_login": "arsenyspb",
            "metrics": [{"key": "unit_test_pass_rate", "run": "true", "comparator": ">=", "threshold": 1.0}],
            **arb,
        }
    )


def test_register_writes_canonical_arbitration_mode() -> None:
    db = _DB()
    register(db, _manifest(arbitration={"mode": "recommend"}))
    assert db.upserts["tenants"][0]["arbitration_mode"] == "recommend"


def test_register_defaults_arbitration_mode_to_shadow() -> None:
    db = _DB()
    register(db, _manifest())
    assert db.upserts["tenants"][0]["arbitration_mode"] == "shadow"
