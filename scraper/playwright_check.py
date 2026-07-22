from playwright.sync_api import (
    sync_playwright,
    TimeoutError
)

import os
import re
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv

load_dotenv()

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

AUTH_STATE = BASE_DIR / "auth.json"

# ----------------------------------------------------------
# Import the login flow (NEW - single-command workflow).
# ----------------------------------------------------------
#
# Needed so ensure_authenticated() (below) can trigger a login
# automatically when auth.json is missing or expired, instead of
# requiring someone to run `python login.py` by hand first.
#
# login.py lives in this SAME folder as playwright_check.py. When
# scraper.py is run directly (`python scraper.py` from inside this
# folder), Python already puts this folder on sys.path, so the
# plain import below just works. The fallback only matters if this
# module gets imported some other way (e.g. as `scraper.
# playwright_check` from a project root that's on sys.path instead)
# - in that case we add THIS file's own directory (not BASE_DIR,
# which points at the project root one level up) to sys.path,
# since that's where login.py actually is.
# ----------------------------------------------------------
try:
    # When imported as scraper.playwright_check
    from .login import perform_login

except ImportError:
    try:
        # When executed directly from the scraper folder
        from login import perform_login

    except ImportError as e:
        raise ImportError(
            "Unable to import perform_login from login.py"
        ) from e
# ----------------------------------------------------------
# Dashboard / Widget Configuration
# ----------------------------------------------------------

# Number of times a selector will be retried.
# PERF: was 5, then 3 - now capped at 2 (requirement #4: "retry a
# failed click at most 2 times"). A selector that's genuinely
# present still matches on attempt 1 either way - this only caps
# the worst case for a missing/slow selector instead of slowing
# down the common case.
MAX_SELECTOR_RETRIES = 2

# Delay (milliseconds) between retries.
# PERF: was 1000ms, then 400ms - now 200ms. A missing/slow
# selector still fails fast; this just removes wasted idle time
# on every retry across what used to be hundreds of retries over
# a full crawl.
RETRY_DELAY = 200

# Time to wait for widgets to appear.
WIDGET_TIMEOUT = 15000


# ----------------------------------------------------------
# Application Context (NEW)
# ----------------------------------------------------------
#
# NEW (requirement #4):
#
# We need to remember which hostname the app itself lives on,
# so that at any point later in the crawl we can tell "this
# link/click stays inside the application" from "this link
# leaves the application" (e.g. the public Ideabytes website,
# Facebook, LinkedIn, etc).
#
# This is set ONCE, right after the dashboard is first opened
# in crawl_dashboard_sections(). Using a plain dict (instead of
# a bare module-level variable) means we can update it from
# inside a function without needing `global` everywhere.
# ----------------------------------------------------------
APP_CONTEXT = {
    "hostname": None
}


# ----------------------------------------------------------
# Wait For The DOM To Actually Stop Changing (NEW)
# ----------------------------------------------------------
#
# NEW - fixes "Auto-refresh support", "WebSocket update handling"
# and "Polling stabilization":
#
# This live dashboard keeps an open WebSocket connection and/or
# polls in the background, which means `page.wait_for_load_state
# ("networkidle")` basically NEVER resolves on its own - the app
# is *designed* to keep the network busy forever. The old code
# waited up to 5 full seconds for networkidle on every single
# section/click and then ALWAYS timed out, burning 5 seconds for
# nothing before even getting to the fixed 1500ms sleep after it.
#
# A much better signal for "has this screen actually finished
# updating" - whether the update came from a WebSocket push, a
# polling XHR, or Angular's own re-render - is the DOM itself: a
# MutationObserver tells us the instant nothing has changed for
# `quiet_ms` in a row, and we still cap the TOTAL wait at
# `max_wait_ms` so a dashboard that never truly settles (e.g. a
# live chart that ticks every second) can't hang the crawl.
#
# This resolves FAST (typically 150-400ms) on a screen that has
# already settled, and never waits longer than max_wait_ms on
# one that hasn't - both are big wins over the old fixed sleeps.
# ----------------------------------------------------------
def wait_for_ui_stable(page, max_wait_ms=2500, quiet_ms=350):
    """
    Waits until the DOM stops mutating for `quiet_ms` milliseconds
    in a row, or until `max_wait_ms` total has elapsed - whichever
    comes first. Covers auto-refreshing widgets, WebSocket-pushed
    updates, and polling-driven updates in one shot, since all
    three eventually show up as DOM mutations.
    """

    script = """
        ([maxWaitMs, quietMs]) => new Promise((resolve) => {
            const start = Date.now();
            let quietTimer = null;

            const finish = (reason) => {
                clearTimeout(quietTimer);
                observer.disconnect();
                resolve(reason);
            };

            const observer = new MutationObserver(() => {
                if (Date.now() - start >= maxWaitMs) {
                    finish('max-wait-reached');
                    return;
                }
                clearTimeout(quietTimer);
                quietTimer = setTimeout(
                    () => finish('quiet'),
                    quietMs
                );
            });

            observer.observe(document.body, {
                childList: true,
                subtree: true,
                attributes: true,
                characterData: true
            });

            // Nothing mutated at all - resolve as soon as the
            // initial quiet window passes instead of waiting
            // the full max_wait_ms for no reason.
            quietTimer = setTimeout(
                () => finish('quiet-initial'),
                quietMs
            );

            // Absolute safety net in case the observer never
            // fires and the quiet timer somehow gets lost.
            setTimeout(
                () => finish('timeout-fallback'),
                maxWaitMs
            );
        })
    """

    try:

        reason = page.evaluate(script, [max_wait_ms, quiet_ms])

        print(f"UI stabilized ({reason}).")

    except Exception as e:

        print(f"UI stability check skipped: {e}")


# ----------------------------------------------------------
# Wait For Charts/Canvases To Finish Drawing (NEW)
# ----------------------------------------------------------
#
# NEW - fixes "Chart stabilization: Missing":
#
# Charting libraries (Chart.js, ECharts, D3-on-canvas, ...) draw
# onto a <canvas>, which is NOT part of the DOM tree that
# MutationObserver watches - a chart can still be mid-animation
# even after wait_for_ui_stable() reports the DOM is quiet.
#
# This samples each canvas's actual pixel content (toDataURL())
# twice, a short beat apart, and keeps sampling until two
# consecutive samples match (the chart stopped changing) or
# `max_wait_ms` is used up. If there are no canvases on screen at
# all, this returns immediately at ~zero cost.
# ----------------------------------------------------------
def wait_for_charts_stable(page, max_wait_ms=1500, sample_gap_ms=250):
    """
    Waits until every <canvas> on the page renders the same pixel
    content across two consecutive samples, or until max_wait_ms
    has elapsed. Costs ~0ms when there are no canvases at all.
    """

    try:

        canvas_count = page.locator("canvas").count()

    except Exception:

        canvas_count = 0

    if canvas_count == 0:

        # No charts on this screen - nothing to stabilize.
        return

    print(f"Stabilizing {canvas_count} chart canvas(es)...")

    sample_script = """
        () => Array.from(document.querySelectorAll('canvas'))
            .map(c => {
                try {
                    return c.toDataURL();
                } catch (e) {
                    // Tainted/cross-origin canvas - can't read
                    // pixels, treat as "always different" so we
                    // fall through to the max_wait_ms cap below
                    // instead of looping forever.
                    return 'unreadable-' + Math.random();
                }
            })
    """

    elapsed = 0

    try:

        previous = page.evaluate(sample_script)

    except Exception as e:

        print(f"Chart stabilization skipped: {e}")

        return

    while elapsed < max_wait_ms:

        page.wait_for_timeout(sample_gap_ms)

        elapsed += sample_gap_ms

        try:

            current = page.evaluate(sample_script)

        except Exception:

            break

        if current == previous:

            print("Charts stable.")

            return

        previous = current

    print("Chart stabilization timeout reached - continuing anyway.")


