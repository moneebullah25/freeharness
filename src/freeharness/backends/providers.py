"""
Chat sites the bridge can drive. One `Provider` per site: where it lives, which
selectors find its composer/answer, and how to tell it has failed.

Selectors are the part that rots. Every entry is a list tried in order, first
visible hit wins (see bridge._first). Provenance of each set is in the comment
beside it: MEASURED means read off the live DOM on 2026-10-24, logged out.

SITES TRIED AND NOT REGISTERED (2026-10-24, headless, logged out) -- do not re-probe
without a new idea; each needs a login or a visible browser:
  Grok       first message works, then "Sign up to continue" (cookie banner: "Reject All")
  Qwen       "Welcome to Qwen -- Log in" modal on send
  HuggingChat "Start chatting" redirects to huggingface.co/login
  Duck.ai    terms "Continue" + "Got It!" clear, then "Duck.ai is temporarily unavailable
             (code 84f2)" -- same with a normal user agent; probably needs a headed browser
  Kimi, Z.ai login / slider captcha;  DeepSeek 403 headless;  Claude, Mistral Cloudflare
  LMArena, You.com, DeepAI, Blackbox, Phind, Pi: no usable anonymous chat found
A logged-in profile (`freeharness login`) unlocked Qwen and Grok; see the evals below.

Only sites that answered a real prompt WITHOUT a login are `verified`. The rest
are registered so the work is not lost, but `auto` never rotates onto them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from .limits import NO_LIMIT


@dataclass(frozen=True)
class Provider:
    key: str
    label: str                      # shown as the backend name
    url: str                        # first page to open
    sel: dict                       # input / send / stop / assistant / blocked
    new_url: str = ""               # fresh-conversation URL ("" = same as url)
    headless_ok: bool = True        # False: the site challenges headless Chromium
    shared: bool = False            # lives in the one logged-in Chrome profile (CHROME_PROFILE):
                                    # Chromium locks a profile, so these are tabs of ONE browser
    verified: bool = False          # answered a real prompt, logged out
    reply_timeout: int = 90         # seconds for the answer to START (ChatGPT's measured TTFT: ~8-45s)
    answer_timeout: int = 100       # seconds for it to FINISH once started
    max_prompt: int = NO_LIMIT      # chars the site takes in one message. NO assumed limit by default (a
                                    # safety value): real limits are learned from the site's own
                                    # signals (backends/limits.py), never guessed. Set only from a
                                    # measurement that has been repeated.
    errors: tuple = ()              # extra lowercase markers of a site-rendered failure
    full_markers: tuple = ()        # lowercase substrings of the SITE's own "this chat is full / too long"
                                    # notice (page or reply). Seen => the shim stops continuing that chat
                                    # and starts a new one with a compact history. Wording is from memory
                                    # of each site and UNVERIFIED until seen live (docs/PROVIDERS.md).
    size_errors: tuple = ()         # generic-looking site errors known to mean "prompt too big" for this
                                    # site (a measured one); treated like a length signal
    consent: tuple = ()             # buttons to click away after load (best effort)
    login_url: tuple = ()           # URL fragments of the site's sign-in page: landing there means the session is logged out
    challenge_url: tuple = ()       # URL fragments of the site's human-verification page; seeing one
                                    # means "stop and cool down", never "keep waiting"
    channel: str = ""               # "chrome": drive installed Chrome (needed for a profile that `freeharness
                                    # login` created); "" = Playwright's bundled Chromium
    copy_button: str = ""           # the last reply's "Copy" button: its clipboard text is the exact
                                    # markdown, for sites whose code viewers do not keep code in the DOM
    strip: re.Pattern | None = field(default=None, compare=False)  # UI chrome in the answer text

    @property
    def profile(self) -> Path:
        return paths.provider_profile(self.key)

    @property
    def fresh_url(self) -> str:
        return self.new_url or self.url

    def clean(self, text: str) -> str:
        return self.strip.sub("", text, count=1) if self.strip else text


CHATGPT = Provider(
    key="chatgpt", label="ChatGPT", url="https://chatgpt.com/",
    new_url="https://chatgpt.com/?model=auto",
    headless_ok=False,          # Cloudflare challenges headless even when logged in (2026-10-25): a visible window
    channel="chrome",           # its own profile (profiles/chatgpt), signed in with `freeharness login chatgpt`
    verified=True,
    errors=("something went wrong", "you've reached our limit", "too many requests",
            "please try again later", "unusual activity", "conversation is too long"),
    full_markers=("maximum length for this conversation", "conversation is too long",
                  "start a new chat", "message you submitted was too long", "message is too long"),
    # RE-MEASURED 2026-10-25 (the page was rebuilt; the old ids/testids are gone, kept as fallbacks):
    # the composer is a ProseMirror editor, Send/Stop are labeled buttons, and a reply is
    # `[data-markdown-text-style=assistant-message]` (a user bubble has data-user-message-bubble).
    sel={
        "input": ["div.ProseMirror[contenteditable='true']",
                  "#prompt-textarea",
                  "div[contenteditable='true']"],
        "send": ["button[aria-label='Send']",
                 "[data-testid='send-button']",
                 "button[aria-label*='Send']"],
        "stop": ["button[aria-label='Stop']",
                 "[data-testid='stop-button']",
                 "button[aria-label*='Stop']"],
        "assistant": ["[data-markdown-text-style='assistant-message']",
                      "[data-message-author-role='assistant']"],
        # Measured against the live challenge page: the turnstile iframe has no
        # src attribute at match time, so these three are what actually fire.
        "blocked": ["input[name='cf-turnstile-response']",
                    "script[src*='challenges.cloudflare.com']",
                    "[id*='cf-chl']"],
    },
)

# MEASURED: answered "17*23" with 391, headless, no login.
GEMINI = Provider(
    key="gemini", label="Gemini", url="https://gemini.google.com/app",
    verified=True,
    # MEASURED in the first eval run: a step still had no answer after 90s while the
    # stop button was present, i.e. Gemini was thinking, not failing. Steps take
    # 15-60s normally, so give it room instead of cooling the provider for being slow.
    reply_timeout=240, answer_timeout=240,
    # MEASURED (OSS run, 2026-10-25): ONE 58k-char prompt got "I encountered an error doing what you
    # asked" while steps of 8-12k chars were fine. A single observation, so it is NOT a ceiling: it is
    # only registered as a size-looking error, and the learned limit (limits.py) needs a repeat.
    errors=("something went wrong", "you've reached your limit", "try again later",
            "can't help with that", "i can't help", "i encountered an error doing what you asked"),
    size_errors=("i encountered an error doing what you asked",),
    full_markers=("conversation is too long", "start a new chat", "start a new conversation",
                  "too long for me to"),
    # `model-response` also holds UI chrome ("Show code", "Gemini said", "Processed
    # and analyzed in code"); its `message-content` child is just the answer.
    sel={
        "input": ["div.ql-editor[role='textbox']",
                  "div[contenteditable='true'][role='textbox']",
                  "rich-textarea div[contenteditable='true']"],
        "send": ["button[aria-label='Send message']"],
        "stop": ["button[aria-label='Stop response']"],
        "assistant": ["model-response message-content"],
        "blocked": ["form[action*='consent.google.com']"],
    },
)

# MEASURED: answered "17*23" with 391, headless, no login. The answer renders in
# `div.prose`; a citation chip ("tiger-algebra") trails the text inside it.
# Eval (free-agent/eval, 8 tool-loop tasks): 0/8 -- refuses to use tools in every task.
# Keep it out of the default rotation; it only answers plain questions.
PERPLEXITY = Provider(
    key="perplexity", label="Perplexity", url="https://www.perplexity.ai/",
    verified=True,
    # MEASURED: after a handful of logged-out queries the answer node reads
    # "Sign up and repeat your request." -- that is the anonymous quota, not an answer.
    errors=("something went wrong", "rate limit", "try again later",
            "sign up and repeat", "sign in to continue"),
    full_markers=("thread is too long", "conversation is too long", "start a new thread",
                  "start a new chat"),
    sel={
        "input": ["div#ask-input[contenteditable='true']",
                  "div[contenteditable='true'][role='textbox']"],
        "send": ["button[aria-label='Submit']"],
        "stop": ["button[aria-label^='Stop response']"],
        "assistant": ["div.prose"],
        "blocked": ["input[name='cf-turnstile-response']", "[id*='cf-chl']"],
    },
)

# MEASURED 2026-10-25, headless, logged in (free account): answered "17*23". Auto mode
# "thinks" first, so the first token can take 30s+. Answer text: `.chat-response-message`
# (the user's own message uses `.chat-user-message`, so the two never collide).
QWEN = Provider(
    key="qwen", label="Qwen", url="https://chat.qwen.ai/", shared=True,
    # MEASURED: code renders in a Monaco editor that only keeps on-screen lines in the DOM,
    # so a reply with several tool_call blocks came back as bare "tool_call" labels, and the
    # visible copy has every space as U+00A0. The Copy button's clipboard text is exact.
    copy_button=".copy-response-button",
    # MEASURED 2026-10-25 after ~70 requests in an hour: the page was redirected to
    # /_____tmd_____/punish?x5secdata=... (Alibaba's slide-to-verify) and stayed there.
    challenge_url=("/_____tmd_____/", "x5secdata"),
    reply_timeout=420, answer_timeout=420,
    errors=("something went wrong", "too many requests", "rate limit", "try again later",
            "daily usage limit", "reached the limit"),
    full_markers=("too long", "new chat", "context length", "exceeds the maximum"),
    sel={
        "input": ["textarea.message-input-textarea"],
        "send": ["button.send-button"],
        "stop": ["button.stop-button"],
        "assistant": [".chat-response-message .qwen-markdown"],
        "blocked": [],
    },
)

# MEASURED 2026-10-25, headless, logged in (free account). First send asks for a birth
# year once per account ("Please confirm your age", then Save); that is a personal
# declaration, answered by hand in the profile, never by the bridge. The composer is a
# TipTap editor; Enter does not send, the Submit button does. Replies and the user's own
# message share `.response-content-markdown`; only the reply sits under `.items-start`.
GROK = Provider(
    key="grok", label="Grok", url="https://grok.com/", shared=True,
    reply_timeout=240, answer_timeout=240,
    consent=("button:has-text('Reject All')",),
    errors=("something went wrong", "rate limit", "too many requests", "try again later",
            "limit reached", "usage limit", "upgrade to continue"),
    full_markers=("conversation is too long", "start a new conversation", "start a new chat",
                  "context length", "maximum length"),
    sel={
        "input": ["div.tiptap.ProseMirror[contenteditable='true']"],
        "send": ["[data-testid='chat-submit']"],
        "stop": ["button[aria-label='Stop model response']"],
        "assistant": ["[id^='response-'].items-start .response-content-markdown"],
        "blocked": [],
    },
)

# Passed the tool-use eval: the default rotation, in order (docs/PROVIDERS.md).
ELIGIBLE = ("gemini", "qwen")
# "Continue one chat per task" mode. STICKY_VERIFIED = providers where it was MEASURED live (Gemini: 8/8, 28
# steps in 388 s vs 8/8, 33 steps in 532 s with a fresh chat per step, 2026-10-25). It no longer gates the
# default: sticky is ON for every provider (`--sticky auto` = all; `--sticky none` turns it off), so a provider
# outside this tuple is default-on but NOT live-verified, and ends the chat only on the site's own signals.
STICKY_VERIFIED = ("gemini",)
# Everything registered, eligible first. The rest failed the eval or cannot run headless:
# Grok and Perplexity answer without using tools (0/8), so a turn that lands on them can
# "finish" having changed nothing; ChatGPT is behind a Cloudflare check and needs a window.
EVERYTHING = ("gemini", "qwen", "grok", "perplexity", "chatgpt", "deepseek")


def resolve(spec: str, headed: bool = False) -> list[str]:
    """`--providers` value -> provider keys. "all" means every registered site, eligible ones
    first. Sites that block headless browsers (ChatGPT, DeepSeek) run in their own visible window."""
    if spec.strip().lower() == "all":
        return list(EVERYTHING)
    keys = [k.strip() for k in spec.split(",") if k.strip()]
    unknown = [k for k in keys if k not in PROVIDERS]
    if unknown:
        raise ValueError(f"unknown provider(s) {unknown}; choose from {sorted(PROVIDERS)} or 'all'")
    return keys


# MEASURED 2026-10-25, signed in, VISIBLE window: chat.deepseek.com answers HTTP 403 to headless Chrome
# (CloudFront), so it runs headed in its own profile. Enter sends. The send button is an unlabeled div:
# `ds-button--disabled` while the composer is empty or a reply is done, enabled while a reply
# streams (it has become Stop) and while text is typed, which is why one selector serves both.
DEEPSEEK = Provider(
    key="deepseek", label="DeepSeek", url="https://chat.deepseek.com/",
    headless_ok=False, channel="chrome",
    login_url=("/sign_in", "/login"),
    reply_timeout=120, answer_timeout=180,
    errors=("server is busy", "too many requests", "rate limit", "try again later",
            "something went wrong", "message limit"),
    full_markers=("length limit", "conversation is too long", "start a new chat",
                  "conversation limit"),
    sel={
        "input": ["textarea"],
        "send": [".ds-button--primary.ds-button--circle:not(.ds-button--disabled)"],
        "stop": [".ds-button--primary.ds-button--circle:not(.ds-button--disabled)"],
        "assistant": [".ds-assistant-message-main-content"],
        "blocked": [],
    },
)

PROVIDERS: dict[str, Provider] = {p.key: p for p in (CHATGPT, GEMINI, PERPLEXITY, QWEN, GROK, DEEPSEEK)}

