"""Freeze, explicitly execute, and independently report the D/E/F comparison."""

import argparse
import json
from pathlib import Path

from mgar_v3.coordination_eval.experiment import child, execute, freeze


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["freeze", "execute", "child", "report"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--source-ref")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--trial")
    parser.add_argument("--execute-paid", action="store_true")
    parser.add_argument("--resume-known", action="store_true")
    parser.add_argument("--repair-ef", action="store_true")
    args = parser.parse_args()
    if args.command == "freeze":
        if not args.source_ref:
            parser.error("freeze requires --source-ref")
        m = freeze(args.directory, args.source_ref, repair=args.repair_ef)
        from mgar_v3.domain import digest

        print(
            json.dumps(
                {
                    "manifest_hash": digest(m),
                    "max_calls": m["max_new_calls"],
                    "max_cost_micros": m["max_new_cost_micros"],
                }
            )
        )
    elif args.command == "report":
        from mgar_v3.coordination_eval.metrics import report

        print(json.dumps(report(args.directory), ensure_ascii=False))
    else:
        if not args.authorization:
            parser.error("execution requires --authorization")
        if args.command == "child":
            if not args.trial:
                parser.error("child requires --trial")
            child(
                args.directory,
                args.authorization,
                args.trial,
                paid=args.execute_paid,
                resume=args.resume_known,
            )
        else:
            execute(args.directory, args.authorization, paid=args.execute_paid)


if __name__ == "__main__":
    main()