# ----------------------------------------------------------
# Wait for Dashboard
# ----------------------------------------------------------
def wait_for_dashboard(page, quick=False):
    """
    Wait until the Angular dashboard has settled.

    This helps avoid scraping while the dashboard
    is still rendering charts or live widgets.

    Still never waits on networkidle - a live app with an open
    WebSocket/polling loop never goes network-idle, so that wait
    used to ALWAYS burn its full timeout for nothing. Uses
    wait_for_ui_stable() (DOM-mutation based - handles auto-
    refresh, WebSocket pushes, and polling alike) followed by
    wait_for_charts_stable() (canvas-pixel based) instead.

    PERF (requirement #3 - "avoid waiting after every click, use
    only the minimum required waits"): this used to be the ONLY
    wait function, and every single click in the old
    explore_ui_element() called this (full "ib-iot-root" wait +
    2500ms UI-stable window + 1500ms chart window) followed
    IMMEDIATELY by a second, separate find_widget() call that
    re-waited on that exact same "ib-iot-root" selector again -
    two redundant passes doing almost the same thing after every
    click, dozens/hundreds of times per crawl.

    `quick=True` merges both call sites into this ONE function
    with a much smaller settle window, meant for "the screen just
    changed a little after a click" - it skips the (already near-
    instant, but still a round trip) `ib-iot-root` re-check, since
    by the time we're clicking inside a section the app shell is
    already known to be loaded. `quick=False` (default) keeps the
    original, fuller check used once per section on first entry.
    """

    ui_max_wait = 800 if quick else 1800
    ui_quiet = 200 if quick else 300
    chart_max_wait = 600 if quick else 1200

    if not quick:

        print("Loading dashboard...")

        try:

            page.wait_for_selector("ib-iot-root", timeout=WIDGET_TIMEOUT)

        except TimeoutError:

            print("Angular root was not detected.")

        page.wait_for_load_state("domcontentloaded")

    else:

        try:

            page.wait_for_load_state("domcontentloaded", timeout=1500)

        except Exception:

            pass

    # Wait until the DOM stops mutating (auto-refresh / WebSocket
    # push / polling all show up here) instead of waiting on a
    # networkidle event that this app will never actually reach.
    wait_for_ui_stable(page, max_wait_ms=ui_max_wait, quiet_ms=ui_quiet)

    # Give any canvas-based charts a chance to finish drawing.
    # This is already a no-op (~0ms) when there are no canvases on
    # screen at all, so it's cheap to call even after every click.
    wait_for_charts_stable(page, max_wait_ms=chart_max_wait)

    if not quick:
        print("Dashboard ready.")


# ----------------------------------------------------------
# Wait for Live Widgets
# ----------------------------------------------------------
def wait_for_widget(
        page,
        selector,
        retries=MAX_SELECTOR_RETRIES
):
    """
    Wait until a widget becomes visible.

    Live dashboards sometimes refresh widgets,
    so this function retries several times.
    """

    print("Waiting for widgets...")

    for attempt in range(retries):

        try:

            page.wait_for_selector(
                selector,
                timeout=2000
            )

            widget = page.locator(selector)

            if widget.is_visible():

                print("Widget detected.")

                return widget

        except TimeoutError:

            print(
                f"Retry {attempt + 1}/{retries}"
            )

        # Wait briefly before trying again.
        page.wait_for_timeout(RETRY_DELAY)

    print("Widget not found.")

    return None


# ----------------------------------------------------------
# Find Widget Using Multiple Selectors
# ----------------------------------------------------------
def find_widget(
        page,
        selectors
):
    """
    Try multiple selectors.

    If one selector fails because the widget
    refreshed, continue trying the remaining
    selectors.
    """

    print("Searching for widget...")

    for selector in selectors:

        print(
            f"Trying selector: {selector}"
        )

        widget = wait_for_widget(
            page,
            selector
        )

        if widget:

            return widget

        print("Trying next selector...")

    print("No working selector found.")

    return None


# ----------------------------------------------------------
# Click With Retry
# ----------------------------------------------------------
def click_with_retry(
        page,
        selectors,
        retries=MAX_SELECTOR_RETRIES
):
    """
    Click a widget using multiple candidate selectors, retrying a
    couple of times per selector.

    PERF (requirement #4 - "retry at most 2 times, avoid nested
    retry loops"): this used to be a selector loop wrapping a
    retry loop, each attempt doing its own wait_for_selector()
    PLUS a separate is_visible() check PLUS a fixed RETRY_DELAY
    sleep on every single branch (timeout, detached, not-visible,
    generic exception all fell through to the same sleep). That's
    collapsed into one loop per selector: wait_for_selector()
    already implies "exists AND visible" for the default 'visible'
    state, so the extra is_visible() round trip is gone, and the
    sleep only happens once, right before the next attempt.
    """

    for selector in selectors:

        for attempt in range(retries):

            try:

                widget = page.locator(selector)

                widget.wait_for(state="visible", timeout=2000)

                widget.click(timeout=2000)

                print(f"Clicked '{selector}'.")

                return True

            except Exception as e:

                if attempt + 1 < retries:
                    page.wait_for_timeout(RETRY_DELAY)

                else:
                    print(f"'{selector}' failed after {retries} attempts: {e}")

    print("Unable to interact with widget.")

    return False


# ----------------------------------------------------------
# Common Dashboard Widget Selectors
# ----------------------------------------------------------

DASHBOARD_WIDGET_SELECTORS = [
    "ib-iot-root"
]

# ----------------------------------------------------------
# Check Page
# ----------------------------------------------------------
def check_page(url):
    """
    Opens a page using the authenticated session
    and checks whether the page loads correctly.

    This version is improved for live IoT dashboards.
    """

    try:

        with sync_playwright() as p:

            print("Opening Browser...")

            browser = p.chromium.launch(
                headless=False
            )

            # ---------------------------------------
            # Use authenticated session if available
            # ---------------------------------------
            print("=" * 50)
            print("AUTH FILE:", AUTH_STATE)
            print("Exists:", os.path.exists(AUTH_STATE))
            print("=" * 50)
            if os.path.exists(AUTH_STATE):

                context = browser.new_context(
                    storage_state=AUTH_STATE
                )

            else:

                print(
                    "[WARNING] auth.json not found. Using anonymous session."
                )

                context = browser.new_context()

            page = context.new_page()

            print("Opening page...")

            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000
            )

            # ---------------------------------------
            # Wait until dashboard is ready
            # ---------------------------------------
            wait_for_dashboard(page)

            # ---------------------------------------
            # Wait for live widgets
            # ---------------------------------------
            print("Waiting for dashboard widgets...")

            find_widget(
                page,
                DASHBOARD_WIDGET_SELECTORS
            )

            print("Dashboard loaded successfully.")

            # ---------------------------------------
            # Print HTTP status
            # ---------------------------------------
            if response and response.status < 400:

                print(
                    f"Playwright Check: PASS (status {response.status})"
                )

            else:

                print(
                    f"Playwright Check: FAIL "
                    f"(status {response.status if response else 'No Response'})"
                )

            browser.close()

    except TimeoutError:

        print(
            "Playwright Check: FAIL (Timeout)"
        )

    except Exception as e:

        print(
            f"Playwright Check: FAIL — {e}"
        )


# ----------------------------------------------------------
# Get Rendered HTML
# ----------------------------------------------------------
#
# This used to take a `url`, open its OWN browser, navigate
# to that url, grab the HTML, then close the browser again.
#
# That is exactly the "open/close per page" problem we are
# fixing. Now this function takes the SAME `page` object that
# the rest of the crawl is already using. By the time this is
# called, the page has already been moved to the right section
# either by page.goto() (only for the very first/dashboard
# load) or by click_with_retry() (every section after that).
#
# So all this function has to do now is: wait a moment for any
# charts/widgets to settle, then read whatever HTML is
# currently on screen.
# ----------------------------------------------------------
def get_rendered_html(page):
    """
    Returns the fully rendered HTML of whatever section is
    currently open on the shared `page`.

    Does NOT open a browser and does NOT navigate anywhere.
    Call wait_for_dashboard()/find_widget() (or use
    go_to_section() below) before calling this, so the content
    has actually finished loading.
    """

    try:

        # ---------------------------------------
        # CHANGED: used to be a flat 2000ms sleep here on top of
        # whatever the caller already waited. Callers now run
        # wait_for_dashboard() (which itself calls
        # wait_for_ui_stable() + wait_for_charts_stable()) right
        # before this, so the heavy lifting is already done. Just
        # a tiny buffer remains, to stay quick.
        # ---------------------------------------
        page.wait_for_timeout(150)

        # ---------------------------------------
        # Capture HTML of the current section.
        # ---------------------------------------
        html = page.content()

        print("Rendered HTML captured.")

        return html

    except TimeoutError:

        print(
            "Timed out while rendering page."
        )

        return None

    except Exception as e:

        print(
            f"get_rendered_html failed: {e}"
        )

        return None


