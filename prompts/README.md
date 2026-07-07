# Custom prompt sets

Drop `<name>.prompt` files here and select them with `--prompt <name>` or
`SCORING_PROMPT=<name>` — no code changes needed. The built-in set (`uv4`)
lives in `portalanaliz/scoring/prompts.py`; files here may NOT shadow it.

Format — one required `[scoring]` section, plain text (Polish recommended, the
forum is Polish). Write only the CRITERIA (the yes/no question and what counts
/ doesn't count); the single-post framing and the bare 0/1 output contract are
added automatically, so don't restate the output format.

```
[scoring]
...czy autor stawia tezę, że spółka jest niedowartościowana? Sygnały: ...
Odpowiedz 0, gdy: ...
```

(The legacy `[extract]` section name is still accepted; a `[filter]` section,
if present, is ignored — there is no separate relevance-filter stage anymore.)

The file name is stored as `post_scores.prompt_version`. Once a file has
scored rows, treat it as immutable — copy to a new name to iterate, otherwise
stored results silently change meaning. List everything available with:

    python -m portalanaliz.scoring prompts
