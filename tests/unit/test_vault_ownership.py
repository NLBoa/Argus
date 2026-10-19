"""The ownership decision table, exactly as specified. Every row is a test."""

from datetime import UTC, datetime, timedelta

import pytest

from argus.vault.ownership import Action, DesiredNote, frontmatter_hash, plan_note
from argus.vault.proposals import EXPIRY, apply_proposal, file_hash, is_expired, unified_diff
from argus.vault.regions import compose, parse, region_hash, replace_region

DESIRED = DesiredNote(
    frontmatter={"course": "CMSC351", "verified": "2026-10-19"},
    regions=(("core", "The idea, version 2."),),
    tail="\n## Explain it yourself\n\n## My notes\n",
)
V1 = DesiredNote(
    frontmatter={"course": "CMSC351", "verified": "2026-10-12"},
    regions=(("core", "The idea, version 1."),),
    tail=DESIRED.tail,
)


def created() -> tuple[str, dict[str, str]]:
    plan = plan_note(None, V1, {}, allow=False)
    assert plan.text is not None
    return plan.text, dict(plan.written)


def test_new_note_is_written_freely() -> None:
    plan = plan_note(None, DESIRED, {}, allow=False)
    assert plan.action is Action.CREATE
    assert plan.text == compose(DESIRED.frontmatter, DESIRED.regions, DESIRED.tail)
    assert plan.written["core"] == region_hash("The idea, version 2.")
    assert plan.written["frontmatter"] == frontmatter_hash(DESIRED.frontmatter, DESIRED.frontmatter)
    assert plan.proposals == ()


def test_unchanged_region_is_rewritten() -> None:
    text, stored = created()
    plan = plan_note(text, DESIRED, stored, allow=False)
    assert plan.action is Action.REWRITE
    assert plan.text is not None
    assert "The idea, version 2." in plan.text and "version 1" not in plan.text
    assert "verified: '2026-10-19'" in plan.text
    assert plan.proposals == ()


def test_identical_content_is_left_alone() -> None:
    text, stored = created()
    plan = plan_note(text, V1, stored, allow=False)
    assert plan.action is Action.UNCHANGED and plan.text is None


def test_user_edited_region_is_left_alone_and_proposed() -> None:
    text, stored = created()
    edited = replace_region(text, "core", "The idea, version 1, with MY clarification.")
    plan = plan_note(edited, DESIRED, stored, allow=False)
    assert plan.action is Action.PROPOSE
    assert plan.text is None or "MY clarification" in plan.text  # never overwritten
    [prop] = plan.proposals
    assert prop.region_ids == ("core",)
    assert "MY clarification" in prop.before and "version 2" in prop.after
    assert file_hash(prop.before) == prop.base_hash


def test_user_text_elsewhere_is_never_touched() -> None:
    text, stored = created()
    with_notes = text.replace("## My notes\n", "## My notes\nI think case 2 is the tricky one.\n")
    plan = plan_note(with_notes, DESIRED, stored, allow=False)
    assert plan.action is Action.REWRITE
    assert plan.text is not None and "I think case 2 is the tricky one." in plan.text


def test_new_region_for_existing_note_is_only_proposed() -> None:
    text, stored = created()
    more = DesiredNote(
        DESIRED.frontmatter, (("core", "The idea, version 2."), ("practice", "Q1")), DESIRED.tail
    )
    plan = plan_note(text, more, stored, allow=False)
    assert plan.text is not None and "%% argus:begin practice %%" not in plan.text
    [prop] = plan.proposals
    assert prop.region_ids == ("practice",)
    assert "%% argus:begin practice %%" in prop.after


def test_deleted_markers_are_proposed_not_reinserted() -> None:
    text, stored = created()
    broken = text.replace("%% argus:end core %%\n", "")
    plan = plan_note(broken, DESIRED, stored, allow=False)
    written = plan.text or broken
    assert "%% argus:end core %%" not in written  # the damaged region is not repaired by itself
    assert "The idea, version 1." in written
    assert [p.region_ids for p in plan.proposals] == [("core",)]


def test_user_changed_frontmatter_key_is_proposed() -> None:
    text, stored = created()
    edited = text.replace("course: CMSC351", "course: CMSC351-honors")
    plan = plan_note(edited, DESIRED, stored, allow=False)
    assert "CMSC351-honors" in (plan.text or edited)
    assert any("frontmatter" in p.region_ids for p in plan.proposals)


def test_user_added_frontmatter_keys_are_kept() -> None:
    text, stored = created()
    tagged = text.replace("---\n%%", "tags:\n- mine\n---\n%%", 1)
    plan = plan_note(tagged, DESIRED, stored, allow=False)
    assert plan.text is not None and "- mine" in plan.text and plan.proposals == ()


def test_allow_edit_writes_freely_and_logs() -> None:
    text, stored = created()
    edited = replace_region(text, "core", "user scribbles")
    more = DesiredNote(
        DESIRED.frontmatter, (("core", "The idea, version 2."), ("practice", "Q1")), DESIRED.tail
    )
    plan = plan_note(edited, more, stored, allow=True)
    assert plan.action is Action.ALLOW_EDIT
    assert plan.text is not None
    assert parse(plan.text).regions["core"].content == "The idea, version 2."
    assert parse(plan.text).regions["practice"].content == "Q1"
    assert plan.proposals == ()
    assert plan.log == ("updated frontmatter", "rewrote core", "added practice")


def test_multiple_edits_become_one_exact_proposal() -> None:
    two = DesiredNote(V1.frontmatter, (("core", "a"), ("extra", "b")), V1.tail)
    first = plan_note(None, two, {}, allow=False)
    assert first.text is not None
    edited = replace_region(replace_region(first.text, "core", "mine a"), "extra", "mine b")
    plan = plan_note(
        edited,
        DesiredNote(V1.frontmatter, (("core", "A"), ("extra", "B")), V1.tail),
        dict(first.written),
        allow=False,
    )
    [prop] = plan.proposals
    assert prop.region_ids == ("core", "extra")
    assert apply_proposal(edited, prop.base_hash, prop.after) == prop.after


# ---------------------------------------------------------------- proposals


def test_apply_once_and_only_on_the_exact_base() -> None:
    text, stored = created()
    edited = replace_region(text, "core", "mine")
    plan = plan_note(edited, DESIRED, stored, allow=False)
    on_disk = (
        plan.text or edited
    )  # the writer saves safe rewrites first; the proposal is based on that
    [prop] = plan.proposals
    applied = apply_proposal(on_disk, prop.base_hash, prop.after)
    assert applied == prop.after
    assert apply_proposal(applied, prop.base_hash, prop.after) is None  # stale: base changed
    assert apply_proposal(on_disk + "\nmore user text\n", prop.base_hash, prop.after) is None


def test_expiry_and_diff() -> None:
    created_at = datetime(2026, 10, 1, tzinfo=UTC)
    assert timedelta(days=14) == EXPIRY
    assert not is_expired(created_at, created_at + timedelta(days=13, hours=23))
    assert is_expired(created_at, created_at + timedelta(days=14))
    diff = unified_diff("a\nb\n", "a\nc\n", "Concepts/X.md")
    assert diff.splitlines()[:2] == ["--- Concepts/X.md (yours)", "+++ Concepts/X.md (proposed)"]
    assert "-b" in diff and "+c" in diff


@pytest.mark.parametrize("allow", [True, False])
def test_create_ignores_allow(allow: bool) -> None:
    assert plan_note(None, DESIRED, {}, allow=allow).action is Action.CREATE
