"""Input contract: ``schema show`` must agree with the validator, field by field.

The whole point of generating the schema from the Pydantic models is that it
cannot drift. That only holds if something checks it, so this file is
field-by-field in both directions:

- every property the schema advertises is accepted by the model
- every model field appears in the schema (a field that exists but is not
  documented is a field the skill will never use)
- ``extra="forbid"`` holds, so a typo is an error rather than a silent drop

Plus: the read face never prints the novel by accident, and ``plan next``
reaches every one of its branches.
"""
from __future__ import annotations

import pytest

from calliope.authoring.models import INPUT_MODELS, schema_for, validate_payload
from calliope.authoring.service import ValidationFailed

FILE_VERBS = [
    (group, verb, entry)
    for group, verbs in INPUT_MODELS.items()
    for verb, entry in verbs.items()
    if entry[1] == "file"
]

ALL_VERBS = [
    (group, verb, entry)
    for group, verbs in INPUT_MODELS.items()
    for verb, entry in verbs.items()
]


def test_there_are_commands_to_check():
    assert len(FILE_VERBS) >= 7


# -- schema <-> validator, per field ---------------------------------------


@pytest.mark.parametrize("group,verb,entry", ALL_VERBS, ids=lambda v: str(v)[:40])
def test_schema_advertises_exactly_the_model_fields(group, verb, entry):
    model, _ = entry
    schema = schema_for(group, verb)
    assert schema is not None
    props = schema["schema"]["properties"]
    assert set(props) == set(model.model_fields), (
        f"{group} {verb}: schema/validator field mismatch"
    )


@pytest.mark.parametrize("group,verb,entry", ALL_VERBS, ids=lambda v: str(v)[:40])
def test_schema_forbids_extra_keys(group, verb, entry):
    schema = schema_for(group, verb)["schema"]
    assert schema.get("additionalProperties") is False, (
        f"{group} {verb} would silently drop unknown keys"
    )


@pytest.mark.parametrize("group,verb,entry", ALL_VERBS, ids=lambda v: str(v)[:40])
def test_every_advertised_field_is_actually_accepted(group, verb, entry):
    """Reverse direction: feed one value per property through the real model.

    A property the model would reject is a documented field that does not work.
    """
    _model, accepts = entry
    if accepts == "none":
        return
    sample = {
        key: _value_for(key, spec)
        for key, spec in schema_for(group, verb)["schema"]["properties"].items()
    }
    payload = sample if accepts == "object" else [sample]
    validate_payload(group, verb, payload)  # must not raise


def _value_for(key: str, spec: dict):
    """A minimal valid value for one property, read off its own JSON Schema.

    Derived from the schema rather than hand-written so the test keeps working
    when a field is added: the new property gets a value from its own spec, and
    a spec the generator cannot satisfy fails loudly instead of being skipped.
    """
    if "enum" in spec:
        return spec["enum"][0]
    if "anyOf" in spec:  # `X | None` is emitted as anyOf with a null branch
        for branch in spec["anyOf"]:
            if branch.get("type") != "null":
                return _value_for(key, branch)
        return None
    if "const" in spec:
        return spec["const"]
    kind = spec.get("type")
    if kind == "array":
        return [_value_for(key, spec.get("items") or {"type": "string"})]
    if kind in ("integer", "number"):
        # Respect the lower bound: duration_sec is gt=0 and order_index ge=1.
        low = spec.get("minimum", 1)
        value = low + 1 if spec.get("exclusiveMinimum") is not None else low
        return value
    if kind == "object":
        return _object_for(key)
    if kind == "boolean":
        return True
    if kind == "string":
        return _string_for(spec)
    raise AssertionError(f"no sample generator for {key}: {spec}")


def _string_for(spec: dict) -> str:
    pattern = spec.get("pattern")
    if pattern:
        # Pydantic emits `^(a|b|c)$` for a Literal-ish field. Take the first
        # alternative: the point is that the pattern is satisfiable, not that
        # every value in it is tried.
        inner = pattern.strip("^$")
        if inner.startswith("(") and inner.endswith(")"):
            return inner[1:-1].split("|")[0]
        raise AssertionError(f"unsupported pattern in test: {pattern}")
    length = max(1, spec.get("minLength", 1))
    if spec.get("maxLength") is not None:
        length = min(length, spec["maxLength"])
    return "x" * length


def _object_for(key: str) -> dict:
    # context's overview/requirements accept only the keys the continuity agent
    # defines, so an arbitrary key would be (correctly) rejected.
    if key == "overview":
        from calliope.agent.continuity import OVERVIEW_KEYS

        return {OVERVIEW_KEYS[0]: "x"}
    if key == "requirements":
        from calliope.agent.continuity import REQUIREMENT_KEYS

        return {REQUIREMENT_KEYS[0]: "x"}
    return {}


@pytest.mark.parametrize("group,verb,entry", ALL_VERBS, ids=lambda v: str(v)[:40])
def test_unknown_key_is_rejected(group, verb, entry):
    _model, accepts = entry
    if accepts == "none":
        return
    # Start from a payload the model accepts, so the ONLY reason it fails is
    # the extra key. Building from scratch would let an unrelated validation
    # error make this test pass for the wrong reason.
    sample = {
        key: _value_for(key, spec)
        for key, spec in schema_for(group, verb)["schema"]["properties"].items()
    }
    sample["definitely_not_a_field"] = 1
    payload = sample if accepts == "object" else [sample]
    with pytest.raises(ValidationFailed) as exc:
        validate_payload(group, verb, payload)
    assert "definitely_not_a_field" in _all_messages(exc.value)


