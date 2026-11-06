"""Pin the producer-aware coherence contract in the bundled skill."""

import hashlib
from pathlib import Path


SKILL = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "governance-fundamentals"
    / "SKILL.md"
)


def _parts() -> tuple[str, str]:
    text = SKILL.read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---", 2)
    return frontmatter, body


def _coherence_section_sha() -> str:
    _, body = _parts()
    start = body.index("\n## Coherence\n")
    end = body.index("\n## ", start + 1)
    return hashlib.sha256(body[start + 1 : end].encode("utf-8")).hexdigest()[:16]


def test_coherence_sources_and_roles_are_explicit() -> None:
    frontmatter, body = _parts()

    # Deliberately a frozen literal, not a derived value: this is a tripwire.
    # It pins a hash of the "## Coherence" section only, so it fires when
    # that contract text changes and stays quiet on freshness re-stamps and
    # edits elsewhere in the skill. (It used to pin `last_verified`, which
    # fired on every re-stamp and missed a content edit that kept the date.)
    # When it fires: re-read the section against coherence_provenance.py and
    # behavioral_sensor.py, check the assertions below still hold, then
    # update the hash.
    # Accept reviewed historical and SN1 snapshots while the automated mirror
    # lands. This pins content, not a freshness date; unknown content still
    # requires the source review described above.
    reviewed_snapshots = {
        "42e86f9405e8744a",  # unitares 817424d9, historical baseline
        "40af918727ff685b",  # unitares 87dd33c3, explicit ODE influence
        "6d854721f11a6e93",  # unitares 4f6bbdcf, SN1 defaults; sources rechecked
    }
    assert _coherence_section_sha() in reviewed_snapshots
    assert "unitares/src/behavioral_sensor.py" in frontmatter
    assert "unitares/src/coherence_provenance.py" in frontmatter
    assert "`legacy_tanh_v`" in body
    assert "`ode_control_feedback`" in body
    assert "`manifold`" in body
    assert "`eis_structural_measurement`" in body


def test_legacy_controller_is_not_taught_as_health_or_balance() -> None:
    _, body = _parts()
    plain_body = body.replace("**", "")

    assert "not a symmetric health/balance score" in plain_body
    assert "Existing critical thresholds are compatibility gates" in body
    assert "Think of it as structural health" not in body
    assert "Coherence reflects balance" not in body


def test_e_and_i_controller_dependency_matches_the_instrument_era() -> None:
    _, body = _parts()

    if _coherence_section_sha() == "6d854721f11a6e93":
        assert "coherence-level self-loops neutralized by default" in body
        assert "coherence-trend and fleet-strategic self-loops neutralized by default" in body
        assert "UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1" in body
        assert "UNITARES_PER_AGENT_I_CALIBRATION=1" in body
        assert "UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=0" in body
        assert "legacy coherence-level term" not in body
        assert "trend of that same legacy controller scalar" not in body
        assert "it feeds E, I," not in body
        assert "omitted-confidence estimator and the compatibility backstops" in body
    else:
        assert "legacy coherence-level term" in body
        assert "trend of that same legacy controller scalar" in body


def test_hidden_confidence_dependency_is_disclosed() -> None:
    frontmatter, body = _parts()

    assert "unitares/src/confidence.py" in frontmatter
    assert "55% of its base weight" in body
    assert "confidence_reliability.coherence_dependency=ode_control_feedback" in body
    assert "not independent confidence evidence" in body
