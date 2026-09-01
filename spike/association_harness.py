#!/usr/bin/env python3
"""Read-only offline measurement harness for 1-2 hop Graphiti expansion.

The script reads a historical recall log, selects a fixed varied sample, copies
a generated MATCH/RETURN-only Cypher file to the graph host/container, and
writes deterministic JSON measurements.  It never calls a model or a Neo4j
write clause.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shlex
import statistics
import subprocess
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_HOST = "choi138-ri"
DEFAULT_CONTAINER = "memory-server-neo4j-1"
DEFAULT_LOG = "/home/justin/.hermes/state/recall-log.jsonl"
DEFAULT_GROUP = "mnemos"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FORBIDDEN_CYPHER = re.compile(
    r"\b(?:CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|LOAD\s+CSV|FOREACH)\b",
    re.IGNORECASE,
)
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._/-]{1,}|[가-힣]{2,}")

# Fixed after reviewing the eligible 1-3-edge rows.  The reasons are emitted
# into selected_rows.json so the sampling decision is auditable.
SAMPLE_ROWS: dict[int, str] = {
    4: "claude-lb completion; concise project-status query",
    13: "Discord projects post; distinct channel-management topic",
    18: "claude-lb M1 scope; milestone/planning topic",
    33: "Anthropic Team/API key; account/authentication topic",
    41: "Sharadar subscription; finance/data-vendor topic",
    59: "delete one M1 plan line; narrow document-edit intent",
    92: "completed work/test debt; delegation/status topic",
    94: "approved numbered tasks; memory-plugin deployment topic",
    95: "memory-health watchdog; cron/heartbeat topic",
    96: "Codex test-only delegation; constrained validation topic",
    98: "M3 gate auto-reset; milestone/reproduction topic",
    104: "Graphiti upstream PR; contribution topic",
    105: "what was item 2; continuity query with sparse wording",
    107: "continue approved work; contextual query from another channel",
    111: "short approval continuation; contextual ambiguity stress case",
    115: "very short GD query; minimal-vocabulary stress case",
    120: "Instagram Reels history; social-media history topic",
    121: "persist Reels history; ingestion/design topic",
}

STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "what", "was",
    "were", "how", "into", "via", "use", "using", "message", "session",
    "scope", "recent", "topics", "triggering", "reply", "react", "pin",
    "id", "current", "work", "orders", "project", "projects", "그렇게",
    "작업", "진행", "해줘", "어떻게", "우리", "현재", "있는", "했던",
    "뭐였더라", "완료", "관련", "부분", "위해서", "하려면", "좋을까",
}


def run(command: list[str], *, input_text: str | None = None) -> str:
    completed = subprocess.run(
        command,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {shlex.join(command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout


def load_recall_log(path: str, host: str) -> list[dict[str, Any]]:
    local = Path(path)
    text = local.read_text(encoding="utf-8") if local.exists() else run(["ssh", host, "cat", path])
    rows = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"recall log line {line_number} is not an object")
        rows.append(row)
    return rows


def select_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = []
    for row_number, reason in SAMPLE_ROWS.items():
        if row_number > len(rows):
            raise ValueError(f"selected row {row_number} is absent from a {len(rows)}-row log")
        row = rows[row_number - 1]
        edges = row.get("edges", [])
        if not 1 <= len(edges) <= 3:
            raise ValueError(f"selected row {row_number} has {len(edges)} edges, expected 1-3")
        if any(not UUID_RE.fullmatch(edge) for edge in edges):
            raise ValueError(f"selected row {row_number} has a non-UUID edge")
        selected.append(
            {
                "row_number": row_number,
                "selection_reason": reason,
                "at": row.get("at"),
                "query": row.get("query", ""),
                "edges": edges,
            }
        )
    return selected


def cypher_for(edge_ids: list[str], group_id: str) -> str:
    if any(not UUID_RE.fullmatch(edge_id) for edge_id in edge_ids):
        raise ValueError("edge IDs must be lowercase UUIDs")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", group_id):
        raise ValueError("group ID contains unsupported characters")
    ids = json.dumps(edge_ids, ensure_ascii=True)
    group = json.dumps(group_id, ensure_ascii=True)
    return f"""// Generated by association_harness.py. Read-only by construction.
