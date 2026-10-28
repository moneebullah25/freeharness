#!/usr/bin/env python3
"""
chatgpt-bridge — drives chatgpt.com in a real browser and streams the answer
back over SSE in the exact wire format InterviewCopilot's `answerViaRelay`
already parses.

    ./bridge.py login          # one-time: log in by hand, session is saved
    ./bridge.py ask "..."      # one-shot, prints the streamed answer
    ./bridge.py serve          # SSE server on 127.0.0.1:8765

Point the app's relayURL at http://127.0.0.1:8765/answer. The wire format
matches `answerViaRelay` -- `data: {"t": "..."}` / `data: [DONE]` / `{"error"}` --
plus one new event, `{"reset": "<full text>"}`, emitted when ChatGPT re-renders
and rewrites text already sent. See README for the 3-line Swift change.

Caveats you already know about, restated once so they're in the repo:
OpenAI's consumer terms prohibit programmatic extraction of Output, so this
risks the account it runs as. A headed browser window IS captured by
screen share. Selectors break whenever the ChatGPT UI ships.
"""
from __future__ import annotations

import argparse
import collections
import math
import re
import sys
import time
from urllib.parse import urlparse
from pathlib import Path

try:
    from playwright.sync_api import Error as PWError, sync_playwright
except ImportError:
    sys.exit("playwright not installed. Run:  uv run --with playwright ./bridge.py ...\n"
             "then once:  uv run --with playwright playwright install chromium")

from .. import paths
from .providers import CHATGPT, PROVIDERS, Provider

# The default provider's values stay importable under their old names:
# probe.py, the phase0 scripts and the tests read them.
PROFILE = CHATGPT.profile          # persistent login lives here
CHAT_URL = CHATGPT.url
PROVIDER = CHATGPT.label           # shown as the backend label in InterviewCopilot
# Every selector the UI can rename lives in providers.py. When something breaks
# it is almost always one of these — each is tried in order, first hit wins.
SEL = CHATGPT.sel

FENCE = re.compile(r"```[^\n]*\n(.*?)\n?```", re.S)   # a fenced block in markdown
IDLE_SETTLE = 0.6      # seconds of unchanged text that counts as finished
POLL = 0.12            # how often to scrape the growing answer
NAV_TIMEOUT = 45_000
# Navigation is intermittently flaky: `domcontentloaded` has loaded in 2.6s and
# also timed out twice at 30s minutes apart. So use a SHORT per-attempt timeout
# with more attempts, and judge success by the composer being present rather
# than by a load event — the page is often usable before the event fires, and
# sometimes the event never fires at all.
NAV_ATTEMPTS = 4
NAV_ATTEMPT_MS = 20_000
COMPOSER_MS = 15_000
# 30s was too short: a 12KB prompt has taken ~10s just to first token, and a
# busy server takes longer. The old message also asserted "(rate limited?)",
# a cause it never checked — which sent us chasing a quota problem that was
# not there. Wait longer, and report what the page ACTUALLY shows.
REPLY_TIMEOUT = 90     # seconds to wait for ChatGPT to start replying
SPA_PAINT = 3_000      # ms to let the single-page app render after navigation
# Stay under InterviewCopilot's own 120s request timeout so a stuck answer
# surfaces as a bridge error the app can show, not as a client-side timeout.
ANSWER_TIMEOUT = 100
# MEASURED: InterviewCopilot's answerViaRelay inlines the whole context folder,
# which is ~131KB / 27K tokens. ChatGPT rejects that outright ("Something went
# wrong") after ~43s. Anything past this budget gets retrieved down instead.
# 12,000 was a guess: 131KB was measured to fail, 12K measured to work, and
# nothing between was ever tested. Throttling is per message, so a bigger prompt
# that does more work is strictly better than two small ones. Raised to a value
# we will now actually test — if it is too high ChatGPT says so, the reactive
# compactor handles it, and record_limit() writes down the real ceiling.
# The one measured datapoint: ~131KB (130,786 chars) was rejected. Sit just
# under it rather than inventing a "safe" fraction of it. If this is still too
# high, ChatGPT says so, the reactive compactor retries in a fresh thread, and
# record_limit() writes down the real number so the next value is evidence.
MAX_PROMPT = 100_000
# ChatGPT's own failure text, which must not be passed off as an answer.
SITE_ERRORS = ("something went wrong", "you've reached our limit",
               "too many requests", "please try again later",
               "unusual activity", "conversation is too long")
