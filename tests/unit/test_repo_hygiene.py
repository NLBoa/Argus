"""Gate checks on the repository itself: CLAUDE.md shape, secret hygiene, tooling wiring."""

import re

import yaml

from tests.conftest import REPO_ROOT

CLAUDE_MD = (REPO_ROOT / "CLAUDE.md").read_text()

REQUIRED_SECTIONS = [
    "What this is",
    "Non-negotiables",
    "Commands",
    "Architecture map",
    "Data model",
    "Conventions",
    "Source quirks",
    "Vault rules",
    "UI rules",
    "Testing rules",
    "Definition of done",
]

NON_NEGOTIABLES = [
    "Deadline alerts are deterministic and never depend on the AI.",
    "The hub never edits user-written vault text without prior permission or an approved proposal.",
    "Official sources (Canvas, Gradescope) beat dates extracted from posts.",
    "No secrets in git, logs, or the vault.",
    "The AI never drafts solutions to graded work.",
    "Every AI output is schema-validated and has a non-AI fallback.",
]


def test_claude_md_has_every_section_in_order() -> None:
    headings = re.findall(r"^## (.+)$", CLAUDE_MD, flags=re.MULTILINE)
    positions = [
        next(i for i, h in enumerate(headings) if h.startswith(s)) for s in REQUIRED_SECTIONS
    ]
    assert positions == sorted(positions)


def test_claude_md_non_negotiables_verbatim() -> None:
    for rule in NON_NEGOTIABLES:
        assert f"- {rule}" in CLAUDE_MD


def test_claude_md_is_short() -> None:
    assert len(CLAUDE_MD.splitlines()) <= 200


def test_claude_md_lists_required_quirks() -> None:
    for quirk in ["iCal times are UTC", "`Link` headers", "`9:30am`", "`seats`", "SSO", "pinned"]:
        assert quirk in CLAUDE_MD


def test_env_example_has_no_values() -> None:
    for line in (REPO_ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            assert key.startswith("ARGUS_")
            assert value == "", f"{key} must be empty in .env.example"


def test_gitleaks_runs_in_pre_commit() -> None:
    cfg = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hooks = [h for repo in cfg["repos"] for h in repo["hooks"]]
    gitleaks = [h for h in hooks if h["id"] == "gitleaks"]
    assert gitleaks
    assert "--staged" in gitleaks[0]["entry"]


def test_secrets_and_local_config_are_gitignored() -> None:
    """Asks git itself: real config and secrets are ignored, the test fixtures are not."""
    import subprocess

    def ignored(path: str) -> bool:
        r = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", path], cwd=REPO_ROOT, check=False
        )
        return r.returncode == 0

    for path in [".env", "config.yaml", "var/argus.db", "x.db", "argus/web/static/dist/app.css"]:
        assert ignored(path), path
    for path in ["tests/fixtures/config.yaml", "config.example.yaml", ".env.example"]:
        assert not ignored(path), path


def test_ci_has_arm64_job_running_make_check() -> None:
    ci = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    job = ci["jobs"]["check"]
    runners = [m["runner"] for m in job["strategy"]["matrix"]["include"]]
    assert any("arm" in r for r in runners)
    assert any(step.get("run") == "make check" for step in job["steps"])


def test_makefile_has_required_targets() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()
    targets = set(re.findall(r"^([a-z0-9-]+):", makefile, flags=re.MULTILINE))
    required = {"setup", "dev", "css", "test-unit", "test-int", "test-e2e", "test-visual", "evals"}
    assert required | {"check", "deploy"} <= targets
