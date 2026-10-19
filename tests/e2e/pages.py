"""Every routable page, for the a11y and visual suites. Add new views here.

(name, path, viewports): kiosk is only ever shown on the Pi's 1920px monitor.
"""

PAGES: list[tuple[str, str, tuple[str, ...]]] = [
    ("today", "/today", ("mobile", "desktop")),
    ("upcoming", "/upcoming", ("mobile", "desktop")),
    ("health", "/health", ("mobile", "desktop")),
    ("styleguide", "/styleguide", ("mobile", "desktop")),
    ("kiosk", "/kiosk", ("kiosk",)),
    ("offline", "/offline", ("mobile",)),
]
VIEWPORTS = {"mobile": (390, 844), "desktop": (1280, 800), "kiosk": (1920, 1080)}
