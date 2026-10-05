"""Pydantic input models: the single source of truth for CLI input shapes.

Two consumers, one definition (plan §5.10):

1. ``cli.ops.*`` validates every ``--file`` payload through these models, so a
   bad payload is rejected *before* any SQL runs.
2. ``calliope-cli schema <group>`` renders ``model_json_schema()`` so an agent
   can ask the CLI what shape it wants instead of guessing from a sample row.

Deriving the printed schema from the validator is the whole point. A schema
documented in prose rots the first time someone adds a field; this cannot.

Every model sets ``extra="forbid"``: an unknown key is a caller bug, and
silently dropping it is how content quietly goes missing.
"""
from __future__ import annotations

from typing import Annotated, Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# clips.shot_size whitelist (coverage_agent.py:124). The DB would happily store
# NULL for an illegal value (coverage_agent.py:141-143) -- we reject instead, so
# a typo surfaces as an error rather than as a clip that silently lost its size.
SHOT_SIZES = ("wide", "medium", "closeUp", "insert", "overShoulder")

#: The same whitelist as a type, so ``model_json_schema()`` emits an ``enum``.
#: Without this the generated schema advertised ``shot_size`` as an arbitrary
#: string while the validator rejected anything else -- an agent reading the
#: schema had no way to learn the legal values. Building it from SHOT_SIZES keeps
#: the two from drifting.
ShotSize = Literal[*SHOT_SIZES]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


#: Long-form prose, where surrounding whitespace is *content*.
#:
#: ``_Strict`` strips every string, which is right for a title ("  Beat 1  " and
#: "Beat 1" are the same beat) and wrong for the source novel: an indented poem
#: and a trailing blank line both change what a later ``project source --grep``
#: finds. It also broke round-tripping -- ``--idea-file`` wrote 2355 characters
#: and 2354 came back, so a byte-for-byte re-read never matched and every
#: subsequent ``--expect-hash`` on ``projects`` was computed over text the caller
#: had never seen. Opting this one field out is cheaper than explaining it later.
RawText = Annotated[str, StringConstraints(strip_whitespace=False)]


class BeatIn(_Strict):
    """One story beat. ``order_index`` omitted means "append"."""

    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    order_index: int | None = Field(default=None, ge=1)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("title must not be blank")
        return v