# ----------------------------------------------------------
# Get Rendered Routes
# ----------------------------------------------------------
def get_rendered_routes(url):
    """
    Discover routes after Angular has rendered.

    This version waits for the dashboard and live
    widgets before collecting links.
    """

    try:

        with sync_playwright() as p:

            print("Opening Browser...")

            browser = p.chromium.launch(
                headless=True
            )

            # ---------------------------------------
            # Use authenticated session
            # ---------------------------------------
            print("=" * 50)
            print("AUTH FILE:", AUTH_STATE)
            print("Exists:", os.path.exists(AUTH_STATE))
            print("=" * 50)
            if os.path.exists(AUTH_STATE):

                context = browser.new_context(
                    storage_state=AUTH_STATE
                )

            else:

                print(
                    "[WARNING] auth.json not found. Using anonymous session."
                )

                context = browser.new_context()

            page = context.new_page()

            print("Loading page...")

            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000
            )

            # ---------------------------------------
            # Wait until dashboard is ready
            # ---------------------------------------
            wait_for_dashboard(page)

            # ---------------------------------------
            # Wait for live dashboard widgets
            # ---------------------------------------
            print("Waiting for widgets...")

            find_widget(
                page,
                DASHBOARD_WIDGET_SELECTORS
            )

            # Give Angular a little extra time
            # in case widgets are refreshing.
            page.wait_for_timeout(2000)

            print("Collecting routes...")

            routes = []

            anchors = page.locator("a")

            count = anchors.count()

            print(f"Found {count} anchor elements.")

            for i in range(count):

                try:

                    anchor = anchors.nth(i)

                    # Skip invisible links.
                    if not anchor.is_visible():
                        continue

                    href = anchor.get_attribute("href")

                    if not href:
                        continue

                    text = anchor.inner_text().strip()

                    routes.append({

                        "text": text,

                        "href": urljoin(
                            url,
                            href
                        )

                    })

                except Exception as e:

                    # If Angular refreshes the DOM
                    # while looping through links,
                    # continue instead of failing.
                    error = str(e).lower()

                    if (
                        "detached" in error
                        or "not attached" in error
                    ):

                        print(
                            "Widget refreshed while reading routes."
                        )

                    else:

                        print(
                            f"Skipping link: {e}"
                        )

                    continue

            print(
                f"Collected {len(routes)} routes."
            )

            browser.close()

            return routes

    except TimeoutError:

        print(
            "Timed out while collecting routes."
        )

        return []

    except Exception as e:

        print(
            f"get_rendered_routes failed: {e}"
        )

        return []


# ----------------------------------------------------------
# Sidebar / Menu Navigation Selectors
# ----------------------------------------------------------
#
# Instead of loading each internal page with its own
# page.goto(url), we now click the app's own sidebar/menu,
# exactly like a real user would in an Angular SPA.
#
# Each section has a list of possible selectors because we
# don't know the exact DOM markup of the live app yet. Update
# these once you inspect the real sidebar (DevTools) so
# click_with_retry() finds the correct element on the first
# try instead of falling through the fallback list.
# ----------------------------------------------------------
SECTION_NAV_SELECTORS = {

    "Reports": [
        "a:has-text('Reports')",
        "button:has-text('Reports')",
        "[data-testid='nav-reports']",
        "text=Reports"
    ],

    "Alerts": [
        "a:has-text('Alerts')",
        "button:has-text('Alerts')",
        "[data-testid='nav-alerts']",
        "text=Alerts"
    ],

    "Devices": [
        "a:has-text('Devices')",
        "button:has-text('Devices')",
        "[data-testid='nav-devices']",
        "text=Devices"
    ],

    "Alarms": [
        "a:has-text('Alarms')",
        "button:has-text('Alarms')",
        "[data-testid='nav-alarms']",
        "text=Alarms"
    ],

    "Users": [
        "a:has-text('Users')",
        "button:has-text('Users')",
        "[data-testid='nav-users']",
        "text=Users"
    ],

}


# ----------------------------------------------------------
# Sections To Visit, But NEVER Click Inside (NEW)
# ----------------------------------------------------------
#
# UNSAFE_CLICK_WORDS blocks anything whose OWN visible text/label
# says "Delete", "Remove", etc. That isn't enough for a section like
# "Rules": actions there can be destructive without ever showing a
# literal "Delete" label on the element that triggers them - e.g. a
# toggle that flips a rule's enabled/disabled state, or a "..."
# context menu whose items render inside the same CDK overlay
# Playwright treats as a popup and gets explored automatically.
# Rather than trying to out-guess every possible destructive
# interaction pattern, any section listed here is still opened and
# its top-level HTML is still captured (via on_section_ready), but
# explore_section_elements() is skipped entirely - nothing inside
# it is ever clicked. Matched case-insensitively against the
# section's "name". Add more names here if another section turns
# out to have the same problem.
# ----------------------------------------------------------

# ----------------------------------------------------------
# SAFE MODE (HARD OVERRIDE)
# ----------------------------------------------------------
# When True (the default, and it should STAY True for any run
# against a real/production company system), the crawler NEVER
# clicks anything inside a section - it only navigates via the
# sidebar and captures the HTML that's already on screen. This
# is the only setting that guarantees zero edits/deletes, because
# it does not depend on correctly anticipating every dangerous
# label/word/control - it removes the ability to click at all.
#
# Only set this to False if you specifically need the "explore
# elements inside a section" behaviour AND you are running against
# a throwaway/staging environment you're fully comfortable with
# the crawler mutating.
# ----------------------------------------------------------
SAFE_MODE = False

# READ_ONLY_SECTIONS is matched by SUBSTRING (not exact equality)
# against the lowercased section name, so naming variants like
# "Automation Rules", "Rule Engine", "Alarm History", etc. are all
# still caught even if the exact label differs from the string
# listed here. This closes the gap where an exact-match check could
# silently fail to protect a section whose real name doesn't match
# the string below word-for-word.
READ_ONLY_SECTIONS = set()


# ----------------------------------------------------------
# Element Discovery Configuration
# ----------------------------------------------------------
#
# PERF (requirements #1, #2, #5): the crawler no longer does a
# recursive, depth-first walk that re-discovers the whole page
# after every single click (see explore_section_elements() further
# down for why that was the single biggest source of the 60-90
# minute runtime). Each section now gets ONE discovery pass; every
# safe element found in that pass is clicked exactly once. A
# MAX_EXPLORATION_DEPTH guard is no longer needed because there is
# no recursion left to bound.

# FIXED - the cap used to apply to EVERY selector, including the
# generic icon-like ones ("i", "svg", "mat-icon"). A page can have
# a dozen genuinely different <i> icons (nav icons, status icons,
# the "View Trend" icon, ...) that are NOT a repeated pattern at
# all - capping by raw selector string was cutting the real "View
# Trend" icon off the list before the crawler ever reached it,
# just because it happened to be the 17th/18th <i> tag discovered.
# Now the cap only applies to selectors that are actually prone to
# generating large repeated lists (dropdown panels, listboxes,
# chips) - the same category the old is_dropdown_option() targeted,
# this is just a backstop for anything that slips past the dropdown-
# option filter built into the discovery script below.
MAX_SAME_SELECTOR_PER_SCREEN = 8

REPEAT_PRONE_SELECTORS = {
    "[class*='panel']",
    "[role='listbox']",
    "[class*='dropdown']",
    "[class*='chip']",
}

# ----------------------------------------------------------
# Words that make an element UNSAFE to click.
# ----------------------------------------------------------
#
# If any of these appear in the element's visible text, or in the
# href it points to, we skip it completely. This covers destructive
# actions (logout, delete, ...) AND anything that would take the
# crawler outside the application (support, privacy, the Ideabytes
# marketing site, social media, etc).
UNSAFE_CLICK_WORDS = [
    "logout",
    "log out",
    "sign out",
    "exit",
    "delete",
    "remove",
    "terminate",
    "shutdown",
    "restart",
    "save",
    "submit",
    "cancel",
    "reset",
    "create",
    "update",
    "clear",
    "privacy",
    "terms",
    "support",
    "documentation",
    "help",
    "website",
    "facebook",
    "linkedin",
    "twitter",
    "youtube",
    "mailto",
    "tel",
    "ideabytes",

    # ADDED: soft-delete / state-mutating words that were missing.
    # These don't sound as destructive as "delete" but change data
    # just as permanently (acknowledging/muting an alarm removes it
    # from the active list; enabling/disabling a rule changes its
    # behaviour), and none of them were caught before.
    "acknowledge",
    "ack",
    "dismiss",
    "resolve",
    "resolved",
    "mute",
    "unmute",
    "snooze",
    "archive",
    "unarchive",
    "purge",
    "discard",
    "enable",
    "disable",
    "activate",
    "deactivate",
    "toggle",
    "on/off",
    "publish",
    "unpublish",
    "approve",
    "reject",
    "block",
    "unblock",
    "ban",
    "revoke",
    "assign",
    "unassign",
    "pause",
    "resume",
    "stop",
    "start",
    "run",
    "execute",
    "trigger",
    "add",
    "edit",
    "modify",
    "change",
    "configure",
    "install",
    "uninstall",
    "deploy",
    "confirm",
    "yes",
    "ok",
    "apply",
]

