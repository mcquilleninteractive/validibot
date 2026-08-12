"""
File safety utilities for upload validation and filename sanitization.

This module provides the core defence-in-depth layer for all file uploads
(submissions, resource files, etc.). It is intentionally dependency-free
so it can be imported from forms, models, and ingest pipelines without
pulling in Django ORM or storage backends.

Typical usage::

    from validibot.core.filesafety import (
        build_safe_filename,
        detect_suspicious_magic,
        sha256_hexdigest,
    )

    safe_name = build_safe_filename(
        user_filename, content_type="application/json",
    )
    if detect_suspicious_magic(first_4k_bytes):
        raise ValidationError("Binary file detected")
    checksum = sha256_hexdigest(file_bytes)
"""

import contextlib
import hashlib
import re
import unicodedata
from pathlib import Path

SAFE_EXT_FOR_TYPE = {
    # keep this aligned with SUPPORTED_CONTENT_TYPES & SubmissionFileType
    "application/json": ".json",
    "application/xml": ".xml",
    "text/plain": ".txt",
    "text/x-idf": ".idf",
    "text/turtle": ".ttl",
    "application/n-triples": ".nt",
    "application/n-quads": ".nq",
    "application/ld+json": ".jsonld",
    "application/rdf+xml": ".rdf",
    "application/yaml": ".yaml",
    "text/yaml": ".yaml",
    "application/pdf": ".pdf",
    "application/octet-stream": ".bin",
}

SUSPICIOUS_MAGIC_PREFIXES = (
    b"PK\x03\x04",  # zip/jar/docx/xlsx
    b"%PDF",  # pdf
    b"\x7fELF",  # elf
    b"MZ",  # windows pe
    b"\xfe\xed\xfa",  # mach-o (32-bit, both endians share first 3 bytes)
    b"\xcf\xfa\xed\xfe",  # mach-o 64-bit little-endian
    b"\xca\xfe\xba\xbe",  # mach-o universal / java class
    b"#!",  # shell scripts (shebang)
)

# allow common filename chars; collapse whitespace; drop control chars
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._\-()+=,@ ]+")

# ASCII control characters fall below 32; DEL (127) should be disallowed as well.
_ASCII_MIN_PRINTABLE = 32
_ASCII_MAX_EXCLUSIVE = 127

# Minimum number of bytes needed for meaningful magic-byte detection.
# The shortest prefix in SUSPICIOUS_MAGIC_PREFIXES is 2 bytes (b"#!", b"MZ").
_MIN_MAGIC_BYTES = 2


def sanitize_filename(candidate: str, *, fallback: str = "document") -> str:
    """
    Return a safe, filesystem-friendly version of a user-supplied filename.

    Applies the following transformations in order:

    1. Extract basename (strip directory components to prevent path traversal)
    2. Normalize Unicode to NFKC form (collapse look-alike characters)
    3. Strip ASCII control characters (< 0x20 and 0x7F), except tab
    4. Replace characters outside the safe set with underscores
    5. Collapse whitespace runs to a single space
    6. Remove leading dots (prevent hidden files / dotfile exploits)
    7. Remove trailing dots (invalid on Windows)
    8. Truncate to 100 characters (leave headroom for extension changes)

    If the result is empty after sanitization, returns ``fallback``.

    Args:
        candidate: The raw filename from the upload
            (e.g. ``request.FILES["file"].name``).
        fallback: Name to use when the candidate sanitizes to empty.

    Returns:
        A sanitized filename safe for filesystem storage.

    Examples::

        >>> sanitize_filename("../../etc/passwd")
        'passwd'
        >>> sanitize_filename(".hidden")
        'hidden'
        >>> sanitize_filename("")  # empty -> fallback
        'document'
    """
    candidate = candidate or fallback
    # basename & normalize unicode
    name = Path(candidate).name
    name = unicodedata.normalize("NFKC", name)

    # strip control chars
    name = "".join(
        ch
        for ch in name
        if _ASCII_MIN_PRINTABLE <= ord(ch) < _ASCII_MAX_EXCLUSIVE or ch in "\t"
    )
    name = _FILENAME_SAFE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        name = fallback

    # avoid dotfiles & trailing dots
    if name.startswith("."):
        name = name.lstrip(".") or fallback
    name = name.rstrip(".")

    # cap length (leave room for extension changes)
    return name[:100]


def force_extension(
    name: str,
    *,
    content_type: str,
    default_ext: str | None = None,
) -> str:
    """
    Replace the file extension to match the declared content type.

    Looks up the expected extension in ``SAFE_EXT_FOR_TYPE``. If the
    current extension doesn't match (case-insensitive), it is replaced.
    This prevents double-extension attacks like ``malware.exe.json``.

    Args:
        name: Filename (already sanitized).
        content_type: MIME type declared by the upload.
        default_ext: Extension to use when content_type is not in the map.
            Defaults to ``".txt"``.

    Returns:
        Filename with the correct extension.

    Examples::

        >>> force_extension("data.txt", content_type="application/json")
        'data.json'
        >>> force_extension("data.json", content_type="application/json")
        'data.json'
    """
    want_ext = SAFE_EXT_FOR_TYPE.get(content_type, default_ext or ".txt")
    path = Path(name)
    ext = path.suffix
    # if ext mismatches, replace it
    if ext.lower() != want_ext.lower():
        name = f"{path.stem}{want_ext}"
    return name


