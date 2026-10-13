import os
import re
import sys
import time
import random
import threading
import argparse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
import requests
from deep_translator import GoogleTranslator
from deep_translator.exceptions import TooManyRequests, RequestError

# Matches {placeholder} interpolation tokens used by the TS i18n `t()` helper.
PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")

# i18n locale keys -> codes understood by GoogleTranslator.
LANG_ALIASES = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-tw": "zh-TW",
    "zh-hans": "zh-CN",
    "zh-hant": "zh-TW",
    "he": "iw",
    "pt-br": "pt",
    "nb": "no",
}


def normalize_lang(lang):
    key = lang.strip().lower()
    if key in LANG_ALIASES:
        return LANG_ALIASES[key]
    if key in ("zh-cn", "zh-tw"):
        return LANG_ALIASES[key]
    return key.split("-")[0]


class ProgressPrinter:
    """Live progress: a percentage bar plus the last few translated lines."""

    def __init__(self, total, recent=4):
        self.total = total
        self.recent = recent
        self.count = 0
        self.lines = []
        self._printed = 0
        self._finished = False
        if total == 0:
            print("Nothing to translate (no missing keys).")

    def _render(self):
        pct = (self.count / self.total * 100) if self.total else 100.0
        filled = int(pct / 100 * 30)
        bar = "#" * filled + "-" * (30 - filled)
        block = [f"[{bar}] {pct:5.1f}%  ({self.count}/{self.total})"]
        block += [f"  {line}" for line in self.lines[-self.recent:]]

        out = sys.stdout
        if self._printed:
            out.write(f"\x1b[{self._printed}A")
        for line in block:
            out.write("\x1b[2K" + line[:200] + "\n")
        out.flush()
        self._printed = len(block)

    def step(self, message):
        self.count += 1
        self.lines.append(message)
        self._render()

    def done(self):
        if self.total and not self._finished:
            self._render()
        self._finished = True


class _RateLimiter:
    """Per-engine throttle shared by all worker threads."""

    def __init__(self, max_per_second=4.0):
        self._min_interval = 1.0 / max_per_second
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            wait_for = self._next_allowed - now
            if wait_for > 0:
                time.sleep(wait_for)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval

    def push_back(self, delay):
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + delay)


# ----------------------------------------------------------------------------
# Translation engines: Google first, then the configured fallbacks in order
# (Microsoft -> DeepL -> offline Argos). An engine that is rate limited, out of
# quota, rejects its key or cannot be reached is skipped for a while; an engine
# that fails on one text (e.g. unsupported language) only passes that text on.
# ----------------------------------------------------------------------------

_ENGINE_COOLDOWN = 300  # seconds an unavailable engine is skipped
_HTTP_TIMEOUT = 30


class EngineUnavailable(Exception):
    """The engine cannot serve requests for a while (rate limit, quota, key, network)."""


class TranslationUnavailable(Exception):
    """Every engine in the chain failed for a text."""


class _TransientError(Exception):
    """A rate-limit or server error worth retrying after a backoff."""


def _lang_code(lang, aliases, default=lambda key: key.split("-")[0]):
    key = lang.strip().lower()
    return aliases[key] if key in aliases else default(key)


class _Engine:
    name = ""
    max_per_second = 4.0
    max_retries = 3
    retryable = (_TransientError,)

    def __init__(self):
        self.limiter = _RateLimiter(self.max_per_second)
        self.disabled_until = 0.0
        self.last_error = None
        self.requests = 0
        self._stats_lock = threading.Lock()

    def available(self):
        return time.monotonic() >= self.disabled_until

    def disable(self, reason):
        self.disabled_until = time.monotonic() + _ENGINE_COOLDOWN
        self.last_error = reason

    def _request(self, text, target_lang, source_lang):
        raise NotImplementedError

    def translate(self, text, target_lang, source_lang):
        for attempt in range(self.max_retries):
            self.limiter.wait()
            try:
                result = self._request(text, target_lang, source_lang)
                with self._stats_lock:
                    self.requests += 1
                return result
            except self.retryable as exc:
                if attempt == self.max_retries - 1:
                    raise EngineUnavailable(f"{type(exc).__name__}: {exc}") from exc
                # Exponential backoff with jitter; also pushes back the engine's limiter.
                delay = min(2 ** attempt, 30) + random.uniform(0, 1)
                self.limiter.push_back(delay)
                time.sleep(delay)
        raise EngineUnavailable("retries exhausted")