# CSS selectors used to discover clickable elements. Prefers
# selectors that are actually part of the Angular app's own UI
# (buttons, [routerLink], [data-testid], cards, tiles, tabs,
# accordions, dashboard widgets, form controls, icon-only buttons)
# over a bare "a", which would match every anchor on the page
# (Privacy, Support, Facebook, LinkedIn, the marketing site, ...).
# Anchors are still discovered - sidebars are often plain anchors -
# but every anchor is additionally checked for an unsafe/external
# href inside the discovery script below before it's ever added to
# the clickable list.
CLICKABLE_SELECTORS = [
    "button",
    "[role='button']",
    "[routerLink]",
    "[routerlink]",
    "[data-testid]",
    "a[routerLink]",
    "a[routerlink]",
    "a",
    "div[onclick]",
    "span[onclick]",
    "[class*='card']",
    "[class*='tile']",
    "[class*='tab']",
    "[class*='accordion']",
    "[class*='expansion']",
    "[class*='panel']",
    "[class*='pagination']",
    "[class*='filter']",
    "[class*='widget']",
    "[class*='expand']",
    "ib-card",
    "ib-widget",
    "ib-tab",
    "ib-button",
    "ib-tile",
    "ib-panel",
    "[data-click]",
    "[data-action]",

    # Dropdown/segmented FILTER controls only. These change what's
    # displayed/viewed, not the underlying data, so they're safe to
    # explore. NOTE: "select"/"mat-select" only ever get OPENED and
    # CLOSED here - the discovery script further down explicitly
    # skips role="option"/mat-option/ng-option rows, so no option is
    # ever actually selected/committed.
    "select",
    "mat-select",
    "[role='combobox']",
    "[role='listbox']",
    "[role='tab']",
    "[role='menuitem']",
    "[class*='dropdown']",
    "[class*='segment']",
    "[class*='select']",

    # REMOVED ON PURPOSE - "[class*='chip']": mat-chip / removable
    # chip components very often run a remove/deselect handler on a
    # click of the CHIP ITSELF (not just its small "x" icon), and
    # the chip's own visible text is just the tag/item name (e.g.
    # "Team A"), not a word like "remove" - so the word filter can
    # never catch this case. Rather than guess, chips are excluded
    # from the clickable set entirely.

    # REMOVED ON PURPOSE - these control types directly flip real
    # data state (e.g. acknowledging/muting an alarm, enabling or
    # disabling a rule) and there's no reliable label text to filter
    # them by, since Angular Material toggles/checkboxes/switches
    # often carry no "delete"/"edit"-style wording at all. Rather
    # than guess, they're excluded from the clickable set entirely:
    # "mat-button-toggle", "mat-checkbox", "mat-radio-button",
    # "mat-slide-toggle", "input[type='checkbox']",
    # "input[type='radio']", "[role='switch']", "[role='checkbox']",
    # "[role='radio']", "[class*='toggle']", "[class*='switch']"

    # Icon-only buttons (no visible text at all). Angular click
    # bindings ((click)="...") don't add a literal onclick="" HTML
    # attribute the way plain JS does, so these need their own
    # selectors. The discovery script below only KEEPS one of these
    # if it actually looks interactive (cursor: pointer, or sits
    # inside a clickable ancestor), so this doesn't turn every
    # decorative logo/icon on the page into a click target.
    "mat-icon",
    "svg",
    "i",
    "img",
]

# Tags that are only worth clicking if they actually LOOK
# interactive (cursor:pointer, or inside a clickable ancestor).
ICON_LIKE_TAGS = ["mat-icon", "svg", "i", "img"]


# ----------------------------------------------------------
# Shared "Sibling Identity" Key
# ----------------------------------------------------------
#
# Two entries in CLICKABLE_SELECTORS can both match the exact same
# physical element (a stat card is simultaneously "[role='button']"
# AND "[class*='card']"). Keying identity off the visible TEXT ALONE
# (when there is any) means "Good Status" means the same thing
# regardless of which selector matched it, so it can be recognised
# as an already-known sibling instead of a fresh element. Icon-only
# elements have no text to key off, so they fall back to
# selector+text (empty text) there.
# ----------------------------------------------------------
def sibling_identity(selector, text):

    if text:
        return _normalize_counts(text)

    return f"{selector}|{text}"


# ----------------------------------------------------------
# Strip Volatile Live-Data Numbers From Text
# ----------------------------------------------------------
#
# This dashboard is a LIVE IoT app - temperature/humidity readings
# and device counters change every few seconds via WebSocket/
# polling. Collapsing every run of digits to a single '#' means
# "24.2 C" and "23.9 C" (the same physical card, sampled a moment
# apart) are recognised as the same label instead of looking like
# two different elements.
# ----------------------------------------------------------
def _normalize_counts(text):

    if not text:
        return text

    return re.sub(r"\d+([.,]\d+)?", "#", text)


