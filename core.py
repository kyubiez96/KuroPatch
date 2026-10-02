#!/usr/bin/env python3
"""KuroPatch core engine — decompile, extract, translate, patch, rebuild, sign.

Bugs this file used to have, now fixed:

* ``apktool d -r`` skipped resource decoding, so ``res/values/strings.xml``
  never existed and every resource string was silently missed.
* Smali literals were extracted, translated, counted — and then never written
  back, because nothing recorded where they came from. :class:`StringSource`
  records the origin, and :func:`patcher.patch_smali_literals` applies it.
* :meth:`TranslationQueue.get_stats` could report a negative "pending" count,
  and :meth:`TranslationQueue.mark_done` counted completions for keys it never
  found.
* Translations for the target language were written into *every* ``values-*``
  folder, destroying existing translations of other languages.
* ``download_tools`` was a placeholder that always returned False.
* ``rebuild_apk`` crashed with a TypeError when apktool was missing, because
  ``" ".join(cmd)`` was called with a ``None`` element.
* The jarsigner fallback used SHA1, which Android has rejected for signing
  since API 18.
"""

from __future__ import annotations

import glob
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from patcher import iter_smali_literals, patch_smali_literals
from translator import DEFAULT_SOURCE, DEFAULT_TARGET, TranslatorEngine, should_translate

try:  # optional progress bar
    from tqdm import tqdm

    HAS_TQDM = True
except ImportError:  # pragma: no cover
    HAS_TQDM = False

APKTOOL_URL = "https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar"
JADX_URL = "https://github.com/skylot/jadx/releases/download/v1.5.0/jadx-1.5.0.zip"

TOOL_URLS = {
    "apktool": APKTOOL_URL,
    "jadx": JADX_URL,
}

# <string name="foo" ...>body</string> and the self-closing variant.
_STRING_RE = re.compile(
    r'<string\s+name="(?P<name>[^"]+)"(?P<attrs>[^>]*?)(?:/>|>(?P<body>.*?)</string>)',
    re.DOTALL,
)
# A whole XML tag, including its attributes and slash: <b>, </b>, <xliff:g id="x">
_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z][\w.:-]*(?:\s[^<>]*?)?/?\s*>")
_TAG_NAME_RE = re.compile(r"<\s*(/?)\s*([A-Za-z][\w.:-]*)")
# The <resources ...> opening tag, including any xmlns declarations.
_RESOURCES_OPEN_RE = re.compile(r"<resources\b[^>]*>")


