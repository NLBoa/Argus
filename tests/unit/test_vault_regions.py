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
        "---\ncourse: CMSC351\nmodule: Week 3\ntags:\n- mine\nverified: '2026-10-19'\n---\n"
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
