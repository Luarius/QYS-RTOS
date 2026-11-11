"""Read only verifiable source overlays from a Zephyr build cache."""

from pathlib import Path


def active_project_overlays(cache_file: str | Path, project_root: str | Path) -> list[Path]:
    """Return existing in-project overlays explicitly recorded in the cache.

    Ambiguous or missing values are ignored; callers can still show a snippet
    for the user to copy into the correct source file manually.
    """
    try:
        root = Path(project_root).resolve(strict=True)
        lines = Path(cache_file).read_text(encoding="utf-8").splitlines()
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return []

    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        name, colon, _kind = key.partition(":")
        if separator and colon and name in {"DTC_OVERLAY_FILE", "EXTRA_DTC_OVERLAY_FILE"}:
            values[name] = value.strip()

    def resolve_candidate(value: str) -> Path | None:
        value = value.strip().strip('"')
        if not value or value.endswith("-NOTFOUND"):
            return None
        try:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            candidate = candidate.resolve(strict=True)
            candidate.relative_to(root)
            if candidate.is_file() and candidate.suffix.casefold() == ".overlay":
                return candidate
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        return None

    result: list[Path] = []
    for name in ("DTC_OVERLAY_FILE", "EXTRA_DTC_OVERLAY_FILE"):
        value = values.get(name, "")
        if not value:
            continue
        exact = resolve_candidate(value)
        candidates = [exact] if exact else [resolve_candidate(part) for part in value.split(";")]
        for candidate in candidates:
            if candidate is not None and candidate not in result:
                result.append(candidate)
    return result
