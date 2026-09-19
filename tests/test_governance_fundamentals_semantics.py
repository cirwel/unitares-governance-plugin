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
    assert _coherence_section_sha() == "42e86f9405e8744a"
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


def test_hidden_e_and_i_dependency_is_disclosed() -> None:
    _, body = _parts()

    assert "legacy coherence-level term" in body
    assert "trend of that same legacy controller scalar" in body


def test_hidden_confidence_dependency_is_disclosed() -> None:
    frontmatter, body = _parts()

    assert "unitares/src/confidence.py" in frontmatter
    assert "55% of its base weight" in body
    assert "confidence_reliability.coherence_dependency=ode_control_feedback" in body
    assert "not independent confidence evidence" in body
