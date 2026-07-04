"""Scoring CLI — resumable, safe to kill and rerun.

Usage:
    python -m portalanaliz.scoring score  [--limit N]   # score unscored posts
    python -m portalanaliz.scoring rollup               # recompute stock_scores
    python -m portalanaliz.scoring stats                # progress + cost so far
    python -m portalanaliz.scoring all    [--limit N]   # score then rollup

Model selection via .env: SCORING_FILTER_MODEL / SCORING_EXTRACT_MODEL
("anthropic:<model>" or "local:<model>"; local = OpenAI-compatible server at
LOCAL_LLM_BASE_URL, default Ollama on :11434).
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import func, select

from portalanaliz.core.config import load_scoring_settings
from portalanaliz.core.db import get_session, init_db
from portalanaliz.core.models import Post, PostScore
from portalanaliz.scoring import prompts
from portalanaliz.scoring.pipeline import score_posts
from portalanaliz.scoring.rollup import compute_stock_scores

log = logging.getLogger(__name__)


def print_stats(session) -> None:
    total_posts = session.scalar(select(func.count(Post.id))) or 0
    rows = session.execute(
        select(PostScore.status, func.count(PostScore.id))
        .where(PostScore.prompt_version == prompts.PROMPT_VERSION)
        .group_by(PostScore.status)
    ).all()
    done = sum(n for _, n in rows)
    cost, inp, out = session.execute(
        select(func.sum(PostScore.cost_usd), func.sum(PostScore.input_tokens),
               func.sum(PostScore.output_tokens))
        .where(PostScore.prompt_version == prompts.PROMPT_VERSION)
    ).one()
    print(f"prompt version : {prompts.PROMPT_VERSION}")
    print(f"posts          : {done}/{total_posts} processed")
    for status, n in sorted(rows):
        print(f"  {status:<18}: {n}")
    print(f"tokens         : {inp or 0} in / {out or 0} out")
    print(f"cost           : ${cost or 0:.4f}"
          + (f" (${(cost or 0) / done:.5f}/post)" if done else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM scoring of archived posts")
    parser.add_argument("command", choices=["score", "rollup", "stats", "all"])
    parser.add_argument("--limit", type=int, default=None,
                        help="max posts sent to the LLM this run (skips are free)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    init_db()
    session = get_session()
    settings = load_scoring_settings()

    if args.command in ("score", "all"):
        log.info("filter=%s extract=%s", settings.filter_model, settings.extract_model)
        stats = score_posts(session, settings, limit=args.limit)
        log.info("run done: %s", stats)
    if args.command in ("rollup", "all"):
        compute_stock_scores(session)
    if args.command == "stats":
        print_stats(session)
    session.close()


if __name__ == "__main__":
    main()