# Regions the app's prompts are built from, with how each may be shrunk:
#   retrieve = BM25 the sections against the question (notes are sectioned)
#   recent   = keep the tail (the latest speech is what the question is about)
#   keep     = never shrink; this is where the question lives
NOTES_HEAD = "My background notes (use them):"
# Reference sections (textbook knowledge) are prefixed so they can be told apart
# from the candidate's own experience. Without this split a RAG textbook that
# says "Qdrant" fifty times outranks the one project section that actually
# tells the interviewer what THIS person built.
REF_PREFIX = "### REF:"
# Measured against 51 personal + 190 reference sections: retrieval accuracy is
# identical from 0.0 to 0.6, so this is a balance knob, not an accuracy fix.
# It earns its place by putting a FLOOR under own-material — at 0.0, "what is
# prompt injection?" retrieved zero personal sections. 0.4 keeps roughly a
# 3-own / 4-ref mix.
PERSONAL_SHARE = 0.4      # of the notes budget reserved for non-REF sections
RELEVANCE_FLOOR = 0.45    # a pool scoring below this fraction of the other is noise
QUESTION_HEAD = "Question:"
REGIONS = [
    (NOTES_HEAD,            "retrieve"),
    ("Conversation so far:", "recent"),
    ("Transcript:",          "recent"),
    ("Question to answer:",  "keep"),
    (QUESTION_HEAD,          "keep"),
    # chatgpt-agent's opening context
    ("Project notes (from the vault):", "retrieve"),
    ("SKILLS available", "retrieve"),      # the index + any preloaded body
    ("Layout:",              "recent"),
    ("TASK:",                "keep"),
]


def delta_event(sent: str, text: str):
    """Turn (already-sent, current-DOM-text) into one SSE event, or None.

    ChatGPT's answer node is *mostly* append-only, but a markdown re-render can
    rewrite text that was already streamed. Emitting a naive suffix there would
    corrupt the answer, so divergence is reported as a full reset instead.
    """
    if text == sent:
        return None
    if text.startswith(sent):
        return {"t": text[len(sent):]}          # clean append
    if sent.startswith(text):
        return None                             # transient truncation mid-render
    return {"reset": text}                      # re-render rewrote earlier text


def render_event(event: dict, shown: str) -> tuple[str, str]:
    """(text to write to a terminal, new shown-so-far) for one SSE event."""
    if "t" in event:
        return event["t"], shown + event["t"]
    text = event["reset"]
    if text.startswith(shown):                       # reset that only extends
        return text[len(shown):], text
    return "\n--- re-rendered ---\n" + text, text   # genuinely rewritten


def _sections(notes: str) -> list[str]:
    """Split notes on markdown headings; one blob if it has none (a transcript)."""
    parts = [p for p in re.split(r"\n(?=#{2,4}\s)", notes) if p.strip()]
    return parts or [notes]


def _bm25(sections: list[str], question: str) -> list[tuple[int, float]]:
    """(index, score) per section, best match first. No deps."""
    def terms(t):
        return collections.Counter(re.findall(r"[a-z0-9]+", t.lower()))

    qt = terms(question)
    n = len(sections)
    tf = [terms(s) for s in sections]
    df = collections.Counter(w for t in tf for w in t)
    avg = sum(sum(t.values()) for t in tf) / max(n, 1)

    def score(i):
        t, length, total = tf[i], sum(tf[i].values()), 0.0
        for w in qt:
            f = t.get(w, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[w] + .5) / (df[w] + .5))
            total += idf * f / (f + 1.5 * (.25 + .75 * length / max(avg, 1)))
        return total

    return sorted(((i, score(i)) for i in range(n)), key=lambda t: -t[1])


