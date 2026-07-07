"""Scoring CLI — resumable, safe to kill and rerun.

Usage:
    python -m portalanaliz.scoring score  [--limit N]   # score unscored posts
    python -m portalanaliz.scoring rollup               # recompute stock_scores
    python -m portalanaliz.scoring stats                # progress + cost per config
    python -m portalanaliz.scoring prompts              # list available prompt sets
    python -m portalanaliz.scoring all    [--limit N]   # score then rollup

Config comes from .env (SCORING_MODEL, SCORING_PROMPT, FOCUS_TICKERS) and can
be overridden per run:

    --model local:qwen3.6:27b
    --prompt uv3                     --tickers SNT,VOT   (empty string = all)
    --workers 5                      # concurrent LLM workers (default 1)

A config is (prompt, model); changing either scores posts fresh under the new
config while keeping old rows for comparison. Reruns within a config are
explicit:

    --rerun error            # drop + rescore this config's error rows
    --rerun scored           # drop + rescore those statuses
    --rerun all              # full fresh pass for this config
    (--rerun respects --tickers/FOCUS_TICKERS; other configs never touched)
"""

from __future__ import annotations

import argparse
import dataclasses
import logging

from sqlalchemy import func, select

from portalanaliz.core.config import ScoringSettings, load_scoring_settings
from portalanaliz.core.db import get_session, init_db
from portalanaliz.core.models import Post, PostScore
from portalanaliz.scoring.pipeline import score_posts
from portalanaliz.scoring.rollup import compute_stock_scores

log = logging.getLogger(__name__)


def print_stats(session, settings: ScoringSettings) -> None:
    total_posts = session.scalar(select(func.count(Post.id))) or 0
    configs = session.execute(
        select(PostScore.prompt_version, PostScore.filter_model,
               PostScore.extract_model,
               func.count(PostScore.id),
               func.sum(PostScore.cost_usd),
               func.sum(PostScore.input_tokens),
               func.sum(PostScore.output_tokens))
        .group_by(PostScore.prompt_version, PostScore.filter_model,
                  PostScore.extract_model)
    ).all()
    active = (settings.prompt, "", settings.model)
    print(f"posts in archive : {total_posts}")
    print(f"active config    : prompt={active[0]} model={settings.model}")
    if settings.tickers:
        print(f"focus tickers    : {', '.join(settings.tickers)}")
    for version, fmodel, emodel, n, cost, inp, out in configs:
        mark = " (active)" if (version, fmodel, emodel) == active else ""
        # New (filterless) rows have fmodel=""; older two-stage rows show it.
        label = f"{version} | {emodel}" + (f" | filter={fmodel}" if fmodel else "")
        print(f"\n[{label}]{mark}")
        statuses = session.execute(
            select(PostScore.status, func.count(PostScore.id))
            .where(PostScore.prompt_version == version,
                   PostScore.filter_model == fmodel,
                   PostScore.extract_model == emodel)
            .group_by(PostScore.status)
        ).all()
        for status, sn in sorted(statuses):
            print(f"  {status:<18}: {sn}")
        uv = session.scalar(
            select(func.count(PostScore.id))
            .where(PostScore.prompt_version == version,
                   PostScore.filter_model == fmodel,
                   PostScore.extract_model == emodel,
                   PostScore.undervalued.is_(True))) or 0
        print(f"  undervalued       : {uv}")
        print(f"  tokens            : {inp or 0} in / {out or 0} out")
        print(f"  cost              : ${cost or 0:.4f}"
              + (f" (${(cost or 0) / n:.5f}/post)" if n else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM scoring of archived posts")
    parser.add_argument("command", choices=["score", "rollup", "stats", "prompts", "all"])
    parser.add_argument("--limit", type=int, default=None,
                        help="max posts sent to the LLM this run")
    parser.add_argument("--workers", type=int, default=1,
                        help="concurrent LLM workers (each its own DB session); "
                             "default 1 = sequential")
    parser.add_argument("--model", help="override SCORING_MODEL")
    parser.add_argument("--prompt", help="override SCORING_PROMPT (named set in prompts.py)")
    parser.add_argument("--tickers",
                        help='override FOCUS_TICKERS, e.g. "SNT,VOT"; "" = all')
    parser.add_argument("--rerun", metavar="WHAT",
                        help='rescore within the active config: "all" or statuses '
                             'like "error" / "chit_chat,scored" (drops those rows first)')
    parser.add_argument("--retry-errors", action="store_true",
                        help='shorthand for --rerun error')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    init_db()
    session = get_session()
    settings = load_scoring_settings()
    overrides = {}
    if args.model:
        overrides["model"] = args.model
    if args.prompt:
        overrides["prompt"] = args.prompt
    if args.tickers is not None:
        overrides["tickers"] = tuple(
            t.strip().upper() for t in args.tickers.split(",") if t.strip())
    if overrides:
        settings = dataclasses.replace(settings, **overrides)

    rerun = args.rerun or ("error" if args.retry_errors else None)
    if rerun:
        from sqlalchemy import delete, select as sa_select

        from portalanaliz.core.models import Post as P, Topic as T
        q = delete(PostScore).where(
            PostScore.prompt_version == settings.prompt,
            PostScore.filter_model == "",
            PostScore.extract_model == settings.model)
        if rerun != "all":
            q = q.where(PostScore.status.in_(
                [s.strip() for s in rerun.split(",") if s.strip()]))
        if settings.tickers:
            in_scope = sa_select(P.id).join(T, T.id == P.topic_id).where(
                T.ticker_hint.in_(settings.tickers))
            q = q.where(PostScore.post_id.in_(in_scope))
        n = session.execute(q).rowcount
        session.commit()
        log.info("rerun %s: dropped %d rows of the active config", rerun, n)

    if args.command == "prompts":
        from portalanaliz.scoring.prompts import available_prompts
        for name, origin in sorted(available_prompts().items()):
            mark = " (active)" if name == settings.prompt else ""
            print(f"{name:<16} {origin}{mark}")

    if args.command in ("score", "all"):
        log.info("config: prompt=%s model=%s tickers=%s workers=%d",
                 settings.prompt, settings.model,
                 ",".join(settings.tickers) or "(all)", args.workers)
        stats = score_posts(session, settings, limit=args.limit, workers=args.workers)
        log.info("run done: %s", stats)
    if args.command in ("rollup", "all"):
        compute_stock_scores(session, settings)
    if args.command == "stats":
        print_stats(session, settings)
    session.close()


if __name__ == "__main__":
    main()