class BeatPatch(_Strict):
    """A partial beat update. Only the fields you pass are written."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    order_index: int | None = Field(default=None, ge=1)


class CastIn(_Strict):
    """A character / location / item. Upserted by ``name``.

    ``appearance`` changes on a row that already has a ``sheet_path`` are
    gated by the caller (plan §5.3): the sheet is rendered output, so silently
    changing the spec invalidates it.
    """

    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(default="character", pattern="^(character|location|item)$")
    role: str | None = None
    age: str | None = None
    appearance: str | None = None
    personality: str | None = None
    description: str | None = None
    reference_image_path: str | None = None


class CastPatch(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = None
    age: str | None = None
    appearance: str | None = None
    personality: str | None = None
    description: str | None = None
    reference_image_path: str | None = None
    portrait_path: str | None = None
    sheet_path: str | None = None


class SceneIn(_Strict):
    heading: str | None = None
    action: str | None = None
    dialog: str | None = None
    duration_sec: float | None = Field(default=None, gt=0)
    order_index: int | None = Field(default=None, ge=1)
    location_id: int | None = None
    characters: list[str] = Field(default_factory=list)


class ScenePatch(_Strict):
    heading: str | None = None
    action: str | None = None
    dialog: str | None = None
    duration_sec: float | None = Field(default=None, gt=0)
    order_index: int | None = Field(default=None, ge=1)
    location_id: int | None = None


class ClipIn(_Strict):
    """One shot inside a scene.

    ``dialog_lines_covered`` is stored as a TEXT-encoded JSON array of 1-based
    indices into the scene dialog's non-blank lines (coverage_agent.py:288).
    It is a list here so a caller cannot write the string ``"[1,2]"`` and get
    a row that reads back as one opaque string.
    """

    description: str | None = None
    shot_size: ShotSize | None = None
    duration_sec: float | None = Field(default=None, gt=0)
    order_index: int | None = Field(default=None, ge=1)
    dialog_lines_covered: list[int] | None = None

    @field_validator("shot_size", mode="before")
    @classmethod
    def _blank_shot_size_is_absent(cls, v: Any) -> Any:
        """Treat ``""`` as "no size", the way the UI's empty form field sends it.

        The Literal below rejects ``""`` outright, which would make a payload
        copied out of a form unusable. Everything else is left to Pydantic so
        the error message is the standard "expected one of ...".
        """
        return None if v == "" else v

    @field_validator("dialog_lines_covered")
    @classmethod
    def _lines_positive(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return None
        for n in v:
            if n < 1:
                raise ValueError(
                    "dialog_lines_covered holds 1-based line numbers; "
                    f"got {n}"
                )
        return sorted(set(v))


class ClipPatch(ClipIn):
    pass


def _ledger_keys(attr: str) -> Callable[[dict[str, Any]], None]:
    """Teach the JSON Schema which keys a ledger bucket accepts.

    ``dict[str, str]`` alone renders as a freeform object, so ``schema show
    context set`` claimed any key worked -- and then the validator rejected it.
    That is the worst shape for a document whose whole point is "never guess a
    field name": the machine-readable answer and the enforced answer disagreed,
    and the caller trusted the machine-readable one.

    The key tuple is read at schema-build time from ``agent.continuity`` (the
    single definition) rather than restated here, so adding a key to the ledger
    cannot leave the schema behind.

    ``propertyNames.enum`` is the JSON Schema spelling of "these keys or none",
    and it validates against the same set the Python validator uses.
    """

    def add(schema: dict[str, Any]) -> None:
        import calliope.agent.continuity as continuity

        allowed = getattr(continuity, attr)
        schema.setdefault("propertyNames", {})["enum"] = list(allowed)
        schema["x-allowed-keys"] = list(allowed)
        schema["description"] = (
            f"Values by key. Allowed keys only: {', '.join(allowed)}. "
            "Each value is capped at 400 characters, which is what the render "
            "pipeline reads, so a longer value is truncated rather than stored."
        )

    return add


class ContextIn(_Strict):
    """The writable half of projects.continuity_json.

    ``shots`` is deliberately absent from this model. It cannot be a slot:
    ``ensure_continuity_plan`` rebuilds the whole plan whenever
    ``based_on != basis_hash(board)`` (continuity.py:492), and
    ``normalize_plan`` never reads the previous value (continuity.py:336-347)
    -- so anything written here is replaced on the next recalculation. Per-clip
    continuity constraints belong in ``clips.description`` with a ``[CARRY]``
    prefix instead (plan §4.1.1).
    """

    overview: dict[str, str] = Field(
        default_factory=dict, json_schema_extra=_ledger_keys("OVERVIEW_KEYS")
    )
    requirements: dict[str, str] = Field(
        default_factory=dict, json_schema_extra=_ledger_keys("REQUIREMENT_KEYS")
    )

    @field_validator("overview")
    @classmethod
    def _overview_known(cls, v: dict[str, str]) -> dict[str, str]:
        from calliope.agent.continuity import OVERVIEW_KEYS

        return _restrict_keys(v, OVERVIEW_KEYS, "overview")

    @field_validator("requirements")
    @classmethod
    def _requirements_known(cls, v: dict[str, str]) -> dict[str, str]:
        from calliope.agent.continuity import REQUIREMENT_KEYS

        return _restrict_keys(v, REQUIREMENT_KEYS, "requirements")


def _restrict_keys(
    value: dict[str, str], allowed: tuple[str, ...], label: str
) -> dict[str, str]:
    """Keep the known keys, coerce to str, cap length.

    ``normalize_plan`` truncates each bucket to 400 chars
    (continuity.py:343/347). Truncating on the way in means what the CLI
    reports is what will actually be stored, instead of the caller
    discovering the cut later.
    """

    out: dict[str, str] = {}
    for key, raw in value.items():
        if key not in allowed:
            raise ValueError(
                f"{label} has no key {key!r}; allowed: {list(allowed)}"
            )
        out[key] = str(raw or "").strip()[:400]
    return out


class ProjectPatch(_Strict):
    """``project set`` takes flags, not a payload.

    ``idea`` is here because it is the novel source text (plan §4.2) -- the
    same field the web UI's new-project textarea writes.
    """

    title: str | None = Field(default=None, min_length=1, max_length=300)
    idea: RawText = None
    genre: str | None = None
    tone: str | None = None
    target_duration: str | None = None
    status: str | None = None
    cover_path: str | None = None


#: group name -> {verb: (model, accepts)}
#: ``accepts`` is "file" (a JSON array/object payload) or "none" (flags only).
#: Rendered by ``cli/schema.py``; validated by ``cli/io.py``.
INPUT_MODELS: dict[str, dict[str, tuple[type[BaseModel], str]]] = {
    "story": {
        "append": (BeatIn, "file"),
        "update": (BeatPatch, "none"),
        "replace-range": (BeatIn, "file"),
    },
    "cast": {
        "upsert": (CastIn, "file"),
        "update": (CastPatch, "none"),
    },
    "script": {
        "append": (SceneIn, "file"),
        "update": (ScenePatch, "none"),
        "replace-range": (SceneIn, "file"),
    },
    "clips": {
        "append": (ClipIn, "file"),
        "update": (ClipPatch, "none"),
        "replace-range": (ClipIn, "file"),
    },
    "context": {
        # "object", not "file": the ledger is ONE document, not a row batch.
        # The ledger lives in projects.continuity_json alongside the other
        # project-level fields, so there is nothing to append to.
        "set": (ContextIn, "object"),
    },
    "project": {
        "set": (ProjectPatch, "none"),
    },
}


def schema_for(group: str, verb: str) -> dict[str, Any] | None:
    """JSON Schema for one write verb, or None if the pair is unknown."""
    entry = INPUT_MODELS.get(group, {}).get(verb)
    if entry is None:
        return None
    model, accepts = entry
    return {
        "group": group,
        "verb": verb,
        "accepts": accepts,
        "payload": {"file": "array", "object": "object", "none": "none"}[accepts],
        "schema": model.model_json_schema(),
    }


def validate_payload(
    group: str, verb: str, payload: Any
) -> list[Any]:
    """Validate ``payload`` for ``<group> <verb>``; raise ValidationFailed.

    Always returns a list, because every caller writes rows. For the row-based
    commands the payload must be an array; ``context set`` is the one exception
    (it writes a single document), so it also accepts a bare object.
    """
    from calliope.authoring.service import ValidationFailed

    entry = INPUT_MODELS.get(group, {}).get(verb)
    if entry is None:
        raise ValidationFailed(
            f"Unknown command {group} {verb}",
            [{"loc": "command", "msg": f"no input model for {group} {verb}"}],
        )
    model, accepts = entry
    if accepts == "none":
        raise ValidationFailed(
            f"{group} {verb} takes flags, not a --file payload",
            [{"loc": "command", "msg": "this command has no --file input"}],
        )
    if accepts == "object":
        # Single document. A one-element array is accepted too -- a caller that
        # batched it should not have to unwrap it by hand.
        items = payload if isinstance(payload, list) else [payload]
        if len(items) != 1:
            raise ValidationFailed(
                f"{group} {verb} takes exactly one object",
                [{"loc": "$", "msg": f"got {len(items)}"}],
            )
        try:
            return [model.model_validate(items[0])]
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            raise ValidationFailed(
                f"{group} {verb}: payload rejected",
                [_problems_from(exc, 0)],
            ) from exc

    if not isinstance(payload, list):
        raise ValidationFailed(
            f"{group} {verb} expects a JSON array",
            [{"loc": "$", "msg": f"got {type(payload).__name__}, want array"}],
        )
    if not payload:
        raise ValidationFailed(
            f"{group} {verb} got an empty array",
            [{"loc": "$", "msg": "nothing to write"}],
        )
    out: list[Any] = []
    problems: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        try:
            out.append(model.model_validate(item))
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            problems.append(_problems_from(exc, index))
    if problems:
        # Report every bad row at once. One error per run turns a 40-row
        # payload into 40 round trips.
        raise ValidationFailed(
            f"{group} {verb}: {len(problems)} of {len(payload)} rows rejected",
            problems,
        )
    return out


def _problems_from(exc: Exception, index: int) -> dict[str, Any]:
    detail: list[dict[str, Any]] = []
    errors = getattr(exc, "errors", None)
    if callable(errors):
        for err in errors():
            loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
            detail.append({"loc": f"[{index}].{loc}", "msg": str(err.get("msg", ""))})
    else:  # pragma: no cover - pydantic always provides .errors()
        detail.append({"loc": f"[{index}]", "msg": str(exc)})
    return {"row": index, "problems": detail}
