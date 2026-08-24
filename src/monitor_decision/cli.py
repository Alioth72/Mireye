from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agentic import AgentConfig, decide
from .models import DecisionRequest
from .replay import replay


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mireye-monitor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    score_parser = subparsers.add_parser("score", help="Score one event/site/Mireye request JSON.")
    score_parser.add_argument("--input", "-i", required=True, help="Path to decision request JSON.")
    score_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    score_parser.add_argument("--agentic", action="store_true", help="Use LLM adjudication after deterministic scoring.")
    score_parser.add_argument(
        "--provider",
        choices=["auto", "openai", "gemini"],
        default="auto",
        help="LLM provider for agentic mode. auto tries OpenAI, then Gemini.",
    )

    replay_parser = subparsers.add_parser("replay", help="Evaluate historical JSON/JSONL records.")
    replay_parser.add_argument("--input", "-i", required=True, help="Path to replay JSON array or JSONL file.")
    replay_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    args = parser.parse_args(argv)
    if args.command == "score":
        return _score(Path(args.input), pretty=args.pretty, agentic=args.agentic, provider=args.provider)
    if args.command == "replay":
        return _replay(Path(args.input), pretty=args.pretty)
    parser.error(f"unknown command {args.command}")
    return 2


def _score(path: Path, pretty: bool, agentic: bool, provider: str) -> int:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    response = decide(
        DecisionRequest.from_dict(payload),
        AgentConfig.from_env(enabled=agentic, provider=provider),
    )
    json.dump(response, sys.stdout, indent=2 if pretty else None, sort_keys=pretty)
    sys.stdout.write("\n")
    return 0


def _replay(path: Path, pretty: bool) -> int:
    records = _read_records(path)
    response = replay(records)
    json.dump(response, sys.stdout, indent=2 if pretty else None, sort_keys=pretty)
    sys.stdout.write("\n")
    return 0


def _read_records(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
