"""CLI.

  python -m clipping                 edição da manhã (nuvem, 05:17)
  python -m clipping --collect-only  coleta noturna (só guarda itens)
  python -m clipping --complement    complemento das 6h (computador)
  python -m clipping --dry-run ...   gera os arquivos em output/ sem enviar e-mail
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader (used on the local computer; GitHub Actions passes secrets as env vars)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    ap = argparse.ArgumentParser(description="LatAm Financials Clipping.")
    ap.add_argument("--dry-run", action="store_true", help="gera os arquivos em output/ mas não envia e-mail")
    ap.add_argument("--collect-only", action="store_true",
                    help="só coleta e guarda os itens (execução noturna; sem Claude e sem e-mail)")
    ap.add_argument("--complement", action="store_true",
                    help="complemento das 6h: confere a edição das 05:17 e envia só o que mudou/faltou")
    ap.add_argument("--force", action="store_true", help="(complemento) roda mesmo se já rodou hoje / fim de semana")
    ap.add_argument("--now", help="simula o horário de execução (ISO 8601, com fuso)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    load_dotenv()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    now = datetime.fromisoformat(args.now) if args.now else None

    from .pipeline import collect_only, run

    if args.collect_only:
        collect_only(now=now)
    elif args.complement:
        from .complement import complement

        complement(now=now, dry_run=args.dry_run, force=args.force)
    else:
        run(dry_run=args.dry_run, now=now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