# ----------------------------------------------------------
# Batched Element Discovery Script (PERF - the core fix for #1/#5)
# ----------------------------------------------------------
#
# The old discover_clickable_elements() looped over every matched
# element for every selector and made a SEPARATE Playwright round
# trip for each of: is_visible(), is_disabled(), get_attribute()
# (x3), inner_text(), plus - for icon-like tags - a whole extra
# element.evaluate() call (is_interactive_icon) with two print()
# calls per icon. On a screen with a few hundred candidate elements
# across ~30 selectors, that's potentially thousands of individual
# round trips to the browser for ONE screen, and this ran again
# after every click and again for every sibling in the old
# recursive walk.
#
# This does the exact same filtering (visibility, disabled state,
# readonly, aria-hidden, unsafe words, icon interactivity, "is this
# inside the sidebar/nav", "is this a dropdown option row", "is this
# anchor's href safe/internal") in ONE JavaScript pass PER SELECTOR,
# via Playwright's evaluate_all(), which sends the whole matched
# element list to the browser once and gets back a small, already-
# filtered list of {index, text} dicts. That turns "thousands of
# round trips" into "about 30" (one per selector) - the single
# biggest reason a full crawl used to take 60-90 minutes.
# ----------------------------------------------------------
_DISCOVERY_SCRIPT = r"""
(elements, args) => {
    const { unsafeWords, appHostname } = args;
    const results = [];

    const iconLikeTags = ['mat-icon', 'svg', 'i', 'img'];

    elements.forEach((el, i) => {
        try {
            const visible = el.checkVisibility
                ? el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })
                : el.offsetParent !== null;
            if (!visible) return;

            if (el.disabled) return;
            if (el.getAttribute('aria-disabled') === 'true') return;
            if (el.hasAttribute('readonly')) return;
            if (el.getAttribute('aria-hidden') === 'true') return;

            const text = (el.innerText || '').trim();

            // BUGFIX: the safety check used to test ONLY visible
            // text. An icon-only Delete/Remove/Logout control (a
            // bare trash-can SVG with no text at all, labelled
            // purely via aria-label="Delete" or title="Delete
            // device") has empty innerText, so it walked straight
            // past the unsafe-word filter and got clicked anyway.
            // Now the same word list is also checked against
            // aria-label/title/matTooltip/name/data-testid/
            // data-action on the element itself, PLUS the same
            // attributes on any nearby descendant (icon-only
            // buttons very often carry the label on a child <svg>/
            // <mat-icon> rather than on the button itself).
            const labelAttrs = ['aria-label', 'title', 'matTooltip', 'name', 'data-testid', 'data-action'];

            let safetyParts = [text];

            labelAttrs.forEach((attr) => {
                const v = el.getAttribute && el.getAttribute(attr);
                if (v) safetyParts.push(v);
            });

            try {
                el.querySelectorAll('[aria-label], [title], [matTooltip]').forEach((child) => {
                    ['aria-label', 'title', 'matTooltip'].forEach((attr) => {
                        const v = child.getAttribute(attr);
                        if (v) safetyParts.push(v);
                    });
                });
            } catch (e) { /* malformed subtree - ignore */ }

            const lowered = safetyParts.join(' ').toLowerCase();

            for (let w = 0; w < unsafeWords.length; w++) {
                if (lowered.indexOf(unsafeWords[w]) !== -1) return;
            }

            // HARD BLOCK: destructive icon buttons (a red trash-can,
            // a "danger"/"warn" styled button) very often carry NO
            // text and NO aria-label/title at all - just a CSS class
            // like "delete-btn", "btn-danger", "text-danger", or a
            // red icon color. The word-based check above can't catch
            // these because there's no text to check. This walks the
            // element and a few ancestors looking for exactly those
            // signals and blocks the click outright if found.
            const dangerClassHints = [
                'danger', 'warn', 'delete', 'remove', 'trash', 'bin',
                'destructive'
            ];
            {
                let node = el;
                for (let d = 0; d < 4 && node; d++) {
                    const cls = (node.className && node.className.toString)
                        ? node.className.toString().toLowerCase() : '';
                    if (dangerClassHints.some((h) => cls.indexOf(h) !== -1)) return;
                    node = node.parentElement;
                }

                // Red-colored icon/button as a last-resort visual
                // signal (many delete icons are colored red purely
                // via inline style/theme color with no telltale
                // class name at all).
                try {
                    const style = window.getComputedStyle(el);
                    const color = (style.color || '').replace(/\s+/g, '');
                    // matches common "red" rgb ranges, e.g. rgb(2xx,
                    // low, low) - a rough heuristic, intentionally
                    // over-inclusive since a missed real button here
                    // just means one fewer thing gets explored.
                    const m = color.match(/^rgba?\((\d+),(\d+),(\d+)/);
                    if (m) {
                        const r = parseInt(m[1], 10), g = parseInt(m[2], 10), b = parseInt(m[3], 10);
                        if (r > 150 && r - g > 60 && r - b > 60) return;
                    }
                } catch (e) { /* ignore */ }
            }

            const tag = el.tagName.toLowerCase();

            // Icon-only elements (mat-icon/svg/i/img) are frequently
            // just decoration. Only keep one if it actually looks
            // interactive: cursor:pointer, sits inside something
            // clearly interactive, or carries an aria-label/title/
            // tooltip/telltale class a couple ancestors up.
            if (iconLikeTags.indexOf(tag) !== -1) {
                let keep = window.getComputedStyle(el).cursor === 'pointer';

                if (!keep) {
                    keep = !!el.closest(
                        'button, a, [role="button"], [routerLink], ' +
                        '[routerlink], [tabindex]:not([tabindex="-1"])'
                    );
                }

                if (!keep) {
                    let node = el;
                    for (let d = 0; d < 4 && node; d++) {
                        const aria = node.getAttribute && node.getAttribute('aria-label');
                        const title = node.getAttribute && node.getAttribute('title');
                        const tooltip = node.getAttribute && node.getAttribute('matTooltip');
                        if (aria || title || tooltip) { keep = true; break; }

                        const cls = (node.className && node.className.toString)
                            ? node.className.toString().toLowerCase() : '';
                        if (/view|trend|expand|history|graph|chart|info|detail|icon-btn|iconbutton/.test(cls)) {
                            keep = true;
                            break;
                        }
                        node = node.parentElement;
                    }
                }

                if (!keep) return;
            }

            // Sidebar/menu navigation is handled explicitly and in
            // order by crawl_dashboard_sections() - never treat those
            // same links as in-page content here.
            if (el.closest(
                'nav, aside, mat-sidenav, [class*="sidebar" i], ' +
                '[class*="sidenav" i], [class*="side-nav" i], ' +
                '[class*="main-menu" i], [role="navigation"], ' +
                'ib-iot-app-menu, ib-iot-sub-navigation, ' +
                'ib-iot-app-header, ib-iot-app-footer'
            )) return;

            // A <select>/<ng-select>/<mat-select> is worth opening
            // once, but each individual OPTION row inside it (role=
            // "option", ng-option, mat-option, ...) is a form value,
            // not its own screen - skip those so the crawler never
            // picks through 31 calendar days or every dropdown value.
            if (el.getAttribute('role') === 'option') return;
            const clsStr = (el.className && el.className.toString)
                ? el.className.toString().toLowerCase() : '';
            if (/(^|\s)ng-option(\s|$)/.test(clsStr)) return;
            if (tag === 'mat-option') return;
            if (el.closest(
                '[role="listbox"], ng-dropdown-panel, .ng-dropdown-panel, ' +
                '[class*="options-list" i]'
            )) return;

            // Anchor href safety: never open a new tab, never follow
            // javascript:void/mailto:/tel:, never leave the app's own
            // hostname.
            if (tag === 'a') {
                const href = el.getAttribute('href');
                const target = el.getAttribute('target');

                if (href) {
                    const lh = href.trim().toLowerCase();

                    if (target && target.trim().toLowerCase() === '_blank') return;

                    if (
                        lh.startsWith('javascript:void') ||
                        lh.startsWith('mailto:') ||
                        lh.startsWith('tel:')
                    ) return;

                    if (appHostname) {
                        try {
                            const resolved = new URL(href, window.location.href);
                            if (resolved.hostname && resolved.hostname !== appHostname) return;
                        } catch (e) { /* relative/unparsable href - allow it through */ }
                    }
                }
            }

            results.push({ index: i, text: text });

        } catch (e) {
            // Skip a single broken element instead of failing the
            // whole batch for this selector.
        }
    });

    return results;
}
"""


def discover_clickable_elements(page, exclude_keys=None, root=None):
    """
    Looks through CLICKABLE_SELECTORS and returns a plain list of
    dicts describing every SAFE, VISIBLE, ENABLED element found on
    the page right now:

        {"selector": "...", "text": "...", "index": 0, "root": None}

    Unsafe elements (Logout, Delete, Save, Privacy, external links,
    dropdown option rows, sidebar/nav links, ...) are filtered out
    entirely inside the browser - see _DISCOVERY_SCRIPT above.

    `exclude_keys`, if given, is a set of sibling_identity() strings
    to leave out entirely (used to stop sibling filter cards - e.g.
    Total Devices / Reporting Devices / Good Status - from being
    treated as duplicates of each other).

    `root`, if given, is a Locator (e.g. an open dialog/popup) to
    search WITHIN instead of the whole page - so that when a modal
    is open, only the controls inside it are discovered.
    """

    search_root = root if root is not None else page

    app_hostname = APP_CONTEXT.get("hostname")

    args = {"unsafeWords": UNSAFE_CLICK_WORDS, "appHostname": app_hostname}

    found = []

    for selector in CLICKABLE_SELECTORS:

        try:

            matches = search_root.locator(selector).evaluate_all(_DISCOVERY_SCRIPT, args)

        except Exception:

            continue

        for match in matches:

            text = match["text"]

            label_key = sibling_identity(selector, text)

            if exclude_keys and label_key in exclude_keys:
                continue

            found.append({
                "selector": selector,
                "text": text,
                "index": match["index"],
                # Which root this element's index was counted
                # against (whole page, or a scoped dialog Locator) -
                # needed later to click element_root.locator(
                # selector).nth(index) against the SAME index space
                # it was discovered in.
                "root": root,
            })

    # This cap only applies to REPEAT_PRONE_SELECTORS (dropdown
    # panels, listboxes, chips) instead of every selector, so a
    # screen with a dozen genuinely distinct icons (nav icons,
    # status icons, "View Trend", ...) never gets truncated just
    # because of discovery order.
    capped = []
    per_selector_count = {}

    for item in found:

        sel = item["selector"]

        if sel not in REPEAT_PRONE_SELECTORS:
            capped.append(item)
            continue

        per_selector_count[sel] = per_selector_count.get(sel, 0) + 1

        if per_selector_count[sel] > MAX_SAME_SELECTOR_PER_SCREEN:
            continue

        capped.append(item)

    if len(capped) < len(found):
        print(
            f"Capped {len(found) - len(capped)} repeated elements "
            f"(>{MAX_SAME_SELECTOR_PER_SCREEN} of the same selector)."
        )

    print(f"Found {len(capped)} clickable elements.")

    return capped



DIALOG_SELECTORS = [
    "[role='dialog']",
    ".cdk-overlay-container .cdk-overlay-pane",
    "mat-dialog-container",
    ".modal.show",
    ".modal.in",
    "[class*='modal'][class*='open']",
]


def get_open_dialog_locator(page):
    """
    Returns a Playwright locator for the first visible dialog/modal/
    overlay found using DIALOG_SELECTORS, or None if nothing is open.
    """

    for selector in DIALOG_SELECTORS:

        try:

            locator = page.locator(selector)

            if locator.count() > 0 and locator.first.is_visible():

                return locator.first

        except Exception:

            continue

    return None


def has_dialog_open(page):
    """Convenience boolean wrapper around get_open_dialog_locator()."""

    return get_open_dialog_locator(page) is not None


