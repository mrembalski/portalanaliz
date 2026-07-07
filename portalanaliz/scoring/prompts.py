"""Versioned prompt sets for the scoring pipeline.

PROMPTS is a registry of named, immutable prompt sets. Pick one via
SCORING_PROMPT in .env or `--prompt <name>` on the CLI; the name is stored as
post_scores.prompt_version. To experiment, ADD a new entry (e.g. "uv5",
"uv4-strict") — never edit an existing one that has scored rows, or stored
results stop meaning what they meant.

Current goal: a single binary undervaluation signal per post. There is no
separate relevance/chit-chat filter stage anymore — chit-chat simply scores 0.
Each post is scored in its own LLM call: the model receives one post and
answers with a single digit (0 or 1), so a set is just the criteria; the
shared single-post framing pins the I/O format.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PromptSet:
    # System prompt for the per-post undervaluation call. Combined with the
    # shared framing (intro + single-digit output contract) by _compose().
    system: str


# ── Shared framing ────────────────────────────────────────────────────────
# Every set answers the same question about one post and returns a single
# digit; only the middle "criteria" differs between sets.

_INTRO = """\
Jesteś analitykiem czytającym posty z polskiego forum giełdowego (GPW, portalanaliz.pl).
Dostaniesz JEDEN post. Pochodzi z wątku o danej spółce (ticker wątku podany
w nagłówku posta), ale może dotyczyć też innych spółek.

Odpowiedz na jedno pytanie:"""

_OUTPUT = """\
Odpowiedz WYŁĄCZNIE jedną cyfrą: 1 (tak) albo 0 (nie).
Nie dodawaj żadnego innego tekstu."""


def _compose(criteria: str) -> str:
    return f"{_INTRO}\n\n{criteria.strip()}\n\n{_OUTPUT}"


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


_CRIT_SENT1 = """\
Czy AUTOR POSTA wyraża POZYTYWNE nastawienie do spółki, której dotyczy post
(oczekuje wzrostu kursu, poprawy wyników, dobrze ocenia biznes lub perspektywy)?

Odpowiedz 1, gdy wydźwięk posta wobec spółki jest wyraźnie pozytywny.
Odpowiedz 0, gdy wydźwięk jest negatywny, mieszany, neutralny (sama relacja
faktów bez oceny) albo post nie dotyczy żadnej spółki."""


PROMPTS: dict[str, PromptSet] = {
    # Flags only undervaluation theses grounded in the author's OWN analysis
    # or research — repeating someone else's recommendation doesn't count.
    # uv4 = uv3's criteria under the single-post framing (uv3 rows in the DB
    # were scored with the old batched JSON framing — kept for comparison).
    "uv4": PromptSet(system=_compose(_CRIT_UV3)),
    # Sentiment, not valuation: 1 = author clearly positive about the company,
    # 0 = negative/mixed/neutral/off-topic. Same single-post 0/1 framing.
    "sent1": PromptSet(system=_compose(_CRIT_SENT1)),
}

DEFAULT_PROMPT = "uv4"

# User-defined prompt sets live here as <name>.prompt files with a single
# [scoring] section (legacy [extract] is also accepted) — no code edit needed.
# The FILE NAME is the stored prompt_version, so treat a file with scored rows
# as immutable: copy to a new name to iterate. The I/O framing is added
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


def post_user(title: str, ticker: str | None, post_time: str, text: str) -> str:
    """Render one post as the user message (full text, no truncation)."""
    return (
        f"Wątek: {title}\n"
        f"Ticker wątku: {ticker or 'nieznany'}\n"
        f"Data posta: {post_time}\n"
        f"Treść:\n{text}"
    )
