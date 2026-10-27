"""SPEC section parsing and validation."""

from pathlib import Path
import re
import shlex
import sys
import hermes_delegate_common as common


SPEC_SECTIONS = {
    "External contracts": ("external contracts", "외부 계약"),
    "Failure paths": ("failure paths", "실패 경로"),
    "Acceptance": ("acceptance", "완료 검증"),
}


SPEC_TOPICS = {
    "ordering": ("order", "concurren", "순서", "동시성"),
    "interruption": ("interrupt", "중단"),
    "rejection": ("reject", "partial", "거절", "부분 실패"),
    "repeat": ("repeat", "retry", "반복", "재시도"),
    "external": ("external", "외부"),
}


SPEC_VAGUE = ("1건 이상", "한 건 이상", "하나 이상", "1개 이상", "최소 1", "at least one",
              "at least 1", "one or more", "1 or more", ">= 1", ">=1", "≥ 1", "≥1", "etc.",
              "=> 1", "1+", "non-empty", "nonempty", "비어 있지 않", "적어도 하나", "적어도 1",
              "최소 한 개", "최소 한 건", "최소 하나", "1 이상", "한 개 이상", "한 번 이상", "1번 이상",
              "1회 이상", "한 회 이상", "not empty")


SPEC_NONZERO = ("non-zero", "nonzero", "0이 아님", "> 0", ">0", "!= 0", "≠ 0", "greater than 0",
                "greater than zero", "more than 0", "more than zero", "0보다 크", "0보다 큼", "0보다 커", "0보다 많", "0 초과",
                "0건 초과", "0개 초과")


SPEC_EXIT_BEFORE = re.compile(r"(?:\b(?:exit(?:s|ed)?|exit\s+(?:code|status)|status|rc|return\s*code|returncode)"
                              r"|종료\s*코드|종료)\s*(?:is|was|=|:|가|는|이)?\s*$", re.IGNORECASE)


SPEC_HOST = re.compile(r"localhost|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9A-Fa-f:.]+\]", re.IGNORECASE)


def spec_phrase_re(phrase):
    body = r" ?".join(re.escape(part) for part in phrase.split(" "))
    before = r"(?<![0-9A-Za-z.])" if phrase[0].isalnum() else r"(?<![0-9])"
    return re.compile(before + body + r"(?![0-9]|\.[0-9])", re.IGNORECASE)


SPEC_VAGUE_RE = tuple((phrase, spec_phrase_re(phrase)) for phrase in SPEC_VAGUE)


SPEC_NONZERO_RE = tuple((phrase, spec_phrase_re(phrase)) for phrase in SPEC_NONZERO)


def spec_vague_phrases(item):
    item = re.sub(r"\s+", " ", re.sub(r"[`*_~]+", "", item))
    found = [phrase for phrase, pattern in SPEC_VAGUE_RE if pattern.search(item)]
    for phrase, pattern in SPEC_NONZERO_RE:
        if any(not SPEC_EXIT_BEFORE.search(item[:match.start()]) for match in pattern.finditer(item)):
            found.append(phrase)
    return found


SPEC_TEMPLATE = (
    "## External contracts\n"
    "- `<path>:<line>` — <observed contract and captured output>\n\n"
    "## Failure paths\n"
    "- Ordering/concurrency: <behavior and verification>\n"
    "- Interruption: <behavior and verification>\n"
    "- Rejection/partial failure: <behavior and verification>\n"
    "- Repeat/retry: <behavior and verification>\n"
    "- External: <behavior and verification>\n\n"
    "## Acceptance\n"
    "- <each expected result and its exact verification>\n"
)


def spec_na(text):
    match = re.fullmatch(r"(?:N/A|해당 없음)[:：]\s*(.*)", text.strip(), re.IGNORECASE | re.DOTALL)
    return bool(match and len(re.sub(r"\s", "", match[1])) >= 8)


def spec_sections(text):
    sections = {name: [] for name in SPEC_SECTIONS}
    current = None
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            if current is not None:
                current.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not line[marker.end():].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
        heading = re.match(r"^(#{1,2}) +(.*)$", line)
        if heading:
            current = None
            if heading[1] == "##":
                for name, aliases in SPEC_SECTIONS.items():
                    if heading[2].strip().lower() in aliases:
                        current = []
                        sections[name].append(current)
                        break
        elif current is not None:
            current.append(line)
    return sections


def spec_items(body):
    items = []
    fence = None
    active = False
    for line in body:
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        item = re.match(r"^(?:[-*]|\d+[.)]) (.*)$", line) if not fence else None
        if item:
            items.append(item[1])
            active = True
        elif re.match(r"^#{1,6}\s", line) and not fence:
            active = False
            continue
        elif active:
            items[-1] += "\n" + line
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not line[marker.end():].strip():
                fence = None
    items = [item.strip() for item in items]
    return items