WITH {ids} AS baseline_ids
UNWIND baseline_ids AS requested_uuid
OPTIONAL MATCH (source)-[edge:RELATES_TO]->(target)
WHERE edge.uuid = requested_uuid AND edge.group_id = {group}
WITH collect({{
  requested_uuid: requested_uuid,
  found: edge IS NOT NULL,
  uuid: edge.uuid,
  relation_type: edge.name,
  fact: edge.fact,
  invalid_at: toString(edge.invalid_at),
  source: {{uuid: source.uuid, name: source.name, summary: source.summary}},
  target: {{uuid: target.uuid, name: target.name, summary: target.summary}}
}}) AS baselines
RETURN apoc.text.base64Encode(apoc.convert.toJson({{kind: 'baselines', baselines: baselines}})) AS json_base64;

WITH {ids} AS baseline_ids
UNWIND baseline_ids AS baseline_uuid
MATCH (base_source)-[base:RELATES_TO]->(base_target)
WHERE base.uuid = baseline_uuid AND base.group_id = {group}
UNWIND [base_source, base_target] AS anchor
MATCH (anchor)-[candidate:RELATES_TO]-(other)
WHERE candidate.group_id = {group}
  AND candidate.invalid_at IS NULL
  AND NOT candidate.uuid IN baseline_ids
WITH candidate,
     collect(DISTINCT baseline_uuid) AS via_baseline_uuids,
     collect(DISTINCT anchor.uuid) AS anchor_node_uuids
WITH collect({{
  uuid: candidate.uuid,
  relation_type: candidate.name,
  fact: candidate.fact,
  hop: 1,
  via_baseline_uuids: via_baseline_uuids,
  anchor_node_uuids: anchor_node_uuids,
  source: {{uuid: startNode(candidate).uuid, name: startNode(candidate).name}},
  target: {{uuid: endNode(candidate).uuid, name: endNode(candidate).name}}
}}) AS candidates
RETURN apoc.text.base64Encode(apoc.convert.toJson({{kind: 'hop1', candidates: candidates}})) AS json_base64;

WITH {ids} AS baseline_ids
UNWIND baseline_ids AS baseline_uuid
MATCH (base_source)-[base:RELATES_TO]->(base_target)
WHERE base.uuid = baseline_uuid AND base.group_id = {group}
UNWIND [base_source, base_target] AS anchor
MATCH (anchor)-[first:RELATES_TO]-(middle)-[candidate:RELATES_TO]-(other)
WHERE first.group_id = {group} AND first.invalid_at IS NULL
  AND candidate.group_id = {group} AND candidate.invalid_at IS NULL
  AND candidate.uuid <> first.uuid
  AND NOT candidate.uuid IN baseline_ids
WITH candidate,
     collect(DISTINCT baseline_uuid) AS via_baseline_uuids,
     collect(DISTINCT anchor.uuid) AS anchor_node_uuids