def _pick_sections(secs: list[str], question: str, cap: int) -> list[int]:
    """Section indices to keep, under `cap` chars, in document order.

    Ranks globally (so IDF is computed over everything) but *selects* from two
    pools, so the candidate's own material always gets a reserved share.
    """
    scored = _bm25(secs, question)
    is_ref = [s.lstrip().startswith(REF_PREFIX) for s in secs]
    personal = [(i, sc) for i, sc in scored if not is_ref[i]]
    reference = [(i, sc) for i, sc in scored if is_ref[i]]

    if not personal or not reference:          # only one kind present: no split
        pools, caps = [[i for i, _ in scored]], [cap]
    else:
        best_own = personal[0][1]
        best_ref = reference[0][1]
        own = int(cap * PERSONAL_SHARE)
        # A question with no discriminating terms ("tell me about a project")
        # scores near-noise against the reference pool, which then fills 60% of
        # the budget with arbitrary textbook sections. Only spend on reference
        # when it actually matches something.
        if best_ref < RELEVANCE_FLOOR * best_own:
            own = cap
        elif best_own < RELEVANCE_FLOOR * best_ref:
            own = int(cap * PERSONAL_SHARE)    # keep the floor; rest to reference
        pools = [[i for i, _ in personal], [i for i, _ in reference]]
        caps = [own, cap - own]

    kept, spare = [], 0
    for pool, budget in zip(pools, caps, strict=False):
        if budget <= 0:
            continue          # deliberately dropped pool — spare must not revive it
        used = 0
        for i in pool:
            if used + len(secs[i]) > budget + spare:
                continue
            kept.append(i)
            used += len(secs[i])
        spare = max(0, budget + spare - used)   # unspent budget rolls forward
    return sorted(kept)


def _regions(prompt: str):
    """[(pos, head, strategy)] for the heads present, in document order."""
    found = []
    for head, how in REGIONS:
        # question heads sit at the end; notes/transcript heads at their first use
        pos = prompt.rfind(head) if how == "keep" else prompt.find(head)
        if pos >= 0:
            found.append((pos, head, how))
    found.sort()
    # "Question:" is a substring of nothing here, but a head found INSIDE an
    # earlier region's body would corrupt the split -- drop overlaps.
    out = []
    for r in found:
        if not out or r[0] > out[-1][0] + len(out[-1][1]):
            out.append(r)
    return out


def shrink_prompt(prompt: str, budget: int = MAX_PROMPT) -> tuple[str, str]:
    """Fit an oversized prompt without ever dropping the question.

    ChatGPT rejects a 131KB paste outright, so the notes get retrieved down and
    a long transcript gets trimmed to its most recent part. Anything marked
    `keep` is preserved whole.
    """
    if len(prompt) <= budget:
        return prompt, ""

    regs = _regions(prompt)
    if not regs:
        # Unknown shape: keep both ends. Instructions live at the top and the
        # question at the bottom, so cutting the middle loses the least.
        half = budget // 2
        return (prompt[:half] + "\n\n[... trimmed ...]\n\n" + prompt[-half:],
                f"unknown prompt shape, kept first+last {half:,} chars")

    # carve the prompt into preamble + one block per region
    blocks, bounds = [], [r[0] for r in regs] + [len(prompt)]
    pre = prompt[:bounds[0]]
    for i, (pos, head, how) in enumerate(regs):
        body = prompt[pos + len(head):bounds[i + 1]]
        blocks.append([head, body, how])

    question = " ".join(b for _, b, how in blocks if how == "keep") or prompt[-1500:]
    fixed = len(pre) + sum(len(h) + (len(b) if how == "keep" else 0)
                           for h, b, how in blocks) + 64
    shrinkable = [b for b in blocks if b[2] != "keep"]
    room = budget - fixed
    if not shrinkable or room < 200:
        half = budget // 2
        return (prompt[:half] + "\n\n[... trimmed ...]\n\n" + prompt[-half:],
                "fixed parts alone exceeded the budget")

    # even split, then hand back whatever a small region does not need
    share = room // len(shrinkable)
    spare = sum(share - len(b[1]) for b in shrinkable if len(b[1]) <= share)
    notes_took = []
    for blk in shrinkable:
        head, body, how = blk
        cap = share if len(body) > share else len(body)
        if len(body) > share and spare > 0:      # a big region can use the slack
            cap += spare
            spare = 0
        if len(body) <= cap:
            continue                              # fits as-is
        if how == "retrieve":
            secs = _sections(body)
            kept = _pick_sections(secs, question, cap)
            blk[1] = "\n\n".join(secs[i] for i in kept) if kept else body[-cap:]
            own = sum(1 for i in kept if not secs[i].lstrip().startswith(REF_PREFIX))
            notes_took.append(f"{own} own + {len(kept) - own} ref of {len(secs)}")
        else:
            blk[1] = body[-cap:]
            notes_took.append("recent transcript")

    out = pre + "".join(f"{h}\n{b.strip()}\n\n" for h, b, _ in blocks)
    return out, f"{', '.join(notes_took)} ({len(prompt):,} -> {len(out):,} chars)"