# ----------------------------------------------------------
# Close A Dialog/Modal If One Opened After A Click
# ----------------------------------------------------------
def close_dialog_if_present(page):
    """
    If clicking an element opened a dialog/modal, try to close
    it using a few common close-button patterns. Safe to call
    even if no dialog is open - it just does nothing.

    FIXED (runtime bug - "some popups are not closed correctly
    before traversal continues"): the original selector list only
    matched close buttons that expose the literal text "Close" or
    a ".close" class. Icon-only close controls (a bare "x"
    mat-icon, an SVG button with only an aria-label, a header
    button with no text at all) matched none of these, so the
    dialog silently stayed open. A lingering overlay then blocks
    is_visible() checks for whatever the crawler tries to click
    next, which looked like "controls being skipped". This adds
    icon/aria-label variants, a backdrop click, and an Escape-key
    fallback, and - importantly - VERIFIES the dialog is actually
    gone afterwards instead of assuming the first matched selector
    worked.
    """

    if not has_dialog_open(page):
        return

    dialog_close_selectors = [
        "[role='dialog'] button[aria-label='Close']",
        "[role='dialog'] button[aria-label='close']",
        "[role='dialog'] .close",
        ".modal button.close",
        ".cdk-overlay-container button[aria-label='Close']",
        ".cdk-overlay-container button[aria-label='close']",
        "button:has-text('Close')",
        # Icon-only close buttons: a mat-icon literally named
        # "close", or any button whose class/aria-label mentions
        # "close" without necessarily having visible text.
        "[role='dialog'] button:has(mat-icon:has-text('close'))",
        ".cdk-overlay-container button:has(mat-icon:has-text('close'))",
        "[role='dialog'] [class*='close' i]",
        ".cdk-overlay-container [class*='close' i]",
        "mat-dialog-container button[aria-label*='close' i]",
    ]

    for selector in dialog_close_selectors:

        try:

            locator = page.locator(selector)

            if locator.count() > 0 and locator.first.is_visible():

                locator.first.click(timeout=2000)

                page.wait_for_timeout(250)

                if not has_dialog_open(page):
                    print("Closed a dialog.")
                    return

        except Exception:

            continue

    # Fallback: most CDK/Material/Bootstrap overlays close on
    # Escape or on a backdrop click, even when we can't find an
    # explicit close button that matches any pattern above. Try
    # both before giving up - leaving a dialog open would otherwise
    # block every subsequent visibility check on this page.
    try:

        page.keyboard.press("Escape")
        page.wait_for_timeout(250)

        if not has_dialog_open(page):
            print("Closed a dialog via Escape.")
            return

    except Exception:
        pass

    try:

        backdrop = page.locator(".cdk-overlay-backdrop, .modal-backdrop")

        if backdrop.count() > 0 and backdrop.first.is_visible():

            backdrop.first.click(timeout=1500, force=True)
            page.wait_for_timeout(250)

            if not has_dialog_open(page):
                print("Closed a dialog via backdrop click.")
                return

    except Exception:
        pass

    if has_dialog_open(page):
        print(
            "[WARNING] A dialog is still open and none of the close "
            "strategies worked - subsequent visibility checks on this "
            "page may be blocked by the overlay."
        )


# ----------------------------------------------------------
# Return To The Previous Screen After Exploring An Element
# ----------------------------------------------------------
def return_to_previous_state(page, previous_url, max_attempts=2):
    """
    Tries to get back to `previous_url` after a click navigated
    (or partially navigated) away from it.

    Only actually navigates if the URL changed - a click that
    just expanded something in-place (accordion, tab, filter)
    doesn't need a go_back() at all, and forcing one would just
    lose the very state we spent time exploring.

    FIXED: this used to fall back to `page.goto(previous_url)`
    whenever `page.go_back()` failed or timed out. That was the
    root cause of the crawl randomly "jumping back to the
    dashboard" after only a few button clicks: a hard reload
    (page.goto) of a deep SPA route like
    `.../devices/12/temperature` is a real, fresh HTTP request,
    and this app (like most Angular SPAs without server-side deep
    linking) responds to a hard reload of a non-root route by
    redirecting back to the default dashboard route - exactly the
    symptom reported. `page.go_back()`, in contrast, replays
    client-side browser history and never triggers that redirect.

    Now we ONLY ever use go_back() (retried a couple of times,
    with short timeouts so a missing history entry fails fast
    instead of hanging), and NEVER fall back to a hard goto(). If
    go_back() can't fully restore the exact previous URL, we log
    it and keep crawling from wherever we ended up, rather than
    resetting the whole session back to the dashboard.
    """

    if page.url == previous_url:

        # Nothing to undo - the click didn't change the URL,
        # so we're still on the screen we started from.
        return

    print("Returning to previous state...")

    for attempt in range(max_attempts):

        try:

            page.go_back(timeout=2500)

        except Exception as e:

            print(f"go_back attempt {attempt + 1} failed: {e}")

            break

        try:

            page.wait_for_load_state(
                "domcontentloaded",
                timeout=1500
            )

        except Exception:

            pass

        if page.url == previous_url:
            break

    if page.url != previous_url:

        print(
            f"[WARNING] Could not fully return to {previous_url} "
            f"(currently at {page.url}). NOT forcing a hard reload "
            f"here - that's what used to kick the crawl back to "
            f"the dashboard. Continuing from the current screen "
            f"instead."
        )

    # PERF: quick=True - a lightweight settle wait is enough here.
    # This used to be a full wait_for_dashboard() (including a
    # fresh "ib-iot-root" wait) every time an element's click
    # navigated away, which adds up fast across a whole section.
    wait_for_dashboard(page, quick=True)


# ----------------------------------------------------------
# Click One Element And Capture What It Reveals
# ----------------------------------------------------------
#
# PERF (requirements #1, #2 - the main fix for the 60-90 minute
# runtime): this replaces the old explore_ui_element(), which
# recursively re-discovered the ENTIRE screen after every single
# click and then explored every one of those newly-discovered
# elements too, depth-first, up to 6 levels deep. Two sibling cards
# on a busy screen could each fan out into dozens of "children",
# each of THOSE into more "children", and so on - an effectively
# unbounded multiplier on top of what should have been one click.
# That combinatorial blow-up, not any single slow step, was the
# real reason a crawl took 60-90 minutes.
#
# The new model matches the requested spec exactly: discover once,
# click each safe element once. If the click opens a popup/dialog,
# explore ONLY what's inside that popup (also a single flat pass,
# no recursion), then close it and move on to the next element.
# Nothing here re-discovers the underlying page or recurses into
# whatever a click reveals outside of a popup.
# ----------------------------------------------------------
def click_and_capture_element(
        page,
        section_name,
        section_url,
        element_info,
        on_element_ready=None
):
    """
    Clicks a single already-discovered element, waits briefly for
    the screen to settle, captures its HTML, and - if the click
    opened a popup/dialog - explores every safe element inside that
    popup (one flat pass, not recursive) before closing it.

    Returns the number of elements actually clicked (1 for the
    element itself, plus one for each popup control explored).
    """

    selector = element_info["selector"]
    text = element_info["text"]
    index = element_info["index"]
    element_root = element_info.get("root") or page

    label = text if text else selector
    state_id = f"{section_name}|{selector}|{text}|{index}"

    previous_url = page.url

    try:

        locator = element_root.locator(selector).nth(index)

        if not locator.is_visible():
            return 0

        try:
            locator.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass

        try:
            locator.click(timeout=3000)
        except Exception:
            # Some icon-only controls sit under an overlapping
            # element (tooltip layer, ripple effect) that intercepts
            # the pointer event, so a normal click can silently
            # fail even though the element is visible. force=True
            # dispatches the click directly - what a native Angular
            # (click) binding needs.
            locator.click(timeout=3000, force=True)

    except Exception as e:

        print(f"Could not click '{label}': {e}")

        return 0

    print(f"Clicked: {label}")

    clicked_count = 1

    # PERF: quick=True - a short settle wait instead of the old
    # full wait_for_dashboard() + a second, separate find_widget()
    # call after every click.
    wait_for_dashboard(page, quick=True)

    # If this click took us outside the application entirely (a
    # link we couldn't fully vet before clicking), bail out
    # immediately without exploring anything on the external site.
    app_hostname = APP_CONTEXT.get("hostname")
    current_hostname = urlparse(page.url).hostname

    if app_hostname and current_hostname and current_hostname != app_hostname:

        print(f"Left application domain ({current_hostname}) - returning.")

        return_to_previous_state(page, previous_url)

        if urlparse(page.url).hostname != app_hostname:

            try:
                page.goto(section_url, wait_until="domcontentloaded", timeout=30000)
                wait_for_dashboard(page, quick=True)
            except Exception as e:
                print(f"Recovery navigation failed: {e}")

        return clicked_count

    dialog_locator = get_open_dialog_locator(page)

    if dialog_locator is not None:

        # INTENTIONAL: popups/modals (Edit Rule, Device Assignment,
        # etc.) are no longer explored at all. Every dropdown,
        # checkbox, or button inside one is a live edit surface tied
        # to real data (an Edit Rule dialog's own "Save" IS blocked
        # by word, but selecting things inside it still changes the
        # dialog's internal state, and some dialogs auto-apply on
        # selection change with no separate Save step at all). The
        # only correct behaviour is: notice the popup opened, capture
        # its HTML as-is, and close it - nothing inside it is ever
        # clicked.
        print(f"-> Popup opened for '{label}' - capturing it and closing without touching its contents.")

        html = get_rendered_html(page)

        if on_element_ready:
            on_element_ready(page, section_name, section_url, label, selector, state_id, html)

        close_dialog_if_present(page)

    html = get_rendered_html(page)

    if on_element_ready:
        on_element_ready(page, section_name, section_url, label, selector, state_id, html)

    # If the click navigated us away (a routerLink card, a tab that
    # changes the URL, ...), get back to the section's base screen
    # before the caller moves on to the next top-level element.
    return_to_previous_state(page, previous_url)

    return clicked_count