WITH collect({{
  uuid: candidate.uuid,
  relation_type: candidate.name,
  fact: candidate.fact,
  hop: 2,
  via_baseline_uuids: via_baseline_uuids,
  anchor_node_uuids: anchor_node_uuids,
  source: {{uuid: startNode(candidate).uuid, name: startNode(candidate).name}},
  target: {{uuid: endNode(candidate).uuid, name: endNode(candidate).name}}
}}) AS candidates
RETURN apoc.text.base64Encode(apoc.convert.toJson({{kind: 'hop2', candidates: candidates}})) AS json_base64;
"""


def assert_read_only(cypher: str) -> None:
    match = FORBIDDEN_CYPHER.search(cypher)
    if match:
        raise ValueError(f"refusing forbidden Cypher token: {match.group(0)}")
    statements = [part.strip() for part in cypher.split(";") if part.strip()]
    if len(statements) != 3:
        raise ValueError(f"expected exactly three read statements, found {len(statements)}")
    for statement in statements:
        without_comments = re.sub(r"(?m)^\s*//.*$", "", statement).lstrip()
        if not without_comments.upper().startswith("WITH "):
            raise ValueError("every generated statement must start with WITH")


def parse_plain_json(stdout: str) -> list[dict[str, Any]]:
    decoded: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        encoded = json.loads(line)
        value = json.loads(base64.b64decode(encoded).decode("utf-8"))
        if isinstance(value, dict) and "kind" in value:
            decoded.append(value)
    if len(decoded) != 3:
        raise ValueError(f"expected three JSON result rows, found {len(decoded)}")
    return decoded


def run_cypher(cypher: str, host: str, container: str, row_number: int) -> dict[str, Any]:
    assert_read_only(cypher)
    remote_host_path = f"/tmp/m3-association-row-{row_number}.cypher"
    container_path = remote_host_path
    with tempfile.TemporaryDirectory(prefix="m3-association-") as temp_dir:
        local_path = Path(temp_dir) / f"row-{row_number}.cypher"
        local_path.write_text(cypher, encoding="utf-8")
        run(["scp", str(local_path), f"{host}:{remote_host_path}"])
    run(["ssh", host, f"docker cp {shlex.quote(remote_host_path)} {shlex.quote(container)}:{shlex.quote(container_path)}"])
    inside = (
        f'/var/lib/neo4j/bin/cypher-shell -u "${{NEO4J_AUTH%%/*}}" '
        f'-p "${{NEO4J_AUTH#*/}}" --format plain -f {shlex.quote(container_path)}'
    )
    remote = f"docker exec {shlex.quote(container)} sh -lc {shlex.quote(inside)}"
    stdout = run(["ssh", host, remote])
    parts = {part["kind"]: part for part in parse_plain_json(stdout)}
    by_uuid: dict[str, dict[str, Any]] = {}
    for kind in ("hop1", "hop2"):
        for candidate in parts[kind]["candidates"]:
            existing = by_uuid.get(candidate["uuid"])
            if existing is None:
                by_uuid[candidate["uuid"]] = candidate
                continue
            existing["via_baseline_uuids"] = sorted(
                set(existing["via_baseline_uuids"]) | set(candidate["via_baseline_uuids"])
            )
            existing["anchor_node_uuids"] = sorted(
                set(existing["anchor_node_uuids"]) | set(candidate["anchor_node_uuids"])
            )
    return {
        "baselines": parts["baselines"]["baselines"],
        "candidates": sorted(by_uuid.values(), key=lambda item: (item["hop"], item["uuid"])),
        "generated_cypher": cypher,
    }


def clean_query(query: str) -> str:
    match = re.search(r"\[최근원\]\s*(.*?)(?:\nSession scope:|$)", query, re.DOTALL)
    if match:
        return match.group(1).strip()
    return re.split(r"\nSession scope:", query, maxsplit=1)[0].strip()


def tokens(text: str) -> set[str]:
    return {
        token.lower().strip("._/-")
        for token in TOKEN_RE.findall(text)
        if len(token.strip("._/-")) >= 2 and token.lower().strip("._/-") not in STOPWORDS
    }


def annotate(row: dict[str, Any], expansion: dict[str, Any]) -> dict[str, Any]:
    query = clean_query(row["query"])
    query_tokens = tokens(query)
    baselines = expansion["baselines"]
    missing = [item["requested_uuid"] for item in baselines if not item["found"]]
    if missing:
        raise ValueError(f"row {row['row_number']} missing baseline edges: {missing}")
    baseline_pairs = {
        frozenset((item["source"]["uuid"], item["target"]["uuid"])) for item in baselines
    }
    candidates = []
    for candidate in expansion["candidates"]:
        candidate_text = " ".join(
            str(value or "")
            for value in (
                candidate.get("fact"), candidate.get("relation_type"),
                candidate.get("source", {}).get("name"), candidate.get("target", {}).get("name"),
            )
        )
        overlap = sorted(query_tokens & tokens(candidate_text))
        pair = frozenset((candidate["source"]["uuid"], candidate["target"]["uuid"]))
        same_endpoint_pair = pair in baseline_pairs
        same_anchor_node = candidate["hop"] == 1
        reasons = []
        if overlap:
            reasons.append("content_word_overlap:" + ",".join(overlap))
        if same_endpoint_pair:
            reasons.append("same_endpoint_pair")
        if same_anchor_node:
            reasons.append("same_anchor_node")
        candidate["heuristic_related"] = bool(reasons)
        candidate["heuristic_reasons"] = reasons
        candidate["lexical_overlap_tokens"] = overlap
        candidates.append(candidate)
    unrelated = sum(not item["heuristic_related"] for item in candidates)
    lexical_unrelated = sum(
        not item["lexical_overlap_tokens"] and "same_endpoint_pair" not in item["heuristic_reasons"]
        for item in candidates
    )
    total = len(candidates)
    return {
        **row,
        "clean_query": query,
        "query_content_tokens": sorted(query_tokens),
        "baseline_kept": baselines,
        "expanded_candidates": candidates,
        "expanded_candidate_count": total,
        "unrelated_candidate_count": unrelated,
        "noise_ratio_estimate": unrelated / total if total else 0.0,
        "lexical_only_noise_sensitivity": lexical_unrelated / total if total else 0.0,
    }


def aggregate(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    baseline_total = sum(len(row["baseline_kept"]) for row in measurements)
    candidate_total = sum(row["expanded_candidate_count"] for row in measurements)
    ratios = [row["noise_ratio_estimate"] for row in measurements]
    lexical_ratios = [row["lexical_only_noise_sensitivity"] for row in measurements]
    return {
        "sample_rows": len(measurements),
        "total_baseline_edges": baseline_total,
        "total_expanded_candidates": candidate_total,
        "fan_out_ratio": candidate_total / baseline_total if baseline_total else 0.0,
        "noise_ratio_estimate_distribution": {
            "min": min(ratios), "median": statistics.median(ratios), "max": max(ratios)
        },
        "lexical_only_noise_sensitivity_distribution": {
            "min": min(lexical_ratios),
            "median": statistics.median(lexical_ratios),
            "max": max(lexical_ratios),
        },
        "heuristic_note": (
            "Primary related=true when a candidate shares a content word, is a parallel edge "
            "between the baseline endpoints, or is a direct 1-hop edge sharing an anchor node. "
            "This intentionally follows the requested exact-node rule and is a conservative "
            "noise estimate. lexical_only_noise_sensitivity omits the broad 1-hop allowance."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--recall-log", default=DEFAULT_LOG)
    parser.add_argument("--group-id", default=DEFAULT_GROUP)
    parser.add_argument("--output-dir", type=Path, default=Path("spike/raw"))
    parser.add_argument(
        "--edge-uuid",
        action="append",
        default=[],
        help="fetch an arbitrary edge UUID list instead of measuring the fixed sample; repeatable",
    )
    parser.add_argument("--reuse-raw", action="store_true", help="recompute metrics without querying Neo4j")
    args = parser.parse_args()

    if args.edge_uuid:
        if args.reuse_raw:
            parser.error("--edge-uuid and --reuse-raw cannot be combined")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        probe_path = args.output_dir / "edge-probe.json"
        expansion = run_cypher(
            cypher_for(args.edge_uuid, args.group_id), args.host, args.container, 0
        )
        probe_path.write_text(
            json.dumps(expansion, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(probe_path)
        return

    rows = load_recall_log(args.recall_log, args.host)
    selected = select_rows(rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "selected_rows.json").write_text(
        json.dumps({"source_row_count": len(rows), "selected": selected}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    measurements = []
    for row in selected:
        raw_path = args.output_dir / f"row-{row['row_number']:03d}-expansion.json"
        if args.reuse_raw:
            expansion = json.loads(raw_path.read_text(encoding="utf-8"))
        else:
            expansion = run_cypher(
                cypher_for(row["edges"], args.group_id), args.host, args.container, row["row_number"]
            )
            raw_path.write_text(json.dumps(expansion, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        measurements.append(annotate(row, expansion))

    aggregate_result = aggregate(measurements)
    (args.output_dir / "measurements.json").write_text(
        json.dumps(measurements, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "aggregate.json").write_text(
        json.dumps(aggregate_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(aggregate_result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
