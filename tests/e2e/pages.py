"""Every routable page, for the a11y and visual suites. Add new views here.

(name, path, viewports): kiosk is only ever shown on the Pi's 1920px monitor.
Every page except the kiosk also renders in the no-token demo (D055): `cases()` yields both.
"""

PAGES: list[tuple[str, str, tuple[str, ...]]] = [
    ("today", "/today", ("mobile", "desktop")),
    ("upcoming", "/upcoming", ("mobile", "desktop")),
    ("health", "/health", ("mobile", "desktop")),
    ("announcements", "/announcements", ("mobile", "desktop")),
    ("workload", "/workload", ("mobile", "desktop")),
    ("item", "/items/2", ("mobile", "desktop")),
    ("task-new", "/tasks/new", ("mobile", "desktop")),
    ("course", "/courses/CMSC351", ("mobile", "desktop")),
    ("exams", "/exams", ("mobile", "desktop")),
    ("exam", "/exams/5", ("mobile", "desktop")),
    ("proposals", "/proposals", ("mobile", "desktop")),
    ("settings", "/settings", ("mobile", "desktop")),
    ("not-found", "/exams/9999", ("mobile",)),
    ("search", "/search?q=master+theorem", ("mobile", "desktop")),
    ("search-empty", "/search", ("mobile",)),
    ("styleguide", "/styleguide", ("mobile", "desktop")),
    ("kiosk", "/kiosk", ("kiosk",)),
    ("offline", "/offline", ("mobile",)),
]
VIEWPORTS = {"mobile": (390, 844), "desktop": (1280, 800), "kiosk": (1920, 1080)}

# Today's other states (D072): each gets its own server, set up by conftest.today_state. The
# default demo already covers a running timer and past-due work; `fresh_server(seed=False)` covers
# the empty week.
TODAY_STATES = ("morning",)

# Demo setups each page is checked in. The kiosk has no token-dependent content.
PROFILES = ("full", "notoken")
SINGLE_PROFILE = {"kiosk"}


def cases() -> list[tuple[str, str, str, str]]:
    """(name, path, viewport, profile) for every page, viewport, and demo setup."""
    return [
        (name, path, vp, profile)
        for name, path, vps in PAGES
        for vp in vps
        for profile in (("full",) if name in SINGLE_PROFILE else PROFILES)
    ]


def case_id(name: str, viewport: str, profile: str) -> str:
    """Also the visual baseline name stem (with the theme appended)."""
    return f"{name}-{viewport}" + ("" if profile == "full" else f"-{profile}")