def click_popup_element(
        page,
        section_name,
        section_url,
        element_info,
        on_element_ready=None
):
    """
    NOTE: no longer called. Popups are now closed immediately on
    open without clicking anything inside them (see the dialog
    handling in explore_section_elements()/the click loop above).
    Left in place only in case a future, deliberately-scoped
    exception is ever needed for one specific, known-safe popup.
    """

    selector = element_info["selector"]
    text = element_info["text"]
    index = element_info["index"]
    element_root = element_info.get("root") or page

    label = text if text else selector
    state_id = f"{section_name}|popup|{selector}|{text}|{index}"

    try:

        locator = element_root.locator(selector).nth(index)

        if not locator.is_visible():
            return 0

        try:
            locator.click(timeout=2000)
        except Exception:
            locator.click(timeout=2000, force=True)

    except Exception as e:

        print(f"Could not click popup control '{label}': {e}")

        return 0

    print(f"  Clicked (popup): {label}")

    wait_for_dashboard(page, quick=True)

    html = get_rendered_html(page)

    if on_element_ready:
        on_element_ready(page, section_name, section_url, label, selector, state_id, html)

    return 1


# ----------------------------------------------------------
# Explore Every Safe Clickable Element On The Current Section
# ----------------------------------------------------------
def explore_section_elements(
        page,
        section_name,
        section_url,
        visited_ui_states,
        on_element_ready=None
):
    """
    Single-pass exploration of one sidebar section (requirement #1):

        - discover every safe, top-level clickable element ONCE
        - click each one exactly once
        - if a popup opens, explore only the popup, then close it
        - move on to the next element

    No recursion, and the page is never re-discovered mid-section -
    both of those were the source of the old runaway runtime. If a
    click leaves the section's elements in a slightly different DOM
    order (rare - live dashboards mostly mutate content in place),
    a handful of elements might be skipped rather than mis-clicked;
    that trade-off is what keeps this fast and predictable.

    `visited_ui_states` is a plain Python set shared across the
    whole crawl, kept only as a cheap defensive guard against
    clicking the exact same (section, selector, text, index)
    combination twice - it is no longer used to drive any
    recursion.

    Returns the total number of elements actually clicked.
    """

    print(f"Exploring clickable elements inside {section_name}...")

    elements = discover_clickable_elements(page)

    explored_count = 0

    for element_info in elements:

        state_id = (
            f"{section_name}|{element_info['selector']}|"
            f"{element_info['text']}|{element_info['index']}"
        )

        if state_id in visited_ui_states:
            continue

        visited_ui_states.add(state_id)

        explored_count += click_and_capture_element(
            page,
            section_name,
            section_url,
            element_info,
            on_element_ready=on_element_ready
        )

        # Make sure nothing is left open from this element before
        # the next one gets a turn - an unclosed overlay would
        # otherwise block visibility checks for whatever comes next.
        close_dialog_if_present(page)

    print(f"Completed {section_name} ({explored_count} elements clicked).")

    return explored_count




# ----------------------------------------------------------
# Ensure Authenticated (NEW - single-command workflow)
# ----------------------------------------------------------
#
# Fixes the "LOGIN" requirement: running `python scraper.py` should
# check auth.json, use it if valid, and log in automatically (then
# regenerate auth.json) if it's missing or expired - with no manual
# login and no manual deletion of auth.json.
#
# Previously, crawl_dashboard_sections() only checked whether
# auth.json EXISTED. It never checked whether the session inside it
# still WORKED, and if it was missing it just shrugged and continued
# with an anonymous (logged-out) context, silently crawling the
# login page instead of the app.
#
# This function replaces that check. It always hands back a context
# that is actually logged in to `dashboard_url`, one of two ways:
#
#   1. auth.json exists and still works -> reuse it as-is (fast
#      path, no login performed).
#   2. auth.json is missing, or the app still shows a login form
#      after navigating (an expired/invalid session) -> call
#      perform_login() on the SAME browser (no second Chromium
#      instance), which overwrites auth.json with a fresh session,
#      then open a new context from that fresh file.
#
# Either way the caller gets back (context, page, response) already
# sitting on `dashboard_url`, so crawl_dashboard_sections() doesn't
# need its own separate "first goto" step anymore.
# ----------------------------------------------------------
def _looks_logged_in(page):
    """
    True if `page` is showing the Angular dashboard shell rather
    than the Keycloak login form. Used to tell "auth.json is still
    valid" apart from "auth.json is missing/expired and we landed
    back on the login page".
    """

    try:
        page.wait_for_selector("ib-iot-root", timeout=8000)
        return True
    except TimeoutError:
        return False


def ensure_authenticated(browser, dashboard_url):
    """
    Guarantees a logged-in context/page for `dashboard_url`,
    logging in automatically (and regenerating auth.json) whenever
    the existing one is missing or has expired.

    Returns (context, page, response) where `page` has already
    finished navigating to `dashboard_url`.
    """

    def _open_with_current_auth():
        if os.path.exists(AUTH_STATE):
            ctx = browser.new_context(storage_state=str(AUTH_STATE))
        else:
            print("[INFO] auth.json not found.")
            ctx = browser.new_context()

        pg = ctx.new_page()

        resp = pg.goto(
            dashboard_url,
            wait_until="domcontentloaded",
            timeout=30000
        )

        return ctx, pg, resp

    print("=" * 50)
    print("AUTH FILE:", AUTH_STATE)
    print("Exists:", os.path.exists(AUTH_STATE))
    print("=" * 50)

    context, page, response = _open_with_current_auth()

    if _looks_logged_in(page):
        print("[INFO] Existing auth.json session is valid - reusing it.")
        return context, page, response

    # ------------------------------------------------------
    # auth.json was either missing or no longer valid (expired
    # session, logged out, etc). Close this dead-end context, log
    # in fresh on the SAME browser, then re-open with the new
    # auth.json. No manual steps required from here.
    # ------------------------------------------------------
    print("[INFO] Session missing or expired - logging in automatically...")

    context.close()

    perform_login(browser)

    context, page, response = _open_with_current_auth()

    if not _looks_logged_in(page):
        # Login ran without raising, but the dashboard still isn't
        # showing up (e.g. wrong credentials, site down). Don't loop
        # forever retrying logins - surface it and let the crawl's
        # existing error handling deal with it, same as any other
        # section-open failure.
        print("[WARNING] Automatic login did not reach the dashboard.")

    return context, page, response