class GoogleEngine(_Engine):
    """Google's free endpoint allows ~5 requests/second."""

    name = "google"
    max_retries = 6
    retryable = (TooManyRequests, RequestError)

    def _request(self, text, target_lang, source_lang):
        try:
            return GoogleTranslator(source='auto', target=normalize_lang(target_lang)).translate(text)
        except requests.exceptions.RequestException as exc:
            raise EngineUnavailable(f"network error: {exc}") from exc


def _check_http_status(response, unavailable_codes):
    """Raise for a failed request: retryable, engine-wide or per-text errors."""
    if response.status_code == 200:
        return
    detail = response.text[:200]
    if response.status_code == 429 or response.status_code >= 500:
        raise _TransientError(f"HTTP {response.status_code}: {detail}")
    if response.status_code in unavailable_codes:
        raise EngineUnavailable(f"HTTP {response.status_code}: {detail}")
    raise ValueError(f"HTTP {response.status_code}: {detail}")


class MicrosoftEngine(_Engine):
    """Azure AI Translator (free F0 tier: 2M characters/month)."""

    name = "microsoft"
    max_per_second = 10.0
    _URL = "https://api.cognitive.microsofttranslator.com/translate"
    _ALIASES = {"zh": "zh-Hans", "zh-cn": "zh-Hans", "zh-hans": "zh-Hans",
                "zh-tw": "zh-Hant", "zh-hant": "zh-Hant", "iw": "he", "no": "nb",
                "pt-pt": "pt-pt"}

    def __init__(self, api_key, region=None):
        super().__init__()
        self._headers = {"Ocp-Apim-Subscription-Key": api_key, "Content-Type": "application/json"}
        if region:
            self._headers["Ocp-Apim-Subscription-Region"] = region

    def _request(self, text, target_lang, source_lang):
        try:
            response = requests.post(
                self._URL,
                params={"api-version": "3.0", "to": _lang_code(target_lang, self._ALIASES)},
                headers=self._headers, json=[{"Text": text}], timeout=_HTTP_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            raise EngineUnavailable(f"network error: {exc}") from exc
        # 401: bad key or region, 403: free quota used up.
        _check_http_status(response, unavailable_codes=(401, 403))
        return response.json()[0]["translations"][0]["text"]


class DeepLEngine(_Engine):
    """DeepL API (Free keys end in ':fx', 500k characters/month)."""

    name = "deepl"
    max_per_second = 5.0
    _ALIASES = {"en": "EN-US", "en-us": "EN-US", "en-gb": "EN-GB",
                "pt": "PT-PT", "pt-pt": "PT-PT", "pt-br": "PT-BR",
                "zh": "ZH-HANS", "zh-cn": "ZH-HANS", "zh-hans": "ZH-HANS",
                "zh-tw": "ZH-HANT", "zh-hant": "ZH-HANT",
                "no": "NB", "nb": "NB", "iw": "HE"}

    def __init__(self, api_key):
        super().__init__()
        host = "api-free.deepl.com" if api_key.endswith(":fx") else "api.deepl.com"
        self._url = f"https://{host}/v2/translate"
        self._headers = {"Authorization": f"DeepL-Auth-Key {api_key}"}

    def _request(self, text, target_lang, source_lang):
        target = _lang_code(target_lang, self._ALIASES, default=lambda key: key.split("-")[0].upper())
        try:
            response = requests.post(
                self._url, headers=self._headers,
                json={"text": [text], "target_lang": target, "preserve_formatting": True},
                timeout=_HTTP_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            raise EngineUnavailable(f"network error: {exc}") from exc
        # 401/403: bad key, 456: quota used up.
        _check_http_status(response, unavailable_codes=(401, 403, 456))
        return response.json()["translations"][0]["text"]


class ArgosEngine(_Engine):
    """Offline Argos Translate. Missing language models are downloaded on first use."""

    name = "argos"
    max_per_second = 1000.0
    max_retries = 1
    _ALIASES = {"zh-cn": "zh", "zh-hans": "zh", "zh-tw": "zt", "zh-hant": "zt",
                "iw": "he", "no": "nb"}

    def __init__(self):
        super().__init__()
        from argostranslate import package, settings, translate
        self._package, self._settings, self._translate = package, settings, translate
        self._lock = threading.Lock()  # model loading and installs are not thread safe
        self._index_updated = False
        self._missing = set()

    def _translation(self, src, tgt):
        from_lang = self._translate.get_language_from_code(src)
        to_lang = self._translate.get_language_from_code(tgt)
        if from_lang is None or to_lang is None:
            return None
        return from_lang.get_translation(to_lang)

    def _install(self, src, tgt):
        """Download the models for src -> tgt, directly or pivoting through English."""
        if not self._index_updated:
            self._package.update_package_index()
            self._index_updated = True
        if not os.path.exists(self._settings.local_package_index):
            raise EngineUnavailable("Argos package index could not be downloaded")
        print(f"Downloading Argos language model(s) for {src} -> {tgt}...")
        if not self._package.install_package_for_language_pair(src, tgt):
            for pair in ((src, "en"), ("en", tgt)):
                if pair[0] != pair[1] and self._translation(*pair) is None:
                    self._package.install_package_for_language_pair(*pair)

    def _request(self, text, target_lang, source_lang):
        src = _lang_code(source_lang, self._ALIASES)
        tgt = _lang_code(target_lang, self._ALIASES)
        with self._lock:
            if (src, tgt) in self._missing:
                raise ValueError(f"no Argos model for {src} -> {tgt}")
            translation = self._translation(src, tgt)
            if translation is None:
                self._install(src, tgt)
                translation = self._translation(src, tgt)
            if translation is None:
                self._missing.add((src, tgt))
                raise ValueError(f"no Argos model for {src} -> {tgt}")
            # Translate line by line, passing batch marker lines through untouched.
            return "\n".join(
                line if not line.strip() or _SEGMENT_RE.fullmatch(line.strip())
                else translation.translate(line)
                for line in text.split("\n"))


class TranslatorChain:
    def __init__(self, engines):
        self.engines = engines

    def describe(self):
        return " -> ".join(engine.name for engine in self.engines)

    def reset_stats(self):
        for engine in self.engines:
            engine.requests = 0
            engine.last_error = None

    def summary(self):
        """Per-engine request counts and reasons, or None if only the first engine was needed."""
        used = [e for e in self.engines if e.requests or e.last_error]
        if not any(e.last_error for e in used) and len(used) <= 1:
            return None
        lines = ["Translation engines used:"]
        for engine in used:
            note = f" (unavailable: {engine.last_error})" if engine.last_error else ""
            lines.append(f"  {engine.name}: {engine.requests} request(s){note}")
        return "\n".join(lines)

    def translate(self, text, target_lang, source_lang):
        # When every engine is cooling down, try them all anyway rather than fail fast.
        engines = [e for e in self.engines if e.available()] or self.engines
        errors = []
        for engine in engines:
            try:
                return engine.translate(text, target_lang, source_lang)
            except EngineUnavailable as exc:
                engine.disable(str(exc))
                errors.append(f"{engine.name}: {exc}")
            except Exception as exc:
                errors.append(f"{engine.name}: {type(exc).__name__}: {exc}")
        raise TranslationUnavailable("; ".join(errors))


_CHAIN = TranslatorChain([GoogleEngine()])


def configure_engines(microsoft_key=None, microsoft_region=None, deepl_key=None, use_argos=True):
    """Set up the fallback chain: Google -> Microsoft -> DeepL -> Argos (offline)."""
    global _CHAIN
    engines = [GoogleEngine()]
    if microsoft_key:
        engines.append(MicrosoftEngine(microsoft_key, microsoft_region))
    if deepl_key:
        engines.append(DeepLEngine(deepl_key))
    if use_argos:
        try:
            engines.append(ArgosEngine())
        except ImportError:
            pass  # argostranslate is optional
    _CHAIN = TranslatorChain(engines)
    print(f"Translation engines: {_CHAIN.describe()}")


def translate_text(text, target_lang, source_lang="en"):
    """Translate `text` with the first engine that succeeds.

    `source_lang` is only used by the offline engine; online engines detect it.
    """
    if text is None or str(text).strip() == "":
        return text
    return _CHAIN.translate(text, target_lang, source_lang)


# Tolerates case changes, stray spaces and native digits (`\d` is Unicode-aware)
# the translator may introduce into an XPLH<n>X sentinel.
_SENTINEL_RE = re.compile(r"X\s*P\s*L\s*H\s*(\d+)\s*X", re.IGNORECASE)


def _protect_placeholders(text):
    """Swap {placeholder} tokens for sentinels that survive machine translation intact."""
    protected = text
    tokens = PLACEHOLDER_RE.findall(text)
    for i, token in enumerate(tokens):
        protected = protected.replace(token, f"XPLH{i}X", 1)
    return protected, tokens


def _restore_placeholders(text, tokens):
    if not tokens:
        return text

    def _token(match):
        index = int(match.group(1))
        return tokens[index] if index < len(tokens) else match.group(0)

    return _SENTINEL_RE.sub(_token, text)


def translate_preserving_placeholders(text, target_lang, source_lang="en"):
    """Translate `text` while keeping {placeholder} tokens untouched."""
    if text is None or text.strip() == "":
        return text

    protected, restore = _protect_placeholders(text)
    translated = translate_text(protected, target_lang, source_lang) or protected
    return _restore_placeholders(translated, restore)


# ----------------------------------------------------------------------------
# Batching: one request per language carries many texts
# ----------------------------------------------------------------------------

# Google blocks an IP that sends one request per text (thousands per run), so
# texts are sent together, each after a numbered marker line, and the reply is
# split at the markers. A batch whose markers do not come back intact is halved
# and retried, down to one text per request.
DEFAULT_BATCH_CHARS = 3000
_SEGMENT_MARKER = "XSEG{}X"
# Tolerates case changes, stray spaces and native digits (`\d` is Unicode-aware).
_SEGMENT_RE = re.compile(r"X\s*S\s*E\s*G\s*(\d+)\s*X", re.IGNORECASE)


def _is_blank(text):
    return text is None or str(text).strip() == ""


def _split_segments(translated, count):
    """The texts between markers 0..count-1, or None unless all came back once, in order."""
    matches = list(_SEGMENT_RE.finditer(translated))
    if [int(m.group(1)) for m in matches] != list(range(count)):
        return None
    ends = [m.start() for m in matches[1:]] + [len(translated)]
    parts = [translated[m.end():end].strip() for m, end in zip(matches, ends)]
    return None if any(part == "" for part in parts) else parts


def _translate_segments(texts, target_lang, protect_placeholders, source_lang):
    if len(texts) == 1:
        if protect_placeholders:
            return [translate_preserving_placeholders(texts[0], target_lang, source_lang)]
        return [translate_text(texts[0], target_lang, source_lang)]

    protected = [_protect_placeholders(t) if protect_placeholders else (t, []) for t in texts]
    joined = "\n".join(f"{_SEGMENT_MARKER.format(i)}\n{text}" for i, (text, _) in enumerate(protected))
    try:
        parts = _split_segments(translate_text(joined, target_lang, source_lang) or "", len(texts))
    except TranslationUnavailable:
        raise  # halving would only send more requests to engines refusing them
    except Exception:
        parts = None
    if parts is None:
        mid = len(texts) // 2
        return (_translate_segments(texts[:mid], target_lang, protect_placeholders, source_lang)
                + _translate_segments(texts[mid:], target_lang, protect_placeholders, source_lang))
    return [_restore_placeholders(part, restore) for part, (_, restore) in zip(parts, protected)]


def translate_texts(texts, target_lang, protect_placeholders=True, source_lang="en"):
    """Translate `texts` into one language with as few requests as possible, in order."""
    results = list(texts)
    todo = [i for i, text in enumerate(texts) if not _is_blank(text)]
    if todo:
        translated = _translate_segments([texts[i] for i in todo], target_lang, protect_placeholders,
                                         source_lang)
        for i, value in zip(todo, translated):
            results[i] = value
    return results


def chunk_worklist(worklist, lang_of, text_of, max_chars=DEFAULT_BATCH_CHARS):
    """Group work items into per-language chunks of at most `max_chars` characters.

    A text longer than `max_chars` gets a chunk of its own.
    """
    by_lang = {}
    for item in worklist:
        by_lang.setdefault(lang_of(item), []).append(item)

    chunks = []
    for items in by_lang.values():
        chunk, size = [], 0
        for item in items:
            # The marker line costs a few characters too.
            cost = len(text_of(item) or "") + len(_SEGMENT_MARKER.format(len(chunk))) + 2
            if chunk and size + cost > max_chars:
                chunks.append(chunk)
                chunk, size = [], 0
            chunk.append(item)
            size += cost
        if chunk:
            chunks.append(chunk)
    return chunks


def run_translations(chunks, translate_fn, workers, progress=None, describe=None):
    """Translate `chunks` (lists of work items) concurrently, returning (item, translated) pairs.

    `translate_fn(chunk)` returns the translations of the chunk's items, in order.
    Only these network-bound calls run in the thread pool; results are consumed on
    the calling thread, so progress printing and file writing stay serial.
    """
    results = []
    failures = []
    if not chunks:
        return results
    _CHAIN.reset_stats()

    def _safe(chunk):
        try:
            return chunk, translate_fn(chunk), None
        except Exception as exc:  # keep going so partial results are still written
            return chunk, None, exc

    workers = max(1, min(workers, len(chunks)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for chunk, translations, error in executor.map(_safe, chunks):
            for i, item in enumerate(chunk):
                translated = None if error is not None else translations[i]
                if error is not None:
                    failures.append((item, error))
                else:
                    results.append((item, translated))
                if progress is not None:
                    shown = f"FAILED: {error}" if error else (
                        describe(item, translated) if describe else str(translated))
                    progress.step(shown)
                elif describe is not None and not error:
                    print(describe(item, translated))

    # Finish the redrawn progress block before printing below it, or it overwrites these lines.
    if progress is not None:
        progress.done()
    if failures:
        print(f"\n{len(failures)} item(s) could not be translated and were left unchanged:")
        for item, error in failures[:20]:
            print(f"  {item}: {error}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")
    summary = _CHAIN.summary()
    if summary:
        print("\n" + summary)
    return results


# ----------------------------------------------------------------------------
# TypeScript i18n support (inline `const DICTS: Record<Lang, Dict> = { ... }`)
# ----------------------------------------------------------------------------

def _find_matching_brace(text, open_index):
    """Return the index of the `}` matching the `{` at `open_index`."""
    depth = 0
    in_str = None
    i = open_index
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in "\"'`":
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("Unbalanced braces while parsing DICTS object")


def _parse_string_entries(body):
    """Parse a flat JS object body into an ordered list of (key, value) pairs.

    Only string-valued entries are returned; keys may be quoted or bare.
    """
    entries = []
    i = 0
    n = len(body)

    def read_string(idx):
        quote = body[idx]
        idx += 1
        buf = []
        while idx < n:
            ch = body[idx]
            if ch == "\\":
                buf.append(body[idx:idx + 2])
                idx += 2
                continue
            if ch == quote:
                return "".join(buf), idx + 1
            buf.append(ch)
            idx += 1
        raise ValueError("Unterminated string in DICTS entry")

    while i < n:
        ch = body[i]
        if ch in " \t\r\n,":
            i += 1
            continue
        if body.startswith("//", i):
            i = body.find("\n", i)
            if i == -1:
                break
            continue
        if body.startswith("/*", i):
            end = body.find("*/", i)
            i = end + 2 if end != -1 else n
            continue

        # --- key ---
        if ch in "\"'`":
            key, i = read_string(i)
        else:
            m = re.match(r"[A-Za-z0-9_$.\-]+", body[i:])
            if not m:
                i += 1
                continue
            key = m.group(0)
            i += m.end()

        while i < n and body[i] in " \t\r\n":
            i += 1
        if i >= n or body[i] != ":":
            continue
        i += 1
        while i < n and body[i] in " \t\r\n":
            i += 1
        if i >= n:
            break

        # --- value ---
        if body[i] in "\"'`":
            raw, i = read_string(i)
            value = raw.encode().decode("unicode_escape") if "\\" in raw else raw
            entries.append((key, value))
        else:
            # Non-string value (nested object, number, etc.) - skip it.
            m = re.match(r"[^,{}\[\]]+", body[i:])
            i += m.end() if m else 1
    return entries


def _escape_ts_string(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def process_i18n_ts(file_path, force_translate, exclude_languages, source_lang, workers=8,
                     batch_chars=DEFAULT_BATCH_CHARS):
    with open(file_path, "r", encoding="utf-8") as fh:
        content = fh.read()

    match = re.search(r"(?:const|let|var)\s+DICTS\b[^=]*=\s*", content)
    if not match:
        print("DICTS declaration not found in i18n file!")
        return

    brace_start = content.index("{", match.end())
    brace_end = _find_matching_brace(content, brace_start)
    dicts_body = content[brace_start + 1:brace_end]

    # Split the DICTS body into `lang: { ... }` sections, preserving order.
    langs = []
    i = 0
    while i < len(dicts_body):
        m = re.match(r"\s*([A-Za-z0-9_$'\"-]+)\s*:\s*\{", dicts_body[i:])
        if not m:
            i += 1
            continue
        lang_name = m.group(1).strip("'\"")
        inner_open = i + m.end() - 1
        inner_close = _find_matching_brace(dicts_body, inner_open)
        inner_body = dicts_body[inner_open + 1:inner_close]
        langs.append([lang_name, _parse_string_entries(inner_body)])
        i = inner_close + 1

    lang_map = {name: dict(entries) for name, entries in langs}
    if source_lang not in lang_map:
        print(f"Source language '{source_lang}' not found in DICTS (have: {', '.join(lang_map)})")
        return

    source_entries = next(entries for name, entries in langs if name == source_lang)

    # Build the full worklist first so progress can be shown as a percentage.
    worklist = []
    for name, _ in langs:
        if name == source_lang or name in exclude_languages:
            continue
        existing = lang_map[name]
        for key, src_value in source_entries:
            if not force_translate and key in existing and existing[key].strip() != "":
                continue
            worklist.append((name, key, src_value))

    progress = ProgressPrinter(len(worklist), recent=4)
    results = run_translations(
        chunk_worklist(worklist, lambda item: item[0], lambda item: item[2], batch_chars),
        lambda chunk: translate_texts([item[2] for item in chunk], chunk[0][0], source_lang=source_lang),
        workers,
        progress=progress,
        describe=lambda item, translated: f"({item[0]}) {item[1]}: {item[2]} --> {translated}",
    )
    for (name, key, _src), translated in results:
        lang_map[name][key] = translated
    progress.done()

    # Rebuild the DICTS object using the source key order for every language.
    indent = "  "
    blocks = []
    for name, _ in langs:
        values = lang_map[name]
        ordered_keys = [k for k, _ in source_entries]
        ordered_keys += [k for k in values if k not in ordered_keys]
        lines = [f"{indent}{name}: {{"]
        for key in ordered_keys:
            if key not in values:
                continue
            key_repr = f'"{key}"'
            lines.append(f'{indent}{indent}{key_repr}: "{_escape_ts_string(values[key])}",')
        lines.append(f"{indent}}},")
        blocks.append("\n".join(lines))

    new_body = "\n" + "\n".join(blocks) + "\n"
    new_content = content[:brace_start + 1] + new_body + content[brace_end:]
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print("\ni18n TypeScript translation completed!")

def process_resx_files(directory, force_translate, exclude_languages, workers=8,
                       batch_chars=DEFAULT_BATCH_CHARS, source_lang="en"):
    original_file = os.path.join(directory, "AppRes.resx")
    if not os.path.exists(original_file):
        print("Original resx file not found!")
        return

    tree = ET.parse(original_file)
    root = tree.getroot()
    original_entries = {entry.attrib['name']: entry for entry in root.findall('data')}

    # --- Phase A: collect per-file targets and the global translation worklist ---
    targets = []   # (lang_code, file_path, resx_root)
    worklist = []  # (lang_code, name, orig_entry, orig_value)
    for file_name in sorted(os.listdir(directory)):
        if not (file_name.startswith("AppRes.") and file_name.endswith(".resx") and file_name != "AppRes.resx"):
            continue
        lang_code = file_name[7:-5]  # Extracting language code from filename
        if lang_code in exclude_languages:
            continue

        file_path = os.path.join(directory, file_name)
        if os.path.exists(file_path):
            resx_root = ET.parse(file_path).getroot()
        else:
            resx_root = ET.Element("root")
            for element in root.findall("resheader"):
                resx_root.append(element)
            for schema in root.findall("{http://www.w3.org/2001/XMLSchema}schema"):
                resx_root.append(schema)

        existing_entries = {entry.attrib['name']: entry for entry in resx_root.findall('data')}
        targets.append((lang_code, file_path, resx_root))

        for name, orig_entry in original_entries.items():
            if force_translate or name not in existing_entries:
                orig_value_elem = orig_entry.find('value')
                orig_value = orig_value_elem.text if orig_value_elem is not None else ""
                worklist.append((lang_code, name, orig_entry, orig_value))

    # --- Phase B: translate every missing key across all languages in parallel ---
    progress = ProgressPrinter(len(worklist), recent=4)
    results = run_translations(
        chunk_worklist(worklist, lambda item: item[0], lambda item: item[3], batch_chars),
        lambda chunk: translate_texts([item[3] for item in chunk], chunk[0][0][:2], protect_placeholders=False,
                                      source_lang=source_lang),
        workers,
        progress=progress,
        describe=lambda item, translated: f"({item[0]}) {item[1]}: {item[3]} --> {translated}",
    )
    progress.done()

    # --- Phase C: build and write each file exactly once, on this thread ---
    by_lang = {}
    for (lang_code, name, orig_entry, _orig_value), translated in results:
        new_entry = ET.Element("data", name=name)
        if 'xml:space' in orig_entry.attrib:
            new_entry.set("xml:space", orig_entry.attrib['xml:space'])
        value_elem = ET.SubElement(new_entry, "value")
        value_elem.text = translated

        comment_elem = orig_entry.find("comment")
        if comment_elem is not None:
            new_comment = ET.SubElement(new_entry, "comment")
            new_comment.text = comment_elem.text

        by_lang.setdefault(lang_code, []).append(new_entry)

    for lang_code, file_path, resx_root in targets:
        for new_entry in by_lang.get(lang_code, []):
            resx_root.append(new_entry)
        ET.ElementTree(resx_root).write(file_path, encoding="utf-8", xml_declaration=True)
        print(f"Translate ({lang_code}): Done!")

    print("\nTranslation process completed!")

def main():
    parser = argparse.ArgumentParser(description="Translate .resx files")
    parser.add_argument("--resx-directory", type=str, help="Path to the .resx directory", required=False)
    parser.add_argument("--i18n-file", type=str, help="Path to a TypeScript i18n file (inline `const DICTS` object)", required=False)
    parser.add_argument("--i18n-source-lang", type=str, help="Source language key inside DICTS (default: en)", default="en")
    parser.add_argument("--exclude-languages", type=str, help="Comma-separated list of languages to exclude", required=False, default="")
    parser.add_argument("--force", action="store_true", help="Force re-translate all entries")
    parser.add_argument("--new-only", action="store_true", help="Only translate new entries")
    parser.add_argument("--workers", type=int, default=8, help="Parallel translation workers (default: 8)")
    parser.add_argument("--batch-chars", type=int, default=DEFAULT_BATCH_CHARS,
                        help=f"Characters sent per request, many texts at once (default: {DEFAULT_BATCH_CHARS}, max 4900)")
    parser.add_argument("--resx-source-lang", type=str, default="en",
                        help="Language of AppRes.resx, used by the offline fallback (default: en)")
    parser.add_argument("--microsoft-key", type=str, help="Azure Translator key: fallback after Google")
    parser.add_argument("--microsoft-region", type=str,
                        help="Azure Translator resource region (e.g. westeurope), unless the resource is global")
    parser.add_argument("--deepl-key", type=str, help="DeepL API key: fallback after Microsoft")
    parser.add_argument("--no-argos", action="store_true",
                        help="Disable the offline Argos Translate fallback (used when argostranslate is installed)")

    args = parser.parse_args()
    configure_engines(args.microsoft_key, args.microsoft_region, args.deepl_key, not args.no_argos)

    exclude_languages = args.exclude_languages.split(',') if args.exclude_languages else []
    batch_chars = max(1, min(args.batch_chars, 4900))

    if args.i18n_file:
        if args.force:
            process_i18n_ts(args.i18n_file, True, exclude_languages, args.i18n_source_lang, args.workers, batch_chars)
            sys.exit(0)
        elif args.new_only:
            process_i18n_ts(args.i18n_file, False, exclude_languages, args.i18n_source_lang, args.workers, batch_chars)
            sys.exit(0)

    if args.resx_directory:
        if args.force:
            process_resx_files(args.resx_directory, True, exclude_languages, args.workers, batch_chars,
                               args.resx_source_lang)
            sys.exit(0)
        elif args.new_only:
            process_resx_files(args.resx_directory, False, exclude_languages, args.workers, batch_chars,
                               args.resx_source_lang)
            sys.exit(0)
    
    while True:
        print("Menu:")
        print("1. resx: Start translation (only new entries)")
        print("2. resx: Force re-translate all entries")
        print("3. i18n.ts: Translate missing keys (only new)")
        print("4. i18n.ts: Force re-translate all keys")
        print("5. Exit")
        choice = input("Choose an option: ")

        if choice in ["1", "2", "3", "4"]:
            exclude_languages = input("Enter languages to exclude (comma separated, optional): ").split(',') if input("Exclude any languages? (y/n): ").lower() == "y" else []
            try:
                workers = int(input("Parallel workers [8]: ").strip() or "8")
            except ValueError:
                workers = 8

        if choice in ["1", "2"]:
            directory = input("Enter the directory of resx files: ")
            process_resx_files(directory, choice == "2", exclude_languages, workers,
                               source_lang=args.resx_source_lang)
        elif choice in ["3", "4"]:
            i18n_file = input("Enter the path to the i18n .ts file: ")
            source_lang = input("Source language key [en]: ").strip() or "en"
            process_i18n_ts(i18n_file, choice == "4", exclude_languages, source_lang, workers)
        elif choice == "5":
            print("Exiting...")
            sys.exit(0)
        else:
            print("Invalid option, please choose again.")

if __name__ == "__main__":
    main()