def _all_messages(exc: ValidationFailed) -> str:
    """The top-level message plus every nested problem message.

    ``extra="forbid"`` violations are reported per row in ``problems``, not in
    the summary line, so a test that only reads ``str(exc)`` would pass on the
    wrong error.
    """
    parts = [str(exc)]
    for problem in exc.problems:
        for detail in problem.get("problems", [problem]):
            parts.append(f"{detail.get('loc')} {detail.get('msg')}")
    return " | ".join(parts)


# -- payload shape ---------------------------------------------------------


def test_file_payload_must_be_an_array():
    with pytest.raises(ValidationFailed) as exc:
        validate_payload("story", "append", {"title": "B1"})
    assert "array" in str(exc.value)


def test_empty_array_is_rejected():
    with pytest.raises(ValidationFailed) as exc:
        validate_payload("story", "append", [])
    assert "empty" in str(exc.value)


def test_every_bad_row_is_reported_at_once():
    """One error per run turns a 40-row payload into 40 round trips."""
    with pytest.raises(ValidationFailed) as exc:
        validate_payload(
            "story",
            "append",
            [{"title": "ok"}, {"description": "no title"}, {"title": ""}],
        )
    assert len(exc.value.problems) == 2


def test_problem_report_points_at_the_row_index():
    with pytest.raises(ValidationFailed) as exc:
        validate_payload("story", "append", [{"title": "ok"}, {"title": ""}])
    assert exc.value.problems[0]["row"] == 1
    assert "title" in exc.value.problems[0]["problems"][0]["loc"]


def test_unknown_group_or_verb_is_a_validation_error():
    with pytest.raises(ValidationFailed):
        validate_payload("nope", "append", [{"title": "x"}])
    with pytest.raises(ValidationFailed):
        validate_payload("story", "explode", [{"title": "x"}])
    assert schema_for("nope", "append") is None


def test_flags_only_verb_rejects_a_payload():
    with pytest.raises(ValidationFailed) as exc:
        validate_payload("story", "update", [{"title": "x"}])
    assert "flags" in str(exc.value)


# -- domain rules encoded in the models ------------------------------------


def test_shot_size_whitelist_matches_the_generator():
    """``coverage_agent.py`` whitelists these; a sixth value written here would
    be stored and then ignored downstream."""
    from calliope.authoring.models import SHOT_SIZES

    assert set(SHOT_SIZES) == {"wide", "medium", "closeUp", "insert", "overShoulder"}


def test_shot_size_schema_advertises_the_whitelist():
    """The enum has to be in the generated schema, not just in the validator.

    This is the drift the schema command exists to prevent: `shot_size` typed as
    `str | None` validated by hand would publish as "any string" while rejecting
    everything but five values.
    """
    props = schema_for("clips", "append")["schema"]["properties"]
    variants = [b for b in props["shot_size"].get("anyOf", []) if "enum" in b]
    assert variants, f"shot_size schema has no enum: {props['shot_size']}"
    assert set(variants[0]["enum"]) == {"wide", "medium", "closeUp", "insert", "overShoulder"}


def test_illegal_shot_size_is_rejected_with_the_allowed_list():
    with pytest.raises(ValidationFailed) as exc:
        validate_payload("clips", "append", [{"description": "x", "shot_size": "HUGE"}])
    # The allowed list has to be in the message: an agent that gets told only
    # "invalid" has to guess, and guessing wrong costs a round trip.
    assert "closeUp" in _all_messages(exc.value)


def test_dialog_lines_covered_must_be_one_based():
    with pytest.raises(ValidationFailed):
        validate_payload("clips", "append", [{"dialog_lines_covered": [0]}])


def test_dialog_lines_covered_is_normalized():
    rows = validate_payload(
        "clips", "append", [{"dialog_lines_covered": [3, 1, 3]}]
    )
    assert rows[0].dialog_lines_covered == [1, 3]


def test_duration_must_be_positive():
    with pytest.raises(ValidationFailed):
        validate_payload("clips", "append", [{"duration_sec": 0}])
    with pytest.raises(ValidationFailed):
        validate_payload("clips", "append", [{"duration_sec": -1}])


def test_context_rejects_an_unknown_overview_key():
    from calliope.agent.continuity import OVERVIEW_KEYS

    with pytest.raises(ValidationFailed):
        validate_payload("context", "set", {"overview": {"not_a_bucket": "x"}})


def test_context_rejects_an_unknown_requirement_key():
    from calliope.agent.continuity import REQUIREMENT_KEYS

    with pytest.raises(ValidationFailed):
        validate_payload("context", "set", {"requirements": {"not_a_bucket": "x"}})


def test_context_has_no_shots_field():
    """The plan rebuilds shots from scratch (continuity.py:492), so offering a
    place to write them would be a promise the store cannot keep."""
    assert "shots" not in INPUT_MODELS["context"]["set"][0].model_fields


def test_context_values_are_capped_like_normalize_plan():
    """normalize_plan truncates each bucket to 400 chars (continuity.py:343).
    Truncating on the way in means the CLI reports what will be stored."""
    rows = validate_payload("context", "set", {"overview": {"style": "y" * 900}})
    assert len(rows[0].overview["style"]) == 400


def test_cast_kind_pattern_is_enforced():
    with pytest.raises(ValidationFailed):
        validate_payload("cast", "upsert", [{"name": "X", "kind": "prop"}])


def test_blank_title_is_rejected():
    with pytest.raises(ValidationFailed):
        validate_payload("story", "append", [{"title": "   "}])


def test_title_max_length_is_enforced():
    with pytest.raises(ValidationFailed):
        validate_payload("story", "append", [{"title": "x" * 500}])