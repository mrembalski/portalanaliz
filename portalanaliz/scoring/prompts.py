"""Versioned prompt sets for the scoring pipeline.

PROMPTS is a registry of named, immutable prompt sets. Pick one via
SCORING_PROMPT in .env or `--prompt <name>` on the CLI; the name is stored as
post_scores.prompt_version. To experiment, ADD a new entry (e.g. "uv4",
"uv3-strict") — never edit an existing one that has scored rows, or stored
results stop meaning what they meant.

Current goal: a single binary undervaluation signal per post. There is no
separate relevance/chit-chat filter stage anymore — chit-chat simply scores 0.
Posts are scored in BATCHES: the model receives several numbered posts at once
and returns one 0/1 per post, so a set is a scoring `system` prompt plus a
shared batch framing that pins the I/O format.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PromptSet:
    # System prompt for the batched undervaluation call. Combined with the
    # shared batch framing (intro + JSON-array output contract) by _compose().
    system: str


# ── Shared batch framing ──────────────────────────────────────────────────
# Every set answers the same question shape over a batch of posts and returns
# the same JSON array; only the middle "criteria" differs between sets.

_BATCH_INTRO = """\
Jesteś analitykiem czytającym posty z polskiego forum giełdowego (GPW, portalanaliz.pl).
Dostaniesz PARTIĘ ponumerowanych postów (POST 1, POST 2, ...). Każdy pochodzi z
wątku o danej spółce (ticker wątku podany w nagłówku posta), ale może dotyczyć
też innych spółek.

Dla KAŻDEGO posta osobno odpowiedz na jedno pytanie:"""

_BATCH_OUTPUT = """\
Odpowiedz WYŁĄCZNIE obiektem JSON z jedną tablicą "scores": po jednej wartości
0 lub 1 na każdy post, w tej samej kolejności co posty (1 = tak, 0 = nie).
Liczba wartości MUSI być równa liczbie postów. Nie dodawaj żadnego innego tekstu.

Przykład dla 5 postów: {"scores": [0, 1, 0, 0, 1]}"""


def _compose(criteria: str) -> str:
    return f"{_BATCH_INTRO}\n\n{criteria.strip()}\n\n{_BATCH_OUTPUT}"


# ── Per-set criteria ──────────────────────────────────────────────────────

_CRIT_UV3 = """\
Czy AUTOR POSTA, na podstawie WŁASNEJ analizy lub własnego researchu, wyraża
opinię, że spółka jest NIEDOWARTOŚCIOWANA (rynek wycenia ją poniżej wartości)?
Muszą zajść oba warunki:
- Warunek 1 — teza o niedowartościowaniu: niska wycena wskaźnikowa na tle
  wyników/branży (C/Z, EV/EBITDA...), cena poniżej oszacowanej wartości, kurs
  docelowy/wycena powyżej obecnej ceny, "rynek nie dostrzega", aktywa/gotówka
  warte więcej niż kapitalizacja.
- Warunek 2 — teza wynika z WŁASNEJ pracy autora: sam liczy, porównuje,
  analizuje wyniki/biznes/wskaźniki, opisuje własny research (raporty, kontakt
  ze spółką, obserwacja branży). Sygnały: własne obliczenia/szacunki, autorskie
  porównania do konkurencji, wnioski z lektury raportu, "policzyłem",
  "z moich szacunków", "moim zdaniem po wynikach...".

Odpowiedz 0, gdy (którekolwiek): autor tylko POWTARZA cudzą opinię (rekomendację
biura maklerskiego, wycenę z raportu, opinię innego forumowicza, artykuł) bez
własnego wkładu; sam optymizm bez odniesienia do wyceny; analiza techniczna,
sentyment, przeczucia; słowo "tanio"/"niedowartościowana" bez uzasadnienia;
relacja wyników bez własnego wniosku autora o wycenie."""


PROMPTS: dict[str, PromptSet] = {
    # Flags only undervaluation theses grounded in the author's OWN analysis
    # or research — repeating someone else's recommendation doesn't count.
    "uv3": PromptSet(system=_compose(_CRIT_UV3)),
}

DEFAULT_PROMPT = "uv3"

# User-defined prompt sets live here as <name>.prompt files with a single
# [scoring] section (legacy [extract] is also accepted) — no code edit needed.
# The FILE NAME is the stored prompt_version, so treat a file with scored rows
# as immutable: copy to a new name to iterate. The batch I/O framing is added
# automatically, so the file only needs the criteria (the question).
PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _load_prompt_file(path: Path) -> PromptSet:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        marker = line.strip().lower()
        if marker in ("[scoring]", "[extract]", "[filter]"):
            current = marker[1:-1]
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    body = sections.get("scoring") or sections.get("extract")
    if body is None:
        raise KeyError(f"{path} is missing a [scoring] section")
    return PromptSet(system=_compose("\n".join(body).strip()))


def available_prompts() -> dict[str, str]:
    """name -> origin ("built-in" or file path)."""
    out = {name: "built-in" for name in PROMPTS}
    if PROMPTS_DIR.is_dir():
        for path in sorted(PROMPTS_DIR.glob("*.prompt")):
            out.setdefault(path.stem, str(path))
    return out


def get_prompts(name: str) -> PromptSet:
    if name in PROMPTS:
        return PROMPTS[name]
    path = PROMPTS_DIR / f"{name}.prompt"
    if path.is_file():
        return _load_prompt_file(path)
    raise KeyError(
        f"unknown prompt set {name!r}; available: "
        + ", ".join(sorted(available_prompts()))
        + f" (or add {path})"
    )


def batch_user(items: list[tuple[str, str | None, str, str]],
               max_chars: int = 6000) -> str:
    """Render a batch of posts as one numbered user message.

    `items` is a list of (topic_title, ticker_hint, post_time, text) in the
    order the model must score them; POST i in the prompt == items[i-1]."""
    blocks = []
    for i, (title, ticker, post_time, text) in enumerate(items, 1):
        blocks.append(
            f"### POST {i}\n"
            f"Wątek: {title}\n"
            f"Ticker wątku: {ticker or 'nieznany'}\n"
            f"Data posta: {post_time}\n"
            f"Treść:\n{text[:max_chars]}"
        )
    return (f"Oceń poniższe {len(items)} postów. Zwróć tablicę "
            f"{len(items)} wartości 0/1.\n\n" + "\n\n".join(blocks))
