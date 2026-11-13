"""CLI: python -m clipping [--dry-run] [--now 2026-11-13T05:30-03:00]"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from .pipeline import collect_only, run


def main() -> int:
    ap = argparse.ArgumentParser(description="Gera e envia o LatAm Financials Clipping.")
    ap.add_argument("--dry-run", action="store_true", help="gera os arquivos em output/ mas não envia e-mail")
    ap.add_argument("--collect-only", action="store_true",
                    help="só coleta e guarda os itens (execução de hora em hora; sem Claude e sem e-mail)")
    ap.add_argument("--now", help="simula o horário de execução (ISO 8601, com fuso)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    now = datetime.fromisoformat(args.now) if args.now else None
    if args.collect_only:
        collect_only(now=now)
    else:
        run(dry_run=args.dry_run, now=now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