def valid_reference(candidates, match):
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            with candidate.open("rb") as stream:
                if match:
                    count = sum(1 for _ in stream)
                    start, end = int(match[2]), int(match[3] or match[2])
                    valid = 1 <= start <= end <= count
                else:
                    stream.read(1)
                    valid = True
        except (OSError, ValueError):
            continue
        if valid:
            return True
    return False


def contract_references(item, workdir, spec_dir, problem):
    resolved = False
    for span in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", item):
        match = re.fullmatch(r"(.+):(\d+)(?:-(\d+))?", span)
        try:
            path = Path(match[1] if match else span).expanduser()
        except (RuntimeError, ValueError):
            problem("External contracts", f"invalid file reference {span!r}", item)
            continue
        candidates = [path] if path.is_absolute() else [Path(workdir) / path, Path(spec_dir) / path]
        if match and SPEC_HOST.fullmatch(match[1]) and not any(
                candidate.is_file() for candidate in candidates):
            continue
        valid = valid_reference(candidates, match)
        resolved |= valid
        if match and not valid:
            problem("External contracts", f"invalid file/line reference {span!r}", item)
    if not resolved:
        problem("External contracts", "no resolving file reference", item)


def contract_items(items, workdir, spec_dir, problem):
    for item in items:
        if spec_na(item):
            if len(items) != 1:
                problem("External contracts", "N/A must be the only item", item)
            continue
        contract_references(item, workdir, spec_dir, problem)


def failure_items(items, problem):
    covered = set()
    for item in items:
        match = re.match(r"^([^:：]+)[:：]\s*(.*)$", item, re.DOTALL)
        if not match:
            problem("Failure paths", "expected label: answer", item)
            continue
        label, answer = match[1].lower(), match[2]
        for topic, aliases in SPEC_TOPICS.items():
            if any(alias in label for alias in aliases):
                covered.add(topic)
                if not spec_na(answer) and len(re.sub(r"\s", "", answer)) < 15:
                    problem("Failure paths", f"too-short {topic} answer", item)
    for topic in SPEC_TOPICS:
        if topic not in covered:
            problem("Failure paths", f"missing {topic} topic")


def acceptance_items(items, problem):
    for item in items:
        if re.match(r"^(?:N/A|해당 없음)[:：]", item, re.IGNORECASE):
            problem("Acceptance", "N/A is not allowed", item)
        for phrase in spec_vague_phrases(item):
            problem("Acceptance", f"vague phrase {phrase!r}", item)


def spec_problems(text, kind, workdir, spec_dir):
    sections = spec_sections(text)
    problems = []

    def problem(section, reason, item=None):
        excerpt = f" {item[:60]!r}" if item is not None else ""
        problems.append(f"{section}{excerpt}: {reason}")

    required = ("Acceptance",) if kind == "qa" else tuple(SPEC_SECTIONS)
    for section in required:
        bodies = sections[section]
        if not bodies:
            problem(section, "missing section")
        if len(bodies) > 1:
            problem(section, "duplicated section")
        for body in bodies:
            items = spec_items(body)
            if not items:
                problem(section, "no items")
            if section == "External contracts":
                contract_items(items, workdir, spec_dir, problem)
            elif section == "Failure paths":
                failure_items(items, problem)
            else:
                acceptance_items(items, problem)
    return problems


def read_spec_gate(spec, kind, workdir):
    try:
        data = Path(spec).read_bytes()
    except OSError as exc:
        return b"", [f"SPEC: cannot read file: {exc}"]
    problems = []
    if not 0 < len(data) <= 1024 * 1024:
        problems.append("SPEC: must be non-empty and at most 1 MiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        problems.append("SPEC: not UTF-8")
        return data, problems
    problems.extend(spec_problems(text, kind, workdir, Path(spec).parent))
    return data, problems


def require_spec_gate(spec, kind, workdir):
    data, problems = read_spec_gate(spec, kind, workdir)
    if problems:
        details = "; ".join(f"({i}) {problem}" for i, problem in enumerate(problems, 1))
        raise common.Fail(f"SPEC gate refused {spec}: {len(problems)} problem(s): {details}; check with: hermes-delegate spec-check "
                   f"--spec {shlex.quote(str(spec))} --workdir {shlex.quote(str(workdir))} --kind {kind}", 2)
    return data


def cmd_spec_check(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = common.Parser(prog="hermes-delegate spec-check", allow_abbrev=False)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec")
    source.add_argument("--template", action="store_true")
    parser.add_argument("--workdir")
    parser.add_argument("--kind", choices=("codex", "claude", "qa"), default="codex")
    args = parser.parse_args(argv)
    if args.template:
        out.write(SPEC_TEMPLATE)
        return 0
    spec = Path(args.spec).absolute()
    _, problems = read_spec_gate(spec, args.kind, args.workdir or spec.parent)
    for problem in problems:
        print(problem.replace("\n", " ").replace("\r", " "), file=err)
    if problems:
        return 2
    print("ok", file=out)
    return 0
