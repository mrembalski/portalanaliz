# Custom prompt sets

Drop `<name>.prompt` files here and select them with `--prompt <name>` or
`SCORING_PROMPT=<name>` — no code changes needed. Built-in sets (`uv1`, `uv2`, `uv3`)
live in `portalanaliz/scoring/prompts.py`; files here may NOT shadow them.

Format — two required sections, plain text (Polish recommended, the forum is
Polish):

```
[filter]
...system prompt for the chit-chat gate; must ask for {"analysis": true/false}...

[extract]
...system prompt for the undervaluation call; must ask for
{"undervalued": true|false, "tickers": [...], "reason": "..."}...
```

The file name is stored as `post_scores.prompt_version`. Once a file has
scored rows, treat it as immutable — copy to a new name to iterate, otherwise
stored results silently change meaning. List everything available with:

    python -m portalanaliz.scoring prompts
