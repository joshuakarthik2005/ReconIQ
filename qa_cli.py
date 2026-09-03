"""
Settlement Q&A CLI
===================

Standalone entry point for the post-pipeline Q&A layer.
Run after ``run_full_pipeline.py`` has produced ``reports/``.

Usage:
  python qa_cli.py                                  # interactive REPL
  python qa_cli.py --question "Why didn't TXN_007 match?"   # single query
  python qa_cli.py --reports-dir reports/            # custom reports dir
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.qa import SettlementQA


def main():
    parser = argparse.ArgumentParser(
        description="Settlement Q&A — ask questions about reconciliation results"
    )
    parser.add_argument(
        "--question", "-q",
        type=str,
        default=None,
        help="Single question to answer (non-interactive mode)",
    )
    parser.add_argument(
        "--reports-dir",
        type=str,
        default=str(PROJECT_ROOT / "reports"),
        help="Path to reports directory (default: reports/)",
    )
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    if not (reports_dir / "exceptions.json").exists():
        print(
            f"Error: {reports_dir / 'exceptions.json'} not found.\n"
            f"Run 'python run_full_pipeline.py' first to generate reports.",
            file=sys.stderr,
        )
        return 1

    qa = SettlementQA(reports_dir)

    if args.question:
        # Single-question mode
        print(qa.answer(args.question))
        return 0

    # Interactive REPL mode
    print("Settlement Q&A (type 'quit' to exit)")
    print("=" * 50)
    print("Examples:")
    print("  Why didn't TXN_007 match?")
    print("  Show me TXN_001")
    print("  Breakdown by GL category")
    print("  Summary")
    print("=" * 50)

    while True:
        try:
            question = input("\nQ> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if question.lower() in ("quit", "exit", "q"):
            break
        if not question:
            continue

        print()
        print(qa.answer(question))

    return 0


if __name__ == "__main__":
    sys.exit(main())