def looks_like_site_error(text: str, errors: tuple = SITE_ERRORS) -> bool:
    """The site rendered its own failure; that is not an answer."""
    head = text.strip()[:200].lower()
    return any(m in head for m in errors)


def _first(page, kind, timeout=8_000, sel=None):
    """Return a locator for the first selector in sel[kind] that exists."""
    sel = sel or SEL
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        for css in sel[kind]:
            loc = page.locator(css).last
            try:
                if loc.count() and loc.is_visible():
                    return loc
            except PWError:
                continue
        time.sleep(0.1)
    raise LookupError(f"no selector matched for {kind!r}; tried {sel[kind]}. "
                      f"The site's UI likely changed — update its selectors in providers.py")


class Session:
    """One warm browser holding a tab on one provider's chat site."""

    # Class defaults, because close() reads all four and __init__ can raise
    # before any of them exists. A failed launch is the COMMON case — Chromium
    # holds an exclusive lock on user_data_dir, so a second agent or bridge on
    # the same .profile dies here — and an AttributeError in close() would then
    # mask the one error the human actually needs to read.
    keep_generating = False         # True: a request that is dropped does NOT stop the site's answer (the shim adopts it)
    last_before = 0                 # assistant turns on the page when the last message was sent
    last_before_text = None         # ... and the text of the last one (a long chat keeps few turns in the page: the COUNT may not grow)
    owns_browser = True
    owns_driver = True              # False when a shared Playwright driver was passed in
    _pw = ctx = page = None
    p = CHATGPT                     # provider; class default for the same reason
    # The InterviewCopilot relay cuts a prompt over MAX_PROMPT (an assumed ceiling). The shim sends its own
    # prompts and learns real limits from the site (limits.py), so it sets this False: never cut silently.
    shrink_prompts = True

    def __init__(self, headless: bool, provider: Provider = CHATGPT,
                 user_data_dir: Path | str | None = None,
                 channel: str | None = None, pw=None):
        """`user_data_dir` / `channel="chrome"` point at an existing, logged-in
        Chrome profile instead of this provider's own fresh one. Chrome locks
        its profile directory, so use a dedicated copy, not the daily profile.

        `pw` is an already-started sync_playwright(): one thread can run only one
        driver, so several providers in one process (shim.py) must share it."""
        self.p = provider
        self.owns_driver = pw is None
        self._pw = pw or sync_playwright().start()
        profile = Path(user_data_dir) if user_data_dir else provider.profile
        paths.private_dir(profile)       # parents too: profiles/ does not exist on a fresh install
        opts = {"channel": channel} if channel else {}
        args = ["--disable-blink-features=AutomationControlled"]
        if channel:
            args.append("--profile-directory=Default")   # chrome_login.py's layout
        self.ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=headless,
            args=args,
            viewport={"width": 1280, "height": 900},
            **opts,
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(NAV_TIMEOUT)

    @classmethod
    def _on_page(cls, parent: "Session", page) -> "Session":
        """A Session bound to an existing page. Lives next to __init__ so the
        two cannot drift: everything below is state __init__ also sets."""
        s = object.__new__(cls)
        s._pw, s.ctx, s.page = parent._pw, parent.ctx, page
        s.p = parent.p
        s.owns_browser = False          # shadows the class default
        s.page.set_default_timeout(NAV_TIMEOUT)
        return s

    def tab(self, provider: Provider | None = None) -> "Session":
        """A second Session on its own tab of the SAME browser.

        Every method here touches only `self.page`, so a Session is really just
        a tab.

        WHY a worker needs one at all, since this is the thing a future reader
        would undo: a worker has to call `ask(fresh=True)`, and `new_thread()`
        navigates the page it is called on. On a shared page that would throw
        away the orchestrator's own conversation — which holds the protocol,
        the repo context and the decomposition that `Agent.run()` later
        continues. A tab is what keeps the two threads from being one page.

        A second BROWSER is not the alternative, measured rather than assumed:
        Chromium holds an exclusive lock on `user_data_dir`, so a concurrent
        `launch_persistent_context(PROFILE)` dies with "Opening in existing
        browser session. This usually means that the profile is already in use
        by another instance of Chromium." Copying the profile per worker would
        mean a second login and a second Cloudflare reputation to warm up.

        UNVERIFIED, and the design rests on it: that a second tab on
        chatgpt.com is an independent conversation, and that N tabs streaming
        at once do not bleed into each other's DOM scrape. `phase0c_tabs.py`
        measures both; see MULTI-AGENT.md §2.
        """
        s = self._on_page(self, self.ctx.new_page())
        if provider:                    # another site in the same logged-in browser
            s.p = provider
        return s

    def goto(self, url: str) -> None:
        """Navigate with retries.

        Navigation fails transiently — a slow Cloudflare check can leave the tab
        on about:blank past the timeout. One bad goto should not end a task that
        has real work committed. Every navigation goes through here; fixing only
        `open_chat` left `new_thread` to die the identical death.
        """
        last = ""
        for attempt in range(1, NAV_ATTEMPTS + 1):
            try:
                self.page.goto(url, wait_until="domcontentloaded",
                               timeout=NAV_ATTEMPT_MS)
                return
            except PWError as e:
                last = str(e).splitlines()[0][:120]
                # The load event may never fire while the challenge JS runs, yet
                # the composer can already be there. That is good enough.
                for css in self.p.sel["input"]:
                    try:
                        self.page.wait_for_selector(css, timeout=COMPOSER_MS)
                        return
                    except PWError:
                        continue
                if attempt == NAV_ATTEMPTS:
                    raise TimeoutError(
                        f"could not load {url} after {NAV_ATTEMPTS} attempts "
                        f"({last}). {self.page_state()}") from None
                print(f"  \033[33m⟳ navigation attempt {attempt} failed, "
                      f"retrying\033[0m")
                self.page.wait_for_timeout(1_500 * attempt)

    def current_url(self) -> str | None:
        """The page's address, for callers that must know whether it is still the same chat."""
        try:
            return self.page.url
        except PWError:
            return None

    def open_chat(self):
        # Several sites are tabs of one browser here; a background tab is "hidden"
        # and sites such as Grok then never mount their composer.
        try:
            self.page.bring_to_front()
        except PWError:
            pass
        if not self.page.url.startswith(self.p.url):
            self.goto(self.p.url)
        self.page.wait_for_timeout(SPA_PAINT)
        self._dismiss_consent()
        if self._challenged():
            raise RuntimeError(
                f"{self.p.label} is challenging this browser with a human-verification "
                f"page (title {self.page.title()!r}). Complete it yourself once with "
                f"`freeharness login {self.p.key}`; it is never bypassed automatically.")
        self._composer()

    def _composer(self):
        """The prompt box. A page that has not hydrated (seen intermittently on ChatGPT's visible
        window, which then showed the box in a screenshot taken moments later) gets ONE reload
        before the lookup is reported as a changed UI."""
        try:
            return _first(self.page, "input", timeout=COMPOSER_MS, sel=self.p.sel)
        except LookupError:
            self._raise_if_logged_out()
            self.page.reload()
            self.page.wait_for_timeout(SPA_PAINT)
            self._dismiss_consent()
            try:
                return _first(self.page, "input", timeout=COMPOSER_MS, sel=self.p.sel)
            except LookupError:
                self._raise_if_logged_out()
                raise

    def _raise_if_logged_out(self):
        """Landing on the site's sign-in page is a LOGIN problem (re-run `freeharness login <site>`), not a changed UI."""
        if any(m in self.page.url for m in self.p.login_url):
            raise RuntimeError(f"{self.p.label} is signed out (the page is {self.page.url}): please log in "
                               f"again with `freeharness login {self.p.key}`")

    def _dismiss_consent(self):
        """Click away cookie/terms dialogs. Best effort: a miss is not an error,
        the composer lookup that follows is what actually decides."""
        for css in self.p.consent:
            try:
                btn = self.page.locator(css).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=2_000)
            except PWError:
                pass

    def _challenged(self) -> bool:
        if "just a moment" in (self.page.title() or "").lower():
            return True
        if any(m in self.page.url for m in self.p.challenge_url):
            return True
        for css in self.p.sel["blocked"]:
            try:
                if self.page.locator(css).count():
                    return True
            except PWError:
                pass
        return False

    def new_thread(self):
        """Fresh conversation so prior answers can't leak into this one."""
        self.goto(self.p.fresh_url)
        self._dismiss_consent()
        self._composer()

    def _streaming(self) -> bool:
        for css in self.p.sel["stop"]:
            try:
                if self.page.locator(css).count():
                    return True
            except PWError:
                pass
        return False

    def stop_generation(self):
        for css in self.p.sel["stop"]:
            try:
                loc = self.page.locator(css).last
                if loc.count():
                    loc.click(timeout=2_000)
                    return
            except PWError:
                pass

    def ask(self, question: str, fresh: bool = True, should_stop=None):
        """Yield SSE events: {"t": delta} to append, {"reset": full} to replace.
        `should_stop()` is polled while waiting; True abandons the answer and
        stops the site generating (the caller has gone away)."""
        if self.shrink_prompts:
            question, note = shrink_prompt(question)
            if note:
                print(f"  prompt: {note}", file=sys.stderr)
        self.open_chat()
        if fresh:
            self.new_thread()

        before = self.page.locator(self.p.sel["assistant"][0]).count()
        before_text = self._last_text()

        box = self._composer()
        box.click()
        # fill() sets the value directly — typing a multi-line question would
        # submit on the first newline.
        try:
            box.fill(question)
        except PWError:
            self.page.keyboard.insert_text(question)

        # A rate-limited composer leaves the send button visible but
        # aria-disabled, so a plain click waits the full 45s and then throws a
        # raw Playwright traceback. Detect the disabled state and say why.
        try:
            btn = _first(self.page, "send", timeout=4_000, sel=self.p.sel)
            if not btn.is_enabled() or btn.get_attribute("aria-disabled") == "true":
                self.page.wait_for_timeout(3_000)      # it may just be settling
                if not btn.is_enabled() or btn.get_attribute("aria-disabled") == "true":
                    raise TimeoutError(
                        f"{self.p.label}'s send button is disabled — usually the free-tier "
                        "message limit. Wait for the quota to reset, then --resume.")
            btn.click(timeout=15_000)
        except LookupError:
            self.page.keyboard.press("Enter")   # send button hides on some builds
        except PWError as e:
            # The button was found but could not be clicked (covered by a banner, still animating). Enter sends on every
            # site we drive; only if the text is still in the composer afterwards is the prompt really unsent.
            try:
                box.click(timeout=3_000)
                self.page.keyboard.press("Enter")
                self.page.wait_for_timeout(2_000)
            except PWError:
                pass
            if "never sent" in self.page_state():
                raise TimeoutError(f"could not send the prompt: {str(e)[:400]} {self.page_state()}") from None

        self.last_before, self.last_before_text = before, before_text
        yield from self._collect(before, should_stop, before_text)

    def _last_text(self):
        """Text of the newest assistant turn on the page, or None when it cannot be read / there is none."""
        try:
            loc = self.page.locator(self.p.sel["assistant"][0])
            return self.p.clean(loc.last.inner_text()) if loc.count() else None
        except PWError:
            return None

    def _reply_started(self, before: int, before_text=None) -> bool:
        """Has a reply to the message sent when the page held `before` assistant turns (the newest reading `before_text`)
        started? ChatGPT keeps only the latest turns of a long chat in the page, so the COUNT can stay put while the
        newest turn is a new one (MEASURED live 2026-10-28: a complete reply was on the page, the shim waited 600s)."""
        try:
            if self.page.locator(self.p.sel["assistant"][0]).count() > before:
                return True
            if before_text is not None:
                now = self._last_text()
                if now and now != before_text:
                    return True
            return False
        except PWError:
            return False

    def adoptable(self, before: int, before_text=None) -> bool:
        """Did the chat this page is on already start (or finish) the answer to a message sent when it held `before`
        assistant turns? Used to take over an answer whose request was dropped, instead of asking again."""
        return self._reply_started(before, before_text) or self._streaming()

    def adopt(self, before: int, should_stop=None, before_text=None):
        """Same events as ask(), for an answer that is already on (or still arriving on) the page: nothing is typed."""
        try:
            self.page.bring_to_front()
        except PWError:
            pass
        yield from self._collect(before, should_stop, before_text)

    def _collect(self, before: int, should_stop=None, before_text=None):
        # Wait for the new assistant turn to exist.
        answer = self.page.locator(self.p.sel["assistant"][0])
        deadline = time.monotonic() + self.p.reply_timeout
        next_check = time.monotonic() + 2
        while not self._reply_started(before, before_text):
            if should_stop and should_stop():
                if not self.keep_generating:        # the shim keeps a sticky chat's answer so the retry can adopt it
                    self.stop_generation()
                return
            if time.monotonic() > next_check:           # a verification page never produces an answer
                next_check = time.monotonic() + 2
                if self._challenged():
                    raise RuntimeError(
                        f"{self.p.label} showed a human-verification challenge after the prompt "
                        f"was sent. Complete it yourself with `freeharness login {self.p.key}`.")
                if self.chat_full():          # the site refused the message with a notice and will never answer it
                    raise RuntimeError(f"{self.p.label} says the message is too long or the chat is full "
                                       "(a notice on the page, no reply started)")
                shown = self.page_notice(self.p.errors)
                if shown:                     # a limit / "try again later" notice: waiting longer will not produce a reply
                    raise RuntimeError(f"{self.p.label} shows a notice and no reply started: {shown!r}")
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"no reply started within {self.p.reply_timeout}s. "
                    f"{self.page_state()}")
            time.sleep(POLL)

        node = answer.last
        sent = ""
        last_change = time.monotonic()
        hard_stop = time.monotonic() + self.p.answer_timeout
        unreadable = 0

        while True:
            try:
                text = self.p.clean(node.inner_text())
                unreadable = 0
            except PWError:
                # The node can detach when the conversation re-renders. Re-resolve
                # it rather than treating "can't read" as "nothing new".
                unreadable += 1
                if unreadable > 5:
                    node = answer.last
                    unreadable = 0
                text = sent

            event = delta_event(sent, text)
            if event:
                yield event
            if text != sent:
                sent = text
                last_change = time.monotonic()

            if should_stop and should_stop():
                if not self.keep_generating:        # the shim keeps a sticky chat's answer so the retry can adopt it
                    self.stop_generation()
                return
            idle = time.monotonic() - last_change
            if not self._streaming() and idle > IDLE_SETTLE and sent:
                if looks_like_site_error(sent, self.p.errors):
                    raise RuntimeError(f"{self.p.label} returned an error page: "
                                       f"{sent.strip()[:160]}")
                return
            if time.monotonic() > hard_stop:
                self.stop_generation()
                # Partial text has already been streamed; raising makes the app
                # show an error next to it instead of passing a cut-off answer
                # off as complete.
                raise TimeoutError(
                    f"answer did not finish within {self.p.answer_timeout}s "
                    f"({len(sent)} chars streamed)")
            time.sleep(POLL)

    def _notice_text(self) -> str:
        """Lower-cased text of the page's alert-like elements and its last reply (never the whole page: the conversation
        itself, tool output and file contents, may legitimately contain any words)."""
        js = """(css) => {
            const sel = "[role=alert],[role=status],[aria-live=assertive],[class*=toast i],[class*=banner i],"
                      + "[class*=notice i],[class*=warning i],[class*=error i],[class*=limit i],[data-sonner-toast]";
            const out = Array.from(document.querySelectorAll(sel)).slice(-12).map(e => (e.innerText || "").slice(0, 400));
            const turns = document.querySelectorAll(css);
            if (turns.length) out.push((turns[turns.length - 1].innerText || "").slice(0, 400));
            return out.join("\\n");
        }"""
        try:
            return (self.page.evaluate(js, self.p.sel["assistant"][0]) or "").lower()
        except PWError:
            return ""

    def chat_full(self) -> bool:
        """True if the page shows the site's own "this chat is full / too long" notice (the provider's
        `full_markers`)."""
        if not self.p.full_markers:
            return False
        text = self._notice_text()
        return any(m in text for m in self.p.full_markers)

    def page_notice(self, markers) -> str:
        """The first of `markers` (lower-case) that the page's notices show, else ''."""
        text = self._notice_text() if markers else ""
        return next((m for m in markers if m in text), "")

    def page_state(self) -> str:
        """What the page actually shows, for an error that would otherwise guess.

        Every field here is something that distinguishes a real cause: a closed
        page, a navigation, a challenge, a still-full composer (prompt never
        sent), or a stop button (it IS generating, we were just impatient).
        """
        bits = []
        try:
            if self.page.is_closed():
                return "the page was closed (browser crashed or was closed)."
            bits.append(f"url={self.page.url.split('?')[0]}")
            bits.append(f"title={self.page.title()!r}")
            if self._challenged():
                bits.append("a Cloudflare challenge is showing")
            if self._streaming():
                bits.append("a stop button IS present — it was generating, so "
                            "the wait was simply too short")
            for css in self.p.sel["input"]:
                box = self.page.locator(css).last
                if box.count():
                    left = (box.inner_text() or "").strip()
                    bits.append(f"composer holds {len(left)} chars"
                                + (" — the prompt was never sent" if left else ""))
                    break
            turns = self.page.locator(self.p.sel["assistant"][0]).count()
            bits.append(f"{turns} assistant turn(s) on the page")
            notice = " ".join(self._notice_text().split())[:300]
            if notice:
                bits.append(f"notices/last reply: {notice!r}")
        except PWError as e:
            bits.append(f"could not inspect the page: {str(e)[:80]}")
        return "Page state: " + "; ".join(bits) + "."

    def code_blocks(self) -> list[str]:
        """Exact text of every code block in the last answer.

        innerText is useless for code: ChatGPT renders fences as UI chrome, so
        the ``` markers never appear, and it collapses a tab to a single space.
        textContent on the <code> element is the unrendered source. Sites whose
        viewers keep no code in the DOM (Qwen's Monaco) are read from the reply's
        Copy button instead: the clipboard holds the original markdown.
        """
        if self.p.copy_button:
            blocks = self._blocks_from_clipboard()
            if blocks:
                return blocks
        js = """(css) => {
            const turns = document.querySelectorAll(css);
            if (!turns.length) return [];
            const last = turns[turns.length - 1];
            return Array.from(last.querySelectorAll('pre code'))
                        .map(e => e.textContent);
        }"""
        try:
            return self.page.evaluate(js, self.p.sel["assistant"][0]) or []
        except PWError:
            return []

    def _blocks_from_clipboard(self) -> list[str]:
        origin = "{0.scheme}://{0.netloc}".format(urlparse(self.p.url))
        try:
            self.ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
            clicked = self.page.evaluate(
                "(css) => { const b = [...document.querySelectorAll(css)].pop();"
                " if (!b) return false; b.click(); return true; }", self.p.copy_button)
            if not clicked:
                return []
            self.page.wait_for_timeout(600)
            # readText() never settles in a tab that lacks focus, and evaluate() has no timeout.
            md = self.page.evaluate(
                "() => Promise.race([navigator.clipboard.readText(),"
                " new Promise(r => setTimeout(() => r(''), 3000))])") or ""
        except PWError:
            return []
        return [m.group(1).rstrip("\n") for m in FENCE.finditer(md)]

    def close(self):
        # A tab borrowed the browser; closing the context would take the other
        # workers and the orchestrator down with it.
        if not self.owns_browser:
            try:
                if self.page:
                    self.page.close()
            except PWError:
                pass
            return
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw and self.owns_driver:   # the driver still stops if ctx did not
                self._pw.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def provider_arg(sp):
        sp.add_argument("--provider", choices=sorted(PROVIDERS), default=CHATGPT.key,
                        help="which chat site to drive (default: %(default)s)")

    lg = sub.add_parser("login", help="open a browser to sign in once; session persists")
    provider_arg(lg)

    a = sub.add_parser("ask", help="one-shot question, streams to stdout")
    provider_arg(a)
    a.add_argument("question")
    a.add_argument("--headless", action="store_true",
                   help="hide the browser (usually blocked by Cloudflare)")

    args = ap.parse_args()

    if args.cmd == "login":
        prov = PROVIDERS[args.provider]
        sess = Session(headless=False, provider=prov)
        try:
            sess.goto(prov.url)
            print("Sign in in the browser window, open a chat, then press Enter here.")
            input()
            try:
                sess.open_chat()
                print(f"session saved to {prov.profile}")
            except (RuntimeError, LookupError) as e:
                print(f"still not signed in: {e}", file=sys.stderr)
        finally:
            sess.close()

    else:
        sess = Session(headless=args.headless, provider=PROVIDERS[args.provider])
        try:
            shown = ""
            for event in sess.ask(args.question):
                out, shown = render_event(event, shown)
                sys.stdout.write(out)
                sys.stdout.flush()
            print()
        finally:
            sess.close()



if __name__ == "__main__":
    main()