def build_safe_filename(
    original: str,
    *,
    content_type: str,
    fallback: str = "document",
) -> str:
    """
    Sanitize a user-supplied filename and enforce the correct extension.

    This is the main entry point for making uploaded filenames safe for
    storage. It chains ``sanitize_filename`` (path traversal, control chars,
    length) with ``force_extension`` (extension mismatch).

    Args:
        original: Raw filename from the upload.
        content_type: MIME type declared by the upload.
        fallback: Name to use when original sanitizes to empty.

    Returns:
        A safe filename with the correct extension.

    Examples::

        >>> build_safe_filename("../../evil.exe", content_type="application/json")
        'evil.json'
    """
    name = sanitize_filename(original, fallback=fallback)
    return force_extension(name, content_type=content_type)


def detect_suspicious_magic(raw: bytes) -> bool:
    """
    Return True if the first bytes of ``raw`` match a known binary format.

    Checks the file header against ``SUSPICIOUS_MAGIC_PREFIXES`` to catch
    executables, archives, and other binary formats that should not be
    uploaded as text or resource files. This is a fast, shallow check -- not
    a substitute for antivirus scanning, but effective at catching obvious
    misuse.

    Detected formats:
        - ZIP archives (also DOCX/XLSX/JAR)
        - PDF documents
        - ELF binaries (Linux)
        - Windows PE executables
        - Mach-O binaries (macOS)
        - Shell scripts (shebang ``#!``)

    Args:
        raw: The first N bytes of the file (at least 4 bytes recommended).

    Returns:
        True if a suspicious magic prefix was detected.
    """
    if len(raw) < _MIN_MAGIC_BYTES:
        return False
    head = raw[:4]
    return any(head.startswith(prefix) for prefix in SUSPICIOUS_MAGIC_PREFIXES)


def sha256_hexdigest(data: bytes) -> str:
    """
    Return the SHA-256 hex digest of ``data``.

    Used to compute checksums for uploaded files (submissions, resource
    files) for integrity verification and deduplication.

    Args:
        data: Raw bytes to hash.

    Returns:
        Lowercase hex string (64 characters).

    Examples::

        >>> sha256_hexdigest(b"hello")
        '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'
    """
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


# Default chunk size when streaming file content into a hash. 64 KiB
# strikes a balance between syscall overhead (smaller chunks = more
# read calls) and memory pressure (larger chunks = bigger transient
# buffer). Most validator resource files are sub-megabyte; weather
# files (EPW) are a few MB; this size processes either in tens of
# chunks.
_HASH_CHUNK_SIZE = 64 * 1024


def sha256_field_file(field_file) -> str:
    """Compute the SHA-256 hex digest of a Django ``FieldFile``'s contents.

    Streams the file in chunks rather than reading the whole thing
    into memory — important for the 10+ MiB resource files that
    EnergyPlus weather data and FMU bundles can produce.

    The function is content-only: file metadata (storage path,
    upload timestamp) does not contribute to the hash, so re-saving
    the same bytes at a different storage path produces the same
    digest.

    Important: this function does NOT close the file. ``FieldFile``
    objects passed during ``Model.save()`` may be ``UploadedFile``
    instances backed by an in-memory buffer that becomes unusable
    once closed — closing here would leave Django's later
    ``storage.save()`` reading from a dead handle. Instead, we
    save the file's current position, read from the start, and
    restore the position so the caller's subsequent operations see
    the file in the same state we found it.

    Args:
        field_file: Any Django ``FieldFile``-like object. An
            empty/unset FieldFile produces the SHA-256 of empty bytes.

    Returns:
        Lowercase hex string (64 characters).
    """
    if not field_file or not getattr(field_file, "name", ""):
        return sha256_hexdigest(b"")

    # Save current position to restore after hashing. Some file-like
    # objects (older custom storages, unseekable streams) don't
    # support tell(); we tolerate that by setting pos=None.
    try:
        pos = field_file.tell()
    except (AttributeError, OSError, ValueError):
        pos = None

    # Couldn't rewind — the hash will reflect whatever bytes the
    # current position yields. Better than crashing.
    with contextlib.suppress(AttributeError, OSError, ValueError):
        field_file.seek(0)

    h = hashlib.sha256()
    while True:
        chunk = field_file.read(_HASH_CHUNK_SIZE)
        if not chunk:
            break
        h.update(chunk)

    if pos is not None:
        try:
            field_file.seek(pos)
        except (AttributeError, OSError, ValueError):
            # Best-effort: try seeking back to zero so the next
            # read() doesn't return EOF unexpectedly.
            with contextlib.suppress(AttributeError, OSError, ValueError):
                field_file.seek(0)

    return h.hexdigest()
