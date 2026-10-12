"""The data-source policy: which sources this deployment may run on.

Disabling synthetic is only meaningful if three separate things hold — the
default really is off, the refusal really reaches the user, and turning it back
on really is a one-line change. Each of those is pinned here.
"""

from __future__ import annotations

import textwrap

from backtest.data.sources_policy import (
    FALLBACK_SOURCES,
    PROFILE_ENV,
    SourcePolicy,
    SourceSpec,
    build_policy,
    reset_source_policy,
    source_policy,
)

#: The config as shipped. Built explicitly rather than through the cached
#: ``source_policy()``: the test suite points that at the `testing` profile
#: (see tests/conftest.py), so it cannot answer questions about the default.
SHIPPED = build_policy(profile="default", env={})

# --------------------------------------------------------------------------
# The shipped config
# --------------------------------------------------------------------------


def test_synthetic_is_disabled_by_default():
    """The whole point. A random walk must not produce a certifiable score."""
    assert SHIPPED.is_enabled("synthetic") is False


def test_synthetic_is_not_merely_allowed_but_refused():
    policy = SHIPPED
    refusal = policy.refusal_for("synthetic")
    assert refusal and "disabled" in refusal.lower()
    # A refusal that does not say what to do instead is a dead end.
    assert "config/data_sources.yaml" in refusal
    assert policy.enabled_names(), "the message must name the alternatives"


def test_real_sources_stay_on():
    policy = SHIPPED
    for name in ("db", "mstock", "csv"):
        assert policy.is_enabled(name) is True, f"{name} should be available"
        assert policy.refusal_for(name) is None


def test_only_db_and_mstock_are_certifiable():
    """
    CSV is user-supplied bars of unknown provenance. Real in shape, unverified
    in fact — calling it certifiable would put back the exact gap this policy
    exists to close, just one column over.
    """
    policy = SHIPPED
    assert policy.is_certifiable("db") is True
    assert policy.is_certifiable("mstock") is True
    assert policy.is_certifiable("csv") is False
    assert policy.is_certifiable("synthetic") is False


def test_the_whole_vocabulary_is_described():
    """A source that silently vanishes is impossible to debug.

    ``dhan`` joined the vocabulary with the multi-broker feed: it is a real
    broker source a runner can name, so the config has to describe it — an
    undescribed source is one the policy cannot refuse or allow on purpose.
    """
    names = {row["name"] for row in SHIPPED.describe()}
    assert names == {"synthetic", "csv", "db", "mstock", "dhan"}


def test_disabled_sources_are_still_listed():
    """Flagged, not hidden."""
    rows = {r["name"]: r for r in SHIPPED.describe()}
    assert rows["synthetic"]["enabled"] is False
    assert rows["synthetic"]["label"], "a disabled source still needs a name"


# --------------------------------------------------------------------------
# Toggling — the user's whole reason for a config file
# --------------------------------------------------------------------------


def test_flipping_enabled_true_is_enough_to_allow_it(tmp_path):
    cfg = tmp_path / "data_sources.yaml"
    cfg.write_text(
        textwrap.dedent("""
            default:
              sources:
                synthetic:
                  enabled: true
                  label: "Synthetic (random walk)"
                  certifiable: false
            """),
        encoding="utf-8",
    )
    policy = build_policy(path=cfg, profile="default", env={})
    assert policy.is_enabled("synthetic") is True
    assert policy.refusal_for("synthetic") is None
    # Allowed, but still not certifiable — flipping the switch is not a claim
    # about the data.
    assert policy.is_certifiable("synthetic") is False


def test_the_testing_profile_opts_back_in(tmp_path):
    """What the test suite relies on, asserted rather than assumed."""
    cfg = tmp_path / "data_sources.yaml"
    cfg.write_text(
        textwrap.dedent("""
            default:
              sources:
                synthetic:
                  enabled: false
                db:
                  enabled: false
            profiles:
              testing:
                sources:
                  synthetic:
                    enabled: true
            """),
        encoding="utf-8",
    )
    strict = build_policy(path=cfg, profile="default", env={})
    assert strict.is_enabled("synthetic") is False
    relaxed = build_policy(path=cfg, profile="testing", env={})
    assert relaxed.is_enabled("synthetic") is True
    # The override is additive: db stays off.
    assert relaxed.is_enabled("db") is False


def test_the_profile_can_come_from_the_environment(tmp_path):
    cfg = tmp_path / "data_sources.yaml"
    cfg.write_text(
        textwrap.dedent("""
            default:
              sources:
                synthetic:
                  enabled: false
            profiles:
              testing:
                sources:
                  synthetic:
                    enabled: true
            """),
        encoding="utf-8",
    )
    policy = build_policy(path=cfg, env={PROFILE_ENV: "testing"})
    assert policy.profile == "testing"
    assert policy.is_enabled("synthetic") is True


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------


def test_a_missing_file_does_not_re_enable_synthetic(tmp_path):
    """A typo in a YAML path must not be what quietly restores generated data."""
    policy = build_policy(path=tmp_path / "nope.yaml", profile="default", env={})
    assert policy.is_enabled("synthetic") is False
    assert policy.is_enabled("db") is True


def test_a_broken_file_does_not_re_enable_synthetic(tmp_path):
    cfg = tmp_path / "data_sources.yaml"
    cfg.write_text("default: {sources: [this is not a mapping", encoding="utf-8")
    policy = build_policy(path=cfg, profile="default", env={})
    assert policy.is_enabled("synthetic") is False


def test_an_empty_sources_block_falls_back(tmp_path):
    cfg = tmp_path / "data_sources.yaml"
    cfg.write_text("default:\n  sources: {}\n", encoding="utf-8")
    policy = build_policy(path=cfg, profile="default", env={})
    assert policy.is_enabled("synthetic") is False
    assert {r["name"] for r in policy.describe()} == set(FALLBACK_SOURCES)


def test_an_unknown_source_is_refused_with_the_known_list():
    refusal = SHIPPED.refusal_for("postgress")
    assert refusal and "Unknown data source" in refusal
    assert "db" in refusal


def test_a_misspelled_key_is_reported(caplog, tmp_path):
    """`postgress: enabled: true` reads as real data and matches nothing."""
    cfg = tmp_path / "data_sources.yaml"
    cfg.write_text(
        textwrap.dedent("""
            default:
              sources:
                postgress:
                  enabled: true
                db:
                  enabled: true
            """),
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="backtest"):
        build_policy(path=cfg, profile="default", env={})
    assert "postgress" in caplog.text


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------


def test_is_enabled_is_case_and_whitespace_tolerant():
    policy = SHIPPED
    assert policy.is_enabled("  DB ") is True
    assert policy.is_enabled("SYNTHETIC") is False


def test_a_disabled_source_is_never_certifiable():
    """Two switches, and this is the one people forget."""
    policy = SourcePolicy({"x": SourceSpec("x", enabled=False, certifiable=True)}, profile="unit")
    assert policy.is_enabled("x") is False
    assert policy.is_certifiable("x") is False
    assert policy.refusal_for("x")


def test_missing_is_disabled_not_enabled():
    assert SHIPPED.is_enabled("") is False
    assert SHIPPED.is_enabled(None) is False


def test_reset_clears_the_cache():
    reset_source_policy()
    first = source_policy()
    assert source_policy() is first, "cached"
    reset_source_policy()
    assert source_policy() is not first
