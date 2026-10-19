import pytest
from hypothesis import given
from hypothesis import strategies as st

from argus.vault.regions import (
    allows_edit,
    compose,
    parse,
    region_hash,
    render_region,
    replace_region,
    split_frontmatter,
    update_frontmatter,
)

NOTE = """---
course: CMSC351
module: Week 2
tags: [mine]
---
%% argus:begin core %%
The master theorem solves recurrences.
%% argus:end core %%

## Explain it yourself

My own words here.
"""


def test_parse_frontmatter_and_regions() -> None:
    p = parse(NOTE)
    assert p.frontmatter == {"course": "CMSC351", "module": "Week 2", "tags": ["mine"]}
    assert list(p.regions) == ["core"]
    assert p.regions["core"].content == "The master theorem solves recurrences."
    assert p.problems == []


def test_region_hash_ignores_line_ending_and_trailing_space_noise() -> None:
    assert region_hash("a\nb") == region_hash("a\r\nb\n") == region_hash("a  \nb\n\n")
    assert region_hash("a\nb") != region_hash("a\nB")


def test_replace_region_touches_nothing_else() -> None:
    out = replace_region(NOTE, "core", "New content.\nTwo lines.")
    assert "New content.\nTwo lines.\n%% argus:end core %%" in out
    assert out.replace("New content.\nTwo lines.", "The master theorem solves recurrences.") == NOTE


def test_broken_markers_are_reported_not_guessed() -> None:
    broken = NOTE.replace("%% argus:end core %%", "")
    p = parse(broken)
    assert "core" not in p.regions
    assert p.problems == ["region 'core' has no end marker"]
    dup = NOTE + "%% argus:begin core %%\nx\n%% argus:end core %%\n"
    assert parse(dup).problems == ["region 'core' appears more than once"]
    stray = "%% argus:end core %%\n"
    assert parse(stray).problems == ["end marker for 'core' without a begin"]
    with pytest.raises(KeyError):
        replace_region(broken, "core", "x")


def test_nested_marker_is_a_problem() -> None:
    text = "%% argus:begin a %%\n%% argus:begin b %%\nx\n%% argus:end b %%\n%% argus:end a %%\n"
    p = parse(text)
    assert p.problems[0] == "region 'b' starts inside region 'a'"
    assert p.regions == {}  # neither region is trusted


def test_compose_and_render() -> None:
    text = compose({"course": "CMSC351"}, [("core", "Hello.")], tail="\n## My notes\n")
    assert (
        text
        == "---\ncourse: CMSC351\n---\n%% argus:begin core %%\nHello.\n%% argus:end core %%\n\n## My notes\n"
    )
    assert render_region("x", "") == "%% argus:begin x %%\n%% argus:end x %%"
    assert compose(None, [("a", "1"), ("b", "2")]).startswith("%% argus:begin a %%")


def test_frontmatter_update_keeps_user_keys_and_order() -> None:
    out = update_frontmatter(NOTE, {"module": "Week 3", "verified": "2026-10-19"})
    fm, _, body = split_frontmatter(out)
    assert fm == {
        "course": "CMSC351",
        "module": "Week 3",
        "tags": ["mine"],
        "verified": "2026-10-19",
    }
    assert out.startswith(
        "---\ncourse: CMSC351\nmodule: Week 3\ntags: [mine]\nverified: '2026-10-19'\n---\n"  # tags untouched
    )
    assert body == split_frontmatter(NOTE)[2]
    plain = update_frontmatter("no frontmatter\n", {"a": 1})
    assert plain == "---\na: 1\n---\nno frontmatter\n"


def test_bad_frontmatter_is_treated_as_text() -> None:
    weird = "---\n: : :\n---\nbody\n"
    fm, raw, body = split_frontmatter(weird)
    assert fm is None and raw == "" and body == weird


@pytest.mark.parametrize(
    ("fm", "expected"),
    [
        ({"argus-edit": "allow"}, True),
        ({"argus-edit": "Allow"}, True),
        ({"argus-edit": "no"}, False),
        ({}, False),
        (None, False),
    ],
)
def test_allows_edit(fm: dict | None, expected: bool) -> None:  # type: ignore[type-arg]
    assert allows_edit(fm) is expected


@given(
    st.text(
        alphabet=st.characters(blacklist_characters="%\r", blacklist_categories=("Cs",)),
        max_size=200,
    )
)
def test_replace_round_trip(content: str) -> None:
    text = compose({"course": "X"}, [("core", "old")], tail="\nuser text\n")
    new = replace_region(text, "core", content)
    assert region_hash(parse(new).regions["core"].content) == region_hash(content)
    assert new.endswith("\nuser text\n")


# ---------------------------------------------------------------- review R3: frontmatter bytes


def test_owned_keys_change_without_touching_the_students_yaml() -> None:
    import pytest

    from argus.vault.regions import FrontmatterEditError, update_frontmatter

    note = (
        "---\n"
        "course: CMSC351\n"
        "# keep: my reminder to self\n"
        'aliases: [hw1, "Homework 1"]\n'
        "due: '2026-10-20T23:59:00-04:00'\n"
        "summary: >\n"
        "  folded text\n"
        "  over two lines\n"
        "sources:\n"
        "- old 1\n"
        "- old 2\n"
        "tags:\n"
        "  - mine\n"
        "---\n"
        "body\n"
    )
    out = update_frontmatter(
        note, {"due": "2026-10-22T23:59:00-04:00", "sources": ["new"], "verified": False}
    )
    assert out == (
        "---\n"
        "course: CMSC351\n"
        "# keep: my reminder to self\n"
        'aliases: [hw1, "Homework 1"]\n'
        "due: '2026-10-22T23:59:00-04:00'\n"
        "summary: >\n"
        "  folded text\n"
        "  over two lines\n"
        "sources:\n"
        "- new\n"
        "tags:\n"
        "  - mine\n"
        "verified: false\n"
        "---\n"
        "body\n"
    )
    assert (
        update_frontmatter("plain body\n", {"course": "X"}) == "---\ncourse: X\n---\nplain body\n"
    )
    assert update_frontmatter("---\n---\nbody\n", {"course": "X"}) == "---\ncourse: X\n---\nbody\n"
    flow = "---\n{due: 1, course: X}\n---\nx\n"  # one flow mapping: no line belongs to "due"
    with pytest.raises(FrontmatterEditError):
        update_frontmatter(flow, {"due": 2})
    assert "due: 2" in update_frontmatter(flow, {"due": 2}, strict=False)
    anchored = "---\nbase: &b 1\ndue: *b\n---\nx\n"
    assert update_frontmatter(anchored, {"due": 2}) == "---\nbase: &b 1\ndue: 2\n---\nx\n"
    broken = "---\n: : :\n---\nx\n"
    with pytest.raises(FrontmatterEditError):
        update_frontmatter(broken, {"due": 2})


def test_unsafe_frontmatter_edit_becomes_a_proposal() -> None:
    from argus.vault.ownership import Action, DesiredNote, plan_note

    v1 = DesiredNote({"due": 1}, (("core", "x"),), "")
    first = plan_note(None, v1, {}, allow=False)
    assert first.text is not None
    anchored = first.text.replace("---\ndue: 1\n", "---\n{due: 1, course: X}\n", 1)
    plan = plan_note(
        anchored, DesiredNote({"due": 2}, (("core", "x"),), ""), dict(first.written), allow=False
    )
    assert plan.action is Action.PROPOSE and plan.text is None
    [prop] = plan.proposals
    assert prop.region_ids == ("frontmatter",)