def _xml_escape(text: str) -> str:
    """Escape a text node."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_unescape(text: str) -> str:
    """Unescape a text node."""
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


def _decode_resource_body(body: str) -> str:
    """Decode an Android resource body: unescape text nodes, keep tags.

    Escaping is applied per text node rather than to the whole string, so an
    ``&lt;hi&gt;`` in the source stays literal text instead of turning into a
    tag on the way back out.
    """
    out: List[str] = []
    cursor = 0
    for match in _TAG_RE.finditer(body):
        out.append(_xml_unescape(body[cursor:match.start()]))
        out.append(match.group(0))
        cursor = match.end()
    out.append(_xml_unescape(body[cursor:]))
    return "".join(out)


def _tag_name(tag: str) -> str:
    """``</xliff:g>`` -> ``/xliff:g``; ``<b>`` -> ``b``."""
    match = _TAG_NAME_RE.match(tag)
    if not match:
        return tag
    return f"{match.group(1)}{match.group(2)}"


def _encode_resource_body(value: str, allowed_tags: Optional[set] = None) -> str:
    """Inverse of :func:`_decode_resource_body`: escape text, keep real tags.

    ``allowed_tags`` limits which tags survive verbatim. A translation that
    happens to contain ``<hi>`` is escaped as literal text unless the original
    value really had a ``<hi>`` tag in it.
    """
    stashed: List[str] = []

    def replace(match: re.Match) -> str:
        tag = match.group(0)
        if allowed_tags is None or _tag_name(tag) in allowed_tags:
            stashed.append(tag)
        else:
            # Literal text that merely looks like a tag. It is escaped here, not
            # before stashing, or the outer escape would double-encode it.
            stashed.append(_xml_escape(tag))
        return f"\x00TAG{len(stashed) - 1}\x00"

    escaped = _xml_escape(_TAG_RE.sub(replace, value))
    return re.sub(
        r"\x00TAG(\d+)\x00",
        lambda match: stashed[int(match.group(1))],
        escaped,
    )


# ---------------------------------------------------------------------------
# Job bookkeeping
# ---------------------------------------------------------------------------
@dataclass
class TranslationJob:
    """Individual string translation task"""

    key: str
    original: str
    translated: str = ""
    status: str = "pending"  # pending, queued, processing, done, error, skipped
    error: str = ""
    retry_count: int = 0


@dataclass
class TranslationQueue:
    """Batch translation queue with progress tracking.

    Counters are consistent by construction: ``completed + in_progress + pending
    == total`` at all times, and a key can only be counted once.
    """

    items: List[TranslationJob] = field(default_factory=list)
    completed: int = 0
    total: int = 0
    in_progress: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    progress_callback: Optional[Callable] = None

    def add(self, job: TranslationJob) -> None:
        with self.lock:
            self.items.append(job)
            self.total = len(self.items)

    def get(self, key: str) -> Optional[TranslationJob]:
        with self.lock:
            for job in self.items:
                if job.key == key:
                    return job
        return None

    def mark_start(self, key: str) -> None:
        with self.lock:
            job = self._find(key)
            if job is not None and job.status in ("pending", "queued"):
                job.status = "processing"
                self.in_progress += 1
                notify = self.progress_callback
            else:
                notify = None
        if notify:
            notify(self.completed, self.total)

    def mark_done(self, key: str, translated: str, error: Optional[str] = None) -> None:
        with self.lock:
            job = self._find(key)
            if job is None:
                return
            was_counted = job.status == "processing"
            job.translated = translated
            job.status = "done" if not error else "error"
            job.error = error or ""
            if was_counted:
                self.in_progress = max(0, self.in_progress - 1)
                self.completed += 1
            notify = self.progress_callback
            done, total = self.completed, self.total
        if notify:
            notify(done, total)

    def mark_skipped(self, key: str, reason: str = "") -> None:
        with self.lock:
            job = self._find(key)
            if job is None:
                return
            was_counted = job.status == "processing"
            job.status = "skipped"
            job.error = reason
            if was_counted:
                self.in_progress = max(0, self.in_progress - 1)
                self.completed += 1
            notify = self.progress_callback
            done, total = self.completed, self.total
        if notify:
            notify(done, total)

    def _find(self, key: str) -> Optional[TranslationJob]:
        # Callers already hold the lock.
        for job in self.items:
            if job.key == key:
                return job
        return None

    def get_stats(self) -> Dict:
        with self.lock:
            total = len(self.items)
            return {
                "completed": self.completed,
                "total": total,
                "in_progress": self.in_progress,
                "pending": max(0, total - self.completed - self.in_progress),
                "done": sum(1 for j in self.items if j.status == "done"),
                "error": sum(1 for j in self.items if j.status == "error"),
                "skipped": sum(1 for j in self.items if j.status == "skipped"),
                "items": self.items,
            }


@dataclass
class StringSource:
    """Where an extracted string came from, so it can be written back."""

    value: str
    kind: str  # "xml" | "smali"
    path: str
    key: str = ""

    @property
    def is_translatable(self) -> bool:
        return bool(self.value)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class GameTranslator:
    """Main translator engine for Android games"""

    def __init__(self, output_dir: Optional[str] = None, keep_workspace: bool = False):
        self.workspace = tempfile.mkdtemp(prefix="game_translator_")
        self.output_dir = output_dir or self.workspace
        self.keep_workspace = keep_workspace

        self.tools: Dict[str, Optional[str]] = {
            "apktool": None,
            "jadx": None,
            "apksigner": None,
            "zipalign": None,
        }
        self.keystore_path: Optional[str] = None
        self.keystore_pass = "android"
        self.alias = "androiddebugkey"
        self.key_pass = "android"

        # Translation config
        self.target_lang = DEFAULT_TARGET
        self.source_lang = DEFAULT_SOURCE
        self.api_key = os.getenv("TRANSLATE_API_KEY", "")
        self.batch_size = 50
        self.max_retries = 3
        self.workers = 4

        # Where every extracted string came from.
        self.sources: List[StringSource] = []
        # Distinct value -> translation, produced by translate_strings().
        self.translations: Dict[str, str] = {}
        # Default strings.xml snapshot, captured before anything is written.
        self._default_values: Optional[Dict[str, str]] = None
        # path -> resource name -> tags literally present in the original body.
        self._tags_by_path: Dict[str, Dict[str, set]] = {}

        self.queue = TranslationQueue()
        self.callbacks: Dict[str, Optional[Callable]] = {
            "status": None,
            "progress": None,
            "log": None,
        }
        self.engine: Optional[TranslatorEngine] = None

    # -- plumbing ----------------------------------------------------------
    def set_callbacks(self, **kwargs) -> None:
        """Register progress/status callbacks"""
        self.callbacks.update(kwargs)
        if "progress" in kwargs:
            self.queue.progress_callback = kwargs["progress"]

    def log(self, message: str) -> None:
        """Log message to callback or stdout"""
        if self.callbacks.get("log"):
            self.callbacks["log"](message)
        else:
            print(f"[LOG] {message}")

    def set_status(self, status: str, details: str = "") -> None:
        """Update status callback"""
        if self.callbacks.get("status"):
            self.callbacks["status"](status, details)

    # -- tools -------------------------------------------------------------
    def _find_sdk_tool(self, tool: str) -> Optional[str]:
        """Locate an SDK build-tool, honouring ANDROID_SDK and ANDROID_HOME."""
        roots = [
            os.environ.get("ANDROID_SDK_ROOT"),
            os.environ.get("ANDROID_SDK"),
            os.environ.get("ANDROID_HOME"),
            os.path.expanduser("~/Android/Sdk"),
            os.path.expanduser("~/Library/Android/sdk"),
        ]
        for root in roots:
            if not root or not os.path.isdir(root):
                continue
            pattern = os.path.join(root, "build-tools", "*", tool)
            matches = sorted(glob.glob(pattern))
            if matches:
                # Newest build-tools first.
                return matches[-1]
        found = shutil.which(tool)
        return found

    def setup_tools(self, tools_dir: str) -> bool:
        """Verify and setup required tools.

        A missing local copy falls back to the SDK build-tools and then to
        ``PATH``; only tools that cannot be found anywhere are fatal, and only
        apktool is truly required.
        """
        self.log(f"Setting up tools in {tools_dir}")

        for tool in ("apktool", "jadx", "apksigner", "zipalign"):
            local = os.path.join(tools_dir, tool)
            if os.path.exists(local):
                self.tools[tool] = local
                self.log(f"  + {tool} found (local)")
                continue

            sdk = self._find_sdk_tool(tool)
            if sdk:
                self.tools[tool] = sdk
                self.log(f"  + {tool} found ({sdk})")
                continue

            self.tools[tool] = None
            self.log(f"  - {tool} not found")

        if not self.tools.get("apktool"):
            self.log("Error: apktool is required")
            return False
        return True

    def _download(self, url: str, dest: str, timeout: int = 180) -> bool:
        """Download ``url`` to ``dest`` atomically. Returns success."""
        tmp = f"{dest}.part"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            request = urllib.request.Request(url, headers={"User-Agent": "KuroPatch"})
            with urllib.request.urlopen(request, timeout=timeout) as response, open(tmp, "wb") as handle:
                shutil.copyfileobj(response, handle)
            if os.path.getsize(tmp) == 0:
                raise OSError("empty download")
            os.replace(tmp, dest)
            return True
        except (OSError, urllib.error.URLError, ValueError) as exc:
            if os.path.exists(tmp):
                os.unlink(tmp)
            self.log(f"    download failed: {exc}")
            return False

    def download_tools(self, tools_dir: str, callback: Optional[Callable] = None) -> Dict[str, bool]:
        """Download required tools if not present."""
        results: Dict[str, bool] = {}
        os.makedirs(tools_dir, exist_ok=True)

        self.log("Checking for required tools...")

        for tool in ("apktool", "jadx", "apksigner", "zipalign"):
            tool_path = os.path.join(tools_dir, tool)

            if os.path.exists(tool_path):
                results[tool] = True
                self.log(f"  + {tool} already present")
                continue

            url = TOOL_URLS.get(tool)
            if url:
                self.log(f"  Downloading {tool} from {url}")
                if callback:
                    callback(f"Downloading {tool}...", 0)
                if not self._download(url, tool_path):
                    results[tool] = False
                    self.log(f"  - {tool} download failed")
                    continue
                if tool_path.endswith(".zip") or zipfile.is_zipfile(tool_path):
                    self._extract_zip(tool_path, os.path.join(tools_dir, tool))
                    # The archive is not the tool; find the executable inside it.
                    extracted = glob.glob(os.path.join(tools_dir, tool, "**", tool), recursive=True)
                    if extracted:
                        os.chmod(extracted[0], 0o755)
                        self.tools[tool] = extracted[0]
                    else:
                        self.tools[tool] = tool_path
                else:
                    os.chmod(tool_path, 0o755)
                    self.tools[tool] = tool_path
                results[tool] = True
                self.log(f"  + {tool} installed ({self.tools[tool]})")
                continue

            sdk = self._find_sdk_tool(tool)
            if sdk:
                self.tools[tool] = sdk
                results[tool] = True
                self.log(f"  + {tool} found in SDK ({sdk})")
            else:
                results[tool] = False
                self.log(f"  - {tool} not found (needs the Android SDK build-tools)")

        return results

    @staticmethod
    def _extract_zip(archive_path: str, dest_dir: str) -> None:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(dest_dir)
        except (OSError, zipfile.BadZipFile) as exc:  # pragma: no cover
            print(f"[LOG] could not extract {archive_path}: {exc}")

    # -- decompile ---------------------------------------------------------
    def decompile_apk(self, apk_path: str) -> bool:
        """Decompile APK to workspace (smali *and* resources)."""
        self.set_status("decompiling", "Extracting APK resources...")
        self.log(f"Decompiling: {apk_path}")

        if not os.path.isfile(apk_path):
            self.log(f"Error: no such file: {apk_path}")
            return False

        output_dir = self.decompiled_dir
        if not self.tools.get("apktool"):
            self.log("Error: apktool not found")
            return False

        # No -r and no -s: we need both res/ and smali/.
        cmd = [
            "java", "-jar", self.tools["apktool"],
            "d", "-f",
            "-p", os.path.join(self.workspace, "apktool"),
            apk_path, output_dir,
        ]
        self.log(f"Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except FileNotFoundError:
            self.log("Error: java not found on PATH")
            return False
        except subprocess.TimeoutExpired:
            self.log("Error: apktool timed out")
            return False

        if result.returncode != 0:
            self.log(f"apktool error: {(result.stderr or result.stdout).strip()[:2000]}")
            return False

        self.log(f"+ Decompiled to {output_dir}")
        return True

    @property
    def decompiled_dir(self) -> str:
        return os.path.join(self.workspace, "decompiled")

    # -- extract -----------------------------------------------------------
    def extract_strings(self, all_locales: bool = False) -> Dict[str, str]:
        """Extract translatable strings, recording where each one lives."""
        self.set_status("extracting", "Scanning for strings...")
        self.log("Extracting translatable strings...")
        self.sources = []
        self._default_values = None

        strings: Dict[str, str] = {}

        self._tags_by_path: Dict[str, Dict[str, set]] = {}
        xml_files = self._strings_xml_files(all_locales)
        for path in xml_files:
            tags: Dict[str, set] = {}
            for key, value in self._parse_xml_strings(path, tags_out=tags).items():
                strings[key] = value
                self.sources.append(StringSource(value=value, kind="xml", path=path, key=key))
            self._tags_by_path[path] = tags

        smali_dir = os.path.join(self.decompiled_dir, "smali*")
        for smali_path in sorted(glob.glob(smali_dir)):
            if os.path.isdir(smali_path):
                for value in self._extract_from_smali(smali_path):
                    key = "smali_" + hashlib.md5(value.encode("utf-8")).hexdigest()[:8]
                    strings.setdefault(key, value)
                    self.sources.append(StringSource(value=value, kind="smali", path=smali_path))

        # Snapshot the default strings.xml now: by the time patch_strings() runs,
        # it has already been rewritten, and reading it then would compare
        # already-translated values against original mapping keys.
        self._default_resource_values()

        self._enqueue_sources()
        self.log(f"+ Found {len(strings)} strings ({len(self.sources)} occurrences)")
        return strings

    def _strings_xml_files(self, all_locales: bool) -> List[str]:
        res_dir = os.path.join(self.decompiled_dir, "res")
        if not os.path.isdir(res_dir):
            return []

        if all_locales:
            candidates = glob.glob(os.path.join(res_dir, "values*", "strings*.xml"))
        else:
            default = os.path.join(res_dir, "values", "strings.xml")
            candidates = [default] if os.path.exists(default) else []
        return sorted(candidates)

    def _enqueue_sources(self) -> None:
        seen: set = set()
        for source in self.sources:
            if source.value in seen:
                continue
            seen.add(source.value)
            if should_translate(source.value, self.target_lang)[0]:
                self.queue.add(TranslationJob(key=source.value, original=source.value))

    def _parse_xml_strings(self, xml_path: str, tags_out: Optional[Dict[str, set]] = None) -> Dict[str, str]:
        """Extract plain ``<string>`` resources, keeping inner markup verbatim.

        ElementTree is deliberately not used for the value: it mangles
        namespace-prefixed placeholders (``{xliff:g}``), drops CDATA and
        cannot round-trip inline formatting. A regex keeps the resource body
        exactly as apktool emitted it, so ``<b>`` stays a tag and ``%s`` stays a
        format specifier.
        """
        strings: Dict[str, str] = {}
        try:
            with open(xml_path, "r", encoding="utf-8", errors="surrogateescape") as handle:
                text = handle.read()
        except OSError as exc:
            self.log(f"Error reading {xml_path}: {exc}")
            return strings

        for match in _STRING_RE.finditer(text):
            key = match.group("name")
            body = match.group("body") or ""
            if key and body.strip():
                if tags_out is not None:
                    # Tags come from the raw body: after entity decoding,
                    # escaped text such as &lt;hi&gt; would look like a tag.
                    tags_out[key] = {_tag_name(tag) for tag in _TAG_RE.findall(body)}
                strings[key] = _decode_resource_body(body).strip()
        return strings

    def _resources_stub(self) -> str:
        """An empty resource file that carries the default file's namespaces."""
        default = os.path.join(self.decompiled_dir, "res", "values", "strings.xml")
        header = "<resources>"
        if os.path.isfile(default):
            try:
                with open(default, "r", encoding="utf-8", errors="surrogateescape") as handle:
                    match = _RESOURCES_OPEN_RE.search(handle.read())
                if match:
                    header = match.group(0).rstrip(">").rstrip() + ">"
            except OSError:  # pragma: no cover
                pass
        return f'<?xml version="1.0" encoding="utf-8"?>\n{header}\n</resources>\n'

    def _tags_for(self, path: str, key: str) -> set:
        """Tags that were literally present in the original resource body."""
        return self._tags_by_path.get(path, {}).get(key, set())

    @staticmethod
    def _tags_in(fragment: str) -> set:
        """Every tag name appearing in an XML fragment."""
        return {_tag_name(tag) for tag in _TAG_RE.findall(fragment or "")}

    def _extract_from_smali(self, smali_dir: str) -> List[str]:
        """Distinct, plausibly translatable literals from a smali tree."""
        found: List[str] = []
        seen: set = set()
        length_re = re.compile(r"^L[A-Za-z0-9_/$]+;$")

        for root, _dirs, files in os.walk(smali_dir):
            for filename in files:
                if not filename.endswith(".smali"):
                    continue
                path = os.path.join(root, filename)
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                        content = handle.read()
                except OSError as exc:  # pragma: no cover
                    self.log(f"Error reading {path}: {exc}")
                    continue

                for _start, _end, decoded in iter_smali_literals(content):
                    if not (2 < len(decoded) < 500):
                        continue
                    if length_re.match(decoded):  # class descriptors
                        continue
                    if decoded in seen:
                        continue
                    if not should_translate(decoded, self.target_lang)[0]:
                        continue
                    seen.add(decoded)
                    found.append(decoded)
        return found

    # -- translate ---------------------------------------------------------
    def _ensure_engine(self) -> TranslatorEngine:
        if self.engine is None:
            provider_name = "google-v2" if self.api_key else "google-web"
            self.engine = TranslatorEngine(
                cache_file=os.path.join(self.output_dir, "translation_cache.json"),
                source=self.source_lang,
                target=self.target_lang,
                provider_name=provider_name,
                max_retries=self.max_retries,
                api_key=self.api_key or None,
            )
        return self.engine

    def translate_strings(self, strings: Dict[str, str], source_lang: Optional[str] = None) -> Dict[str, str]:
        """Translate every distinct string exactly once."""
        if source_lang:
            self.source_lang = source_lang
        self.set_status("translating", f"Translating {len(strings)} strings...")
        self.log(f"Starting translation: {len(strings)} strings -> {self.target_lang}")

        engine = self._ensure_engine()
        values = list(dict.fromkeys(strings.values()))
        self.log(f"{len(values)} distinct values to translate")

        def work(value: str) -> None:
            self.queue.mark_start(value)
            result = engine.translate_detailed(value)
            if result.status == "done":
                self.queue.mark_done(value, result.translated)
            elif result.status == "skipped":
                self.queue.mark_skipped(value, result.skipped_reason)
            else:
                self.queue.mark_done(value, value, error=result.error)

        iterator = values
        if HAS_TQDM:
            iterator = tqdm(values, desc="Translating", unit="str")
        if self.workers > 1 and len(values) > 1:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                list(pool.map(work, iterator))
        else:
            for value in iterator:
                work(value)

        self.translations = {
            job.original: job.translated
            for job in self.queue.items
            if job.status == "done" and job.translated and job.translated != job.original
        }
        self.log(f"+ Translated {len(self.translations)} strings")
        return {key: self.translations.get(value, value) for key, value in strings.items()}

    def _call_translate_api(self, text: str, source: str, target: str) -> str:
        """Translate one string through the configured provider."""
        engine = self._ensure_engine()
        return engine.translate(text)

    # -- patch -------------------------------------------------------------
    def patch_strings(self, original_strings: Dict[str, str], translated: Dict[str, str]) -> bool:
        """Write translations back into the decompiled tree.

        Rules that matter for a working APK:

        * the *default* ``res/values/strings.xml`` is rewritten, so a device with
          no matching locale folder shows the new language;
        * a ``res/values-<target>/`` file is created or rewritten, so the target
          locale resolves correctly;
        * other ``values-<other>/`` folders are left completely alone — the old
          code overwrote them and destroyed other languages' translations;
        * smali literals are patched in place.
        """
        self.set_status("patching", "Writing translated strings...")
        self.log("Patching translated strings...")

        mapping: Dict[str, str] = dict(self.translations)
        for original, value in (translated or {}).items():
            if original != value:
                mapping[original] = value

        if not mapping:
            self.log("Nothing to patch (no translations were produced)")
            return True

        by_path: Dict[str, List[StringSource]] = {}
        for source in self.sources:
            if source.kind == "xml" and source.value in mapping:
                by_path.setdefault(source.path, []).append(source)

        changed = 0
        for path, occurrences in by_path.items():
            if self._write_xml_strings(path, mapping, occurrences):
                changed += 1

        target_dir = os.path.join(self.decompiled_dir, "res", f"values-{self.target_lang}")
        target_file = os.path.join(target_dir, "strings.xml")
        if self._write_xml_strings(target_file, mapping, None, create_parents=True):
            changed += 1
            self.log(f"  + wrote {target_file}")

        smali_changed = self._patch_smali(mapping)
        self.log(f"+ Patched {changed} resource files, {smali_changed} smali literals")
        return True

    def _write_xml_strings(
        self,
        xml_path: str,
        mapping: Dict[str, str],
        occurrences: Optional[List[StringSource]],
        create_parents: bool = False,
    ) -> bool:
        """Rewrite an Android strings.xml with translations applied.

        The file is edited as text, not round-tripped through ElementTree, so
        comments, attribute order and the resource header all survive.
        """
        if not os.path.exists(xml_path):
            if not create_parents:
                return False
            os.makedirs(os.path.dirname(xml_path), exist_ok=True)
            with open(xml_path, "w", encoding="utf-8") as handle:
                # Reuse the default file's <resources ...> opening tag: a
                # translated value may contain a namespaced placeholder such as
                # <xliff:g>, and an undeclared prefix would not compile.
                handle.write(self._resources_stub())
        elif not os.path.isfile(xml_path):  # pragma: no cover
            return False

        try:
            with open(xml_path, "r", encoding="utf-8", errors="surrogateescape") as handle:
                original_text = handle.read()
        except OSError as exc:
            self.log(f"Error reading {xml_path}: {exc}")
            return False

        wanted = {source.key: mapping[source.value] for source in (occurrences or []) if source.key}
        original_tags = {
            source.key: self._tags_for(source.path, source.key)
            for source in (occurrences or [])
            if source.key
        }
        if not wanted and os.path.exists(xml_path):
            # Target-locale file: use every known key that exists in the default.
            snapshot = self._default_resource_values()
            for key, value in snapshot.items():
                if value in mapping:
                    wanted[key] = mapping[value]
                    original_tags[key] = self._tags_by_path.get(
                        os.path.join(self.decompiled_dir, "res", "values", "strings.xml"), {}
                    ).get(key, set())
            if not wanted:
                return False

        text = original_text
        applied = 0
        for key, value in wanted.items():
            updated = self._replace_resource_value(text, key, value, keep_tags=original_tags.get(key))
            if updated == text:
                # The key does not exist in this file yet (a freshly created
                # values-<lang>/strings.xml starts out empty).
                updated = self._insert_resource_value(text, key, value, keep_tags=original_tags.get(key))
                if updated == text:
                    continue
            text = updated
            applied += 1

        if applied == 0 or text == original_text:
            return False

        with open(xml_path, "w", encoding="utf-8", errors="surrogateescape") as handle:
            handle.write(text)
        return True

    def _default_resource_values(self) -> Dict[str, str]:
        """Snapshot of the default ``strings.xml`` taken *before* patching.

        Reading it after patching would return already-translated values, which
        no longer match the mapping keys — that is what used to leave the
        ``values-<lang>/strings.xml`` file empty.
        """
        if self._default_values is None:
            default = os.path.join(self.decompiled_dir, "res", "values", "strings.xml")
            self._default_values = self._parse_xml_strings(default) if os.path.exists(default) else {}
        return self._default_values

    @staticmethod
    def _insert_resource_value(text: str, key: str, value: str, keep_tags: Optional[set] = None) -> str:
        """Add a ``<string>`` element to a resource file that lacks it."""
        if "</resources>" not in text:
            return text
        body = _encode_resource_body(value, allowed_tags=keep_tags)
        element = f'    <string name="{key}">{body}</string>\n'
        index = text.rindex("</resources>")
        return text[:index] + element + text[index:]

    @staticmethod
    def _replace_resource_value(text: str, key: str, value: str, keep_tags: Optional[set] = None) -> str:
        """Replace one ``<string name="key">`` body, keeping the surrounding XML.

        Inline markup that was present in the original value (``<b>``, ``<xliff:g>``)
        is preserved as a tag; everything else is XML-escaped, so a translation
        containing ``&`` or ``<`` cannot produce a broken resource file.
        """
        pattern = re.compile(
            r'(<string\s+name="' + re.escape(key) + r'"\s*(?P<attrs>[^>]*?)>)(?P<body>.*?)(</string>)',
            re.DOTALL,
        )
        match = pattern.search(text)
        if match is None:
            return text

        body = _encode_resource_body(value, allowed_tags=keep_tags)
        replacement = f"{match.group(1)}{body}{match.group(4)}"
        return text[: match.start()] + replacement + text[match.end():]

    def _patch_smali(self, mapping: Dict[str, str]) -> int:
        """Patch smali literals under the decompiled tree."""
        total = 0
        for smali_dir in sorted(glob.glob(os.path.join(self.decompiled_dir, "smali*"))):
            if not os.path.isdir(smali_dir):
                continue
            for root, _dirs, files in os.walk(smali_dir):
                for filename in files:
                    if not filename.endswith(".smali"):
                        continue
                    path = os.path.join(root, filename)
                    try:
                        with open(path, "r", encoding="utf-8", errors="surrogateescape") as handle:
                            content = handle.read()
                    except OSError:  # pragma: no cover
                        continue
                    new_content, replaced, _unmatched = patch_smali_literals(content, mapping)
                    if replaced and new_content != content:
                        with open(path, "w", encoding="utf-8", errors="surrogateescape") as handle:
                            handle.write(new_content)
                        total += replaced
        return total

    # -- rebuild -----------------------------------------------------------
    def rebuild_apk(self, input_apk: Optional[str] = None) -> str:
        """Rebuild the APK from the decompiled tree.

        ``input_apk`` is optional and only used for the output file name.
        """
        self.set_status("rebuilding", "Rebuilding APK...")
        self.log("Rebuilding APK...")

        if not self.tools.get("apktool"):
            self.log("Error: apktool not found, cannot rebuild")
            return ""

        os.makedirs(self.output_dir, exist_ok=True)
        stem = "patched"
        if input_apk:
            stem = f"{Path(input_apk).stem}_{self.target_lang}"
        output_apk = os.path.join(self.output_dir, f"{stem}.apk")

        cmd = ["java", "-jar", self.tools["apktool"], "b", self.decompiled_dir, "-o", output_apk]
        self.log(f"Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except FileNotFoundError:
            self.log("Error: java not found on PATH")
            return ""
        except subprocess.TimeoutExpired:
            self.log("Error: rebuild timed out")
            return ""

        if result.returncode != 0:
            self.log(f"Build error: {(result.stderr or result.stdout).strip()[:2000]}")
            return ""
        if not os.path.isfile(output_apk):
            self.log("Build reported success but produced no APK")
            return ""

        self.log(f"+ Rebuilt APK: {output_apk}")
        return output_apk

    # -- sign --------------------------------------------------------------
    def _create_debug_keystore(self, keystore_path: str) -> bool:
        """Provide a usable debug keystore. Returns success."""
        candidates = []
        for root in (os.environ.get("ANDROID_SDK_ROOT"), os.environ.get("ANDROID_SDK"), os.environ.get("ANDROID_HOME")):
            if root:
                candidates.append(os.path.join(root, "debug.keystore"))
        candidates.append(os.path.expanduser("~/.android/debug.keystore"))

        for candidate in candidates:
            if os.path.isfile(candidate):
                shutil.copy2(candidate, keystore_path)
                self.alias = "androiddebugkey"  # the alias inside that keystore
                self.log(f"+ Copied debug keystore from {candidate}")
                return True

        cmd = [
            "keytool", "-genkeypair",
            "-keystore", keystore_path,
            "-storepass", self.keystore_pass,
            "-keypass", self.key_pass,
            "-keyalg", "RSA",
            "-keysize", "2048",
            "-validity", "10000",
            "-alias", self.alias,
            "-dname", "CN=Android Debug,O=Android,C=US",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self.log(f"! keytool unavailable: {exc}")
            return False

        if result.returncode == 0:
            self.log(f"+ Generated debug keystore at {keystore_path}")
            return True
        self.log(f"! Keystore generation failed: {(result.stderr or '').strip()[:500]}")
        return False

    def sign_apk(self, apk_path: str) -> str:
        """Zipalign then sign the rebuilt APK."""
        self.set_status("signing", "Signing APK...")
        self.log(f"Signing: {apk_path}")

        if not os.path.isfile(apk_path):
            self.log(f"Error: no such file: {apk_path}")
            return ""

        os.makedirs(self.output_dir, exist_ok=True)
        keystore_path = os.path.join(self.output_dir, "debug.keystore")
        if not os.path.exists(keystore_path) and not self._create_debug_keystore(keystore_path):
            return ""
        self.keystore_path = keystore_path

        working = apk_path

        # 1. zipalign first — alignment must happen before signing.
        if self.tools.get("zipalign"):
            aligned = os.path.join(self.output_dir, "patched_aligned.apk")
            cmd = [self.tools["zipalign"], "-f", "-p", "4", apk_path, aligned]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode == 0 and os.path.isfile(aligned):
                    working = aligned
                    self.log("+ zipalign ok")
                else:
                    self.log(f"! zipalign failed, continuing unaligned: {(result.stderr or '').strip()[:300]}")
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                self.log(f"! zipalign unavailable: {exc}")

        # 2. apksigner (v1+v2+v3).
        if self.tools.get("apksigner"):
            signed = os.path.join(self.output_dir, "patched_signed.apk")
            cmd = [
                self.tools["apksigner"], "sign",
                "--ks", keystore_path,
                "--ks-pass", f"pass:{self.keystore_pass}",
                "--key-pass", f"pass:{self.key_pass}",
                "--ks-key-alias", self.alias,
                "--v1-signing-enabled", "true",
                "--v2-signing-enabled", "true",
                "--v3-signing-enabled", "true",
                "--out", signed,
                working,
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode == 0 and os.path.isfile(signed):
                    self.log("+ APK signed successfully")
                    return signed
                self.log(f"! apksigner failed: {(result.stderr or result.stdout).strip()[:500]}")
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                self.log(f"! apksigner unavailable: {exc}")

        # 3. jarsigner fallback — v1 only, and SHA1 is rejected by modern Android.
        signed_jarsigner = os.path.join(self.output_dir, "patched_signed_v1.apk")
        shutil.copy2(working, signed_jarsigner)
        cmd = [
            "jarsigner",
            "-sigalg", "SHA256withRSA",
            "-digestalg", "SHA-256",
            "-keystore", keystore_path,
            "-storepass", self.keystore_pass,
            "-keypass", self.key_pass,
            signed_jarsigner, self.alias,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self.log(f"! jarsigner unavailable: {exc}")
            return ""

        if result.returncode == 0:
            self.log("+ APK signed (jarsigner, v1 only)")
            return signed_jarsigner

        self.log(f"! Sign error: {(result.stderr or result.stdout).strip()[:500]}")
        return ""

    # -- housekeeping ------------------------------------------------------
    def clean_workspace(self) -> None:
        """Clean up workspace"""
        if os.path.exists(self.workspace):
            shutil.rmtree(self.workspace, ignore_errors=True)
            self.log("+ Cleaned workspace")

    def write_report(self, path: Optional[str] = None) -> str:
        """Write ``found_strings.txt`` — every string we found and what became of it."""
        path = path or os.path.join(self.output_dir, "found_strings.txt")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        stats = self.queue.get_stats()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"# KuroPatch: {stats['total']} translatable strings\n")
            handle.write(f"# done={stats['done']} cached-or-skipped={stats['skipped']} "
                         f"error={stats['error']} -> {self.target_lang}\n\n")
            for job in self.queue.items:
                result = self.translations.get(job.original, "")
                suffix = f" => {result}" if result else ""
                note = f" [{job.error}]" if job.error else ""
                handle.write(f"{job.original}{suffix}{note}\n")
        return path

    def get_queue_stats(self) -> Dict:
        """Get current queue statistics"""
        return self.queue.get_stats()

    def run_full_pipeline(self, apk_path: str, output_dir: Optional[str] = None) -> Dict:
        """Run complete translation pipeline."""
        if output_dir:
            self.output_dir = output_dir

        results: Dict = {
            "success": False,
            "output_apk": "",
            "stats": {},
            "errors": [],
            "report": "",
        }

        try:
            # Step 1: Decompile
            if not self.decompile_apk(apk_path):
                results["errors"].append("Failed to decompile APK")
                return results

            # Step 2: Extract
            original_strings = self.extract_strings()
            if not original_strings:
                results["errors"].append("No translatable strings found")
                return results

            # Step 3: Translate
            translated = self.translate_strings(original_strings)

            # Step 4: Patch
            if not self.patch_strings(original_strings, translated):
                results["errors"].append("Failed to patch strings")
                return results

            try:
                report = self.write_report()
                results["report"] = report
                self.log(f"+ Report: {report}")
            except OSError as exc:  # pragma: no cover
                self.log(f"! Could not write report: {exc}")

            # Step 5: Rebuild
            rebuilt = self.rebuild_apk(apk_path)
            if not rebuilt:
                results["errors"].append("Failed to rebuild APK")
                return results

            # Step 6: Sign
            signed = self.sign_apk(rebuilt)
            if not signed:
                results["errors"].append("Failed to sign APK")
                return results

            results["success"] = True
            results["output_apk"] = signed
            results["stats"] = self.get_queue_stats()
            self.set_status("done", signed)
            self.log(f"+ Pipeline complete: {signed}")
            return results

        except KeyboardInterrupt:
            results["errors"].append("Interrupted")
            raise
        except Exception as exc:  # noqa: BLE001 - top level boundary
            results["errors"].append(f"{type(exc).__name__}: {exc}")
            self.log(f"Pipeline error: {type(exc).__name__}: {exc}")
            return results
        finally:
            if self.engine is not None:
                self.engine.close()
            if not self.keep_workspace:
                self.clean_workspace()

    def __enter__(self) -> "GameTranslator":
        return self

    def __exit__(self, *_exc) -> None:
        if self.engine is not None:
            self.engine.close()
        if not self.keep_workspace:
            self.clean_workspace()
