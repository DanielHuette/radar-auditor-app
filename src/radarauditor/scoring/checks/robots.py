import re
import string
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

GOOGLEBOT = "Googlebot"
BINGBOT = "Bingbot"
GOOGLE_EXTENDED = "Google-Extended"
OAI_SEARCHBOT = "OAI-SearchBot"
CLAUDE_SEARCHBOT = "Claude-SearchBot"
CLAUDE_USER = "Claude-User"
WILDCARD = "*"
VALUE_DIRECTIVES = frozenset(
    {"unavailable_after", "max-snippet", "max-image-preview", "max-video-preview"}
)
AGENT_DIRECTIVE = re.compile(r"^([a-z0-9_-]+)\s*:\s*(.*)$")
BYTE_ORDER_MARK = "\ufeff"
PRODUCT_TOKEN = re.compile(r"[A-Za-z_-]+|\*")
PERCENT_OCTET = re.compile(r"%([0-9A-Fa-f]{2})")
UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
PRINTABLE_ASCII = "".join(chr(code) for code in range(0x21, 0x7F))
INDEX_BLOCKING = frozenset({"noindex", "none"})
SNIPPET_BLOCKING = frozenset({"nosnippet", "max-snippet:0"})


@dataclass(frozen=True)
class _Rule:
    allow: bool
    pattern: str


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)
    has_rule_lines: bool = False


def _settle_octet(match: re.Match[str]) -> str:
    character = chr(int(match.group(1), 16))
    return character if character in UNRESERVED else match.group(0).upper()


def _canonical(text: str) -> str:
    return PERCENT_OCTET.sub(_settle_octet, quote(text, safe=PRINTABLE_ASCII))


def _product_token(value: str) -> str:
    match = PRODUCT_TOKEN.match(value)
    return match.group(0).casefold() if match else value.casefold()


def _pattern_matches(pattern: str, target: str) -> bool:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    expression = ".*".join(re.escape(part) for part in body.split("*"))
    return re.match(f"{expression}{'$' if anchored else ''}", target) is not None


class RobotsRules:
    def __init__(self, text: str | None) -> None:
        self.groups: list[_Group] = []
        current: _Group | None = None
        for raw_line in (text or "").removeprefix(BYTE_ORDER_MARK).splitlines():
            line = raw_line.split("#", 1)[0].strip()
            name, separator, value = line.partition(":")
            if not separator:
                continue
            key, content = name.strip().casefold(), value.strip()
            if key == "user-agent":
                if current is None or current.has_rule_lines:
                    current = _Group()
                    self.groups.append(current)
                current.agents.append(_product_token(content))
            elif key in ("allow", "disallow") and current is not None:
                current.has_rule_lines = True
                if content:
                    current.rules.append(_Rule(key == "allow", _canonical(content)))

    def _rules_for(self, agent: str) -> list[_Rule]:
        token = agent.casefold()
        named = [group for group in self.groups if token in group.agents]
        chosen = named or [group for group in self.groups if WILDCARD in group.agents]
        return [rule for group in chosen for rule in group.rules]

    def allows(self, agent: str, url: str) -> bool:
        parts = urlsplit(url)
        target = _canonical((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))
        best: _Rule | None = None
        for rule in self._rules_for(agent):
            if not _pattern_matches(rule.pattern, target):
                continue
            if best is None or len(rule.pattern) > len(best.pattern):
                best = rule
            elif len(rule.pattern) == len(best.pattern) and rule.allow:
                best = rule
        return best is None or best.allow


def directives_for(value: str | None, agent: str) -> set[str]:
    token = agent.casefold()
    found: set[str] = set()
    for line in (value or "").casefold().splitlines():
        current: str | None = None
        for part in line.split(","):
            directive = part.strip()
            named = AGENT_DIRECTIVE.match(directive)
            if named and named.group(1) not in VALUE_DIRECTIVES:
                current, directive = named.group(1), named.group(2).strip()
            if current in (None, token) and directive:
                found.add(re.sub(r"\s*:\s*", ":", directive))
    return found
