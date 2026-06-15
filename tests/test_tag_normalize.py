"""Unit tests for the deterministic tag normalizer.

The normalizer is formatting-only and fail-open: see scripts/tag_normalize.py
and docs/ontology-need.md for why plural stripping and semantic synonym
merging are deliberately out of scope.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from tag_normalize import (  # noqa: E402
    normalize_call_tags,
    normalize_tag,
    normalize_tag_list,
)


class TestNormalizeTag:

    @pytest.mark.parametrize("raw,expected", [
        ("Postgres", "postgres"),
        ("PostgreSQL", "postgres"),
        ("postgre", "postgres"),
        ("postgres", "postgres"),
        ("Residents", "resident"),
        ("resident", "resident"),
        ("  Postgres  ", "postgres"),
        ("pool_connection_leak", "pool-connection-leak"),
        ("pool connection leak", "pool-connection-leak"),
        ("pool--connection", "pool-connection"),
        ("pool.connection/leak", "pool-connection-leak"),
        ("-leading-and-trailing-", "leading-and-trailing"),
        ("UPPER", "upper"),
    ])
    def test_canonical_forms(self, raw, expected):
        assert normalize_tag(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "---", "...", 5, None, ["x"]])
    def test_unusable_returns_empty(self, raw):
        assert normalize_tag(raw) == ""

    def test_idempotent(self):
        once = normalize_tag("Pool_Connection Leak")
        assert normalize_tag(once) == once == "pool-connection-leak"

    def test_no_plural_stripping(self):
        # Plural stripping is intentionally NOT done — it is too lossy.
        assert normalize_tag("metrics") == "metrics"
        assert normalize_tag("kubernetes") == "kubernetes"


class TestNormalizeTagList:

    def test_dedup_and_canonicalize_in_order(self):
        assert normalize_tag_list(["Postgres", "postgres", "PostgreSQL"]) == ["postgres"]
        assert normalize_tag_list(["resident", "residents"]) == ["resident"]

    def test_preserves_first_seen_order(self):
        assert normalize_tag_list(["beta", "Alpha", "alpha"]) == ["beta", "alpha"]

    def test_drops_empty_string_entries(self):
        assert normalize_tag_list(["ok", "", "   "]) == ["ok"]

    def test_no_change_returns_none(self):
        # Already canonical: nothing to do, signalled by None.
        assert normalize_tag_list(["postgres", "identity"]) is None

    def test_non_list_returns_none(self):
        assert normalize_tag_list("postgres") is None
        assert normalize_tag_list(None) is None

    def test_non_string_member_bails_out(self):
        # An unrecognized payload type means hands off the whole list.
        assert normalize_tag_list(["ok", 7]) is None


class TestNormalizeCallTags:

    def test_mutates_in_place_and_reports_change(self):
        ti = {"action": "search", "tags": ["Postgres", "DB_Pool"]}
        assert normalize_call_tags(ti) is True
        assert ti["tags"] == ["postgres", "db-pool"]
        assert ti["action"] == "search"  # other fields untouched

    def test_no_tags_field_is_noop(self):
        ti = {"action": "search"}
        assert normalize_call_tags(ti) is False
        assert ti == {"action": "search"}

    def test_already_canonical_is_noop(self):
        ti = {"tags": ["postgres", "identity"]}
        assert normalize_call_tags(ti) is False

    def test_non_dict_is_noop(self):
        assert normalize_call_tags("nope") is False  # type: ignore[arg-type]
