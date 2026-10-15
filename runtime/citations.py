import re

_CITES = re.compile(r"^cites: ([A-Za-z0-9][A-Za-z0-9_-]*(?: [A-Za-z0-9][A-Za-z0-9_-]*)*)\s*$")


def cites_line(ids: tuple[str, ...]) -> str:
    return "cites: " + " ".join(ids)


def parse_cites(artifact: str) -> tuple[str, ...] | None:
    lines = (artifact or "").rstrip().splitlines()
    if not lines:
        return None
    found = _CITES.match(lines[-1])
    if found is None:
        return None
    return tuple(found.group(1).split(" "))