# ----------------------------------------------------------
# Single-Browser Crawl Across Dashboard Sections
# ----------------------------------------------------------
#
# This is the ONE place a browser gets created for the whole
# crawl. Every existing helper above (wait_for_dashboard,
# find_widget, click_with_retry, get_rendered_html) is reused
# here unchanged - nothing about them had to be rewritten.
#
# `sections` is a plain list of dicts like:
#   [
#     {"name": "Dashboard", "url": "https://.../dashboard/status-list"},
#     {"name": "Reports",   "url": "https://.../reports/scheduled"},
#     ...
#   ]
#
# The FIRST item in the list is treated as the dashboard itself
# and is the only one loaded with page.goto(). Every item after
# that is reached by clicking the sidebar using
# SECTION_NAV_SELECTORS, never by page.goto().
#
# `on_section_ready(page, name, url, html)` is a callback
# supplied by scraper.py. This file (playwright_check.py) has
# no idea what BeautifulSoup or website_elements.json are -
# it just hands the rendered HTML back to whoever asked for it,
# once per section, so scraper.py can keep doing its own
# parsing exactly like before.
# ----------------------------------------------------------
def crawl_dashboard_sections(
        sections,
        on_section_ready=None,
        on_element_ready=None,
        headless=False,
        read_only_sections=None
):
    """
    Opens ONE browser, ONE context, ONE page.
    Ensures a valid, logged-in session ONCE via ensure_authenticated()
    - reusing auth.json if it still works, logging in automatically
    and regenerating auth.json if it's missing or expired.
    Opens the dashboard ONCE (as part of that same authentication
    check).
    Clicks through every other section using the sidebar.

    After opening each sidebar section, it also explores every
    safe clickable element found INSIDE that section (cards,
    tabs, filters, dashboard widgets, etc) using
    explore_section_elements(): one discovery pass per section,
    each element clicked exactly once, popups explored in place
    and closed - no recursive re-discovery. A single
    `visited_ui_states` set is shared across the whole crawl as a
    cheap guard against double-clicking the same element. The
    crawler never leaves the application's own hostname (see
    APP_CONTEXT).

    Closes the browser ONCE, after every section is scraped.

    Returns a tuple:
        (
            results,                # list of per-section dicts
            sections_visited_count, # sidebar pages successfully opened
            total_elements_explored,# clickable elements clicked
            total_ui_states         # len(visited_ui_states)
        )
    """

    results = []

    # Cheap defensive guard against clicking the exact same
    # (section, selector, text, index) combination twice - shared
    # across the whole crawl. See explore_section_elements().
    visited_ui_states = set()

    # Sections matched (case-insensitively) against this set are
    # opened and their HTML captured, but never clicked inside.
    # Falls back to the module-level READ_ONLY_SECTIONS constant
    # if the caller doesn't override it.
    read_only_lookup = {
        s.strip().lower()
        for s in (read_only_sections if read_only_sections is not None else READ_ONLY_SECTIONS)
    }

    def _is_read_only_section(section_name: str) -> bool:
        # SUBSTRING match, not equality - "Automation Rules",
        # "Rule Engine", "Alarm History" etc. all still match "rule"
        # / "alarm" even though they aren't an exact string match.
        lowered = section_name.strip().lower()
        return any(keyword in lowered for keyword in read_only_lookup)

    sections_visited_count = 0

    total_elements_explored = 0

    with sync_playwright() as p:

        # ---------------------------------------
        # Open the browser ONCE for the whole crawl.
        # ---------------------------------------
        print("Opening Browser (single session for the entire crawl)...")

        browser = p.chromium.launch(headless=headless)

        # ---------------------------------------
        # CHANGED (LOGIN requirement - single-command workflow):
        # this used to just check whether auth.json EXISTED and,
        # if not, fall back to an anonymous (logged-out) session
        # with a warning - silently crawling the login page instead
        # of the app. ensure_authenticated() now checks whether the
        # session actually WORKS, and logs in automatically (via
        # perform_login(), reusing this SAME browser) whenever it's
        # missing or expired, regenerating auth.json with no manual
        # steps needed.
        #
        # This also does what the old index == 0 branch below used
        # to do (the dashboard's page.goto()) as part of validating
        # the session, so `page`/`response` already reflect the
        # dashboard by the time the loop below starts.
        # ---------------------------------------
        dashboard_url = sections[0].get("url", "") if sections else ""

        context, page, response = ensure_authenticated(browser, dashboard_url)

        for index, section in enumerate(sections):

            name = section.get("name", f"section_{index}")
            label_url = section.get("url", "")

            print("\n" + "=" * 60)
            print(f"Section {index + 1}/{len(sections)}: {name}")
            print("=" * 60)

            print(f"Opening {name}...")

            if index == 0:

                # -----------------------------------------
                # CHANGED: the FIRST section is the dashboard
                # itself, and used to be opened here with its own
                # page.goto(). That navigation now happens inside
                # ensure_authenticated() above (it has to get there
                # anyway, to check whether the session is valid),
                # so `page`/`response` are already sitting on the
                # dashboard - this just reports the same
                # PASS/FAIL status as before instead of navigating
                # again.
                # -----------------------------------------
                print(f"Opening dashboard: {label_url}")

                if response and response.status < 400:
                    print(f"Playwright Check: PASS (status {response.status})")
                else:
                    status = response.status if response else "No Response"
                    print(f"Playwright Check: FAIL (status {status})")

                # -----------------------------------------
                # NEW (requirement #4): record the application's
                # own hostname, right after the dashboard has
                # loaded for the first time. Every later domain
                # check (is_internal_and_safe_href, the post-click
                # guard in explore_ui_element) compares against
                # this value.
                # -----------------------------------------
                APP_CONTEXT["hostname"] = urlparse(page.url).hostname

                print(f"Application hostname locked to: {APP_CONTEXT['hostname']}")

            else:

                # -----------------------------------------
                # Every other section: click the sidebar/menu
                # instead of page.goto(). This is what makes
                # the crawl behave like a real user navigating
                # the Angular SPA.
                # -----------------------------------------
                selectors = SECTION_NAV_SELECTORS.get(
                    name,
                    [f"text={name}"]
                )

                clicked = click_with_retry(page, selectors)

                if not clicked:

                    print(f"[WARNING] Could not click '{name}'. Skipping this section.")

                    results.append({
                        "name": name,
                        "url": label_url,
                        "html": None,
                        "error": "nav click failed"
                    })

                    continue

            # This section was opened successfully (either the
            # initial goto, or a successful sidebar click).
            sections_visited_count += 1

            # ---------------------------------------
            # Wait for Angular + the dashboard shell after EVERY
            # navigation, whether it was a goto or a click.
            #
            # PERF: this used to be followed by a separate
            # find_widget(page, DASHBOARD_WIDGET_SELECTORS) call.
            # DASHBOARD_WIDGET_SELECTORS is just ["ib-iot-root"],
            # which wait_for_dashboard() (above) already waits for
            # - so that second call was a guaranteed-redundant
            # round trip on every section, doing nothing that
            # hadn't already been done.
            # ---------------------------------------
            wait_for_dashboard(page)

            # ---------------------------------------
            # Grab the rendered HTML for this section using
            # the SAME shared page (no new browser, no goto).
            # ---------------------------------------
            html = get_rendered_html(page)

            # ---------------------------------------
            # Hand the HTML back to scraper.py so IT can run
            # BeautifulSoup + extract_elements + build the
            # website_elements.json entry, exactly like before.
            # ---------------------------------------
            if on_section_ready:
                on_section_ready(page, name, label_url, html)

            results.append({
                "name": name,
                "url": label_url,
                "html": html
            })

            # ---------------------------------------
            # Instead of stopping at the sidebar page, explore
            # every safe clickable element found INSIDE this
            # section too (cards, tabs, filters, widgets,
            # pagination, etc) - one discovery pass, each element
            # clicked once, popups explored and closed in place.
            # See explore_section_elements() for why this is no
            # longer a recursive walk.
            #
            # EXCEPT for sections in read_only_lookup (e.g.
            # "Rules") - those are visited and their HTML is
            # captured above, but nothing inside them is ever
            # clicked. See READ_ONLY_SECTIONS for why.
            #
            # `visited_ui_states` is shared across every section
            # in this loop as a cheap defensive guard against
            # double-clicking the exact same element.
            # ---------------------------------------
            if SAFE_MODE:

                print(
                    f"SAFE_MODE is on - '{name}' visited and captured "
                    "only, nothing inside it will be clicked."
                )

                explored_here = 0

            elif _is_read_only_section(name):

                print(f"'{name}' is read-only - visiting only, not clicking inside it.")

                explored_here = 0

            else:

                explored_here = explore_section_elements(
                    page,
                    name,
                    label_url,
                    visited_ui_states,
                    on_element_ready=on_element_ready
                )

            total_elements_explored += explored_here

            # PERF: quick=True - just enough of a settle wait so
            # the NEXT sidebar click starts from a clean screen,
            # without paying for a full dashboard-load wait again.
            wait_for_dashboard(page, quick=True)

        # ---------------------------------------
        # Close the browser ONCE, after every section
        # has been visited and scraped.
        # ---------------------------------------
        print("\nClosing browser (crawl finished)...")

        browser.close()

    return (
        results,
        sections_visited_count,
        total_elements_explored,
        len(visited_ui_states)
    )