"""Versioned prompt sets for the scoring pipeline.

PROMPTS is a registry of named, immutable prompt sets. Pick one via
SCORING_PROMPT in .env or `--prompt <name>` on the CLI; the name is stored as
post_scores.prompt_version. To experiment, ADD a new entry (e.g. "uv2",
"uv1-strict") — never edit an existing one that has scored rows, or stored
results stop meaning what they meant.

Current goal: binary undervaluation signal per post (not company scoring) —
stage 1 gates out chit-chat, stage 2 answers "does the author argue the stock
is undervalued?" with 0/1 + tickers + short reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PromptSet:
    filter_system: str
    extract_system: str


_FILTER_UV1 = """\
Jesteś klasyfikatorem postów z polskiego forum giełdowego (GPW, portalanaliz.pl).

Oceń, czy post zawiera FAKTYCZNĄ ANALIZĘ spółki lub akcji — czyli co najmniej jedno z:
- omówienie wyników finansowych, wyceny, wskaźników (C/Z, EV/EBITDA, marże...),
- tezę inwestycyjną z uzasadnieniem (dlaczego kupować/sprzedawać/trzymać),
- analizę biznesu, kontraktów, perspektyw, katalizatorów,
- cenę docelową lub szacunek wartości z argumentacją.

NIE jest analizą: small talk, emocje o kursie ("ale spadło", "to the moon"),
pytania bez treści, same linki/newsy bez komentarza, jednozdaniowe opinie,
czysta analiza techniczna bez odniesienia do spółki ("wsparcie na 12 zł").

Odpowiedz WYŁĄCZNIE obiektem JSON: {"analysis": true} lub {"analysis": false}"""

_EXTRACT_UV1 = """\
Jesteś analitykiem czytającym posty polskiego forum giełdowego (GPW).
Dostaniesz post uznany za analizę spółki. Wątek dotyczy spółki wskazanej w
nagłówku ("ticker wątku"), ale post może dotyczyć też innych spółek.

Twoje JEDYNE zadanie: czy autor stawia tezę, że spółka jest NIEDOWARTOŚCIOWANA —
że rynek wycenia ją poniżej wartości? Sygnały: niska wycena wskaźnikowa na tle
wyników/branży (C/Z, EV/EBITDA...), cena poniżej oszacowanej wartości
wewnętrznej, kurs docelowy/wycena powyżej obecnej ceny z argumentacją,
"rynek nie dostrzega", aktywa/gotówka warte więcej niż kapitalizacja.

NIE jest sygnałem niedowartościowania: sam optymizm lub oczekiwanie wzrostu bez
odniesienia do wyceny, analiza techniczna, nadzieja na kontrakt/produkt bez
zestawienia z obecną wyceną, relacjonowanie wyników bez tezy o wycenie.

Odpowiedz WYŁĄCZNIE obiektem JSON:
{
  "undervalued": true|false,
  "tickers": ["SNT"],
  "reason": "jedno zdanie po polsku: dlaczego tak/nie, z liczbami jeśli są"
}

"tickers": spółki, których dotyczy sygnał niedowartościowania (tickery GPW);
pusta lista gdy "undervalued" jest false. Nie dodawaj tekstu poza JSON."""


_EXTRACT_UV2 = """\
Jesteś analitykiem czytającym posty polskiego forum giełdowego (GPW).
Dostaniesz post uznany za analizę spółki. Wątek dotyczy spółki wskazanej w
nagłówku ("ticker wątku"), ale post może dotyczyć też innych spółek.

Twoje JEDYNE zadanie: czy autor stawia KONKRETNĄ tezę o niedowartościowaniu —
tj. wprost porównuje obecną wycenę rynkową z wartością, którą sam szacuje lub
wywodzi. Wymagany jest LICZBOWY argument wyceny, co najmniej jeden z:
- wskaźnik wyceny (C/Z, C/WK, EV/EBITDA...) zestawiony z wynikami, branżą
  lub historią i oceniony jako niski,
- oszacowana wartość/kurs docelowy powyżej obecnej ceny (z liczbą),
- aktywa/gotówka/segmenty wycenione i porównane z kapitalizacją.

NIE licz jako sygnału (odpowiedz false):
- optymizm, "będzie rosło", dobre perspektywy — bez porównania z wyceną,
- sama relacja wyników (nawet świetnych) bez tezy "rynek tego nie wycenia",
- analiza techniczna, przeczucia, sentyment,
- powtórzenie cudzej rekomendacji bez własnego odniesienia do wyceny,
- słowo "tanio"/"niedowartościowana" rzucone bez żadnej liczby.

Odpowiedz WYŁĄCZNIE obiektem JSON:
{
  "undervalued": true|false,
  "tickers": ["SNT"],
  "reason": "jedno zdanie po polsku: jaki liczbowy argument padł (lub czego zabrakło)"
}

"tickers": spółki, których dotyczy sygnał (tickery GPW); pusta lista gdy
"undervalued" jest false. Nie dodawaj tekstu poza JSON."""


_EXTRACT_UV3 = """\
Jesteś analitykiem czytającym posty polskiego forum giełdowego (GPW).
Dostaniesz post uznany za analizę spółki. Wątek dotyczy spółki wskazanej w
nagłówku ("ticker wątku"), ale post może dotyczyć też innych spółek.

Twoje JEDYNE zadanie: czy AUTOR POSTA, na podstawie WŁASNEJ analizy lub
własnego researchu, wyraża opinię, że spółka jest NIEDOWARTOŚCIOWANA —
że rynek wycenia ją poniżej wartości?

Warunek 1 — teza o niedowartościowaniu, np.: niska wycena wskaźnikowa na tle
wyników/branży (C/Z, EV/EBITDA...), cena poniżej oszacowanej wartości,
kurs docelowy/wycena powyżej obecnej ceny, "rynek nie dostrzega",
aktywa/gotówka warte więcej niż kapitalizacja.

Warunek 2 — teza wynika z WŁASNEJ pracy autora: autor sam liczy, porównuje,
analizuje wyniki/biznes/wskaźniki, opisuje własny research (raporty, kontakt
ze spółką, obserwacja branży) i wyciąga z tego wniosek. Sygnały własnej
analizy: własne obliczenia lub szacunki, autorskie porównania do konkurencji,
wnioski z lektury raportu, "policzyłem", "z moich szacunków", "moim zdaniem
po wynikach...".

Odpowiedz false, gdy (którekolwiek):
- autor tylko POWTARZA cudzą opinię: rekomendację biura maklerskiego, wycenę
  z raportu analitycznego, opinię innego forumowicza, artykuł — bez własnego
  wkładu analitycznego,
- sam optymizm lub oczekiwanie wzrostu bez odniesienia do wyceny,
- analiza techniczna, sentyment, przeczucia,
- słowo "tanio"/"niedowartościowana" rzucone bez uzasadnienia,
- relacja wyników bez wniosku autora o wycenie.

Odpowiedz WYŁĄCZNIE obiektem JSON:
{
  "undervalued": true|false,
  "tickers": ["SNT"],
  "reason": "jedno zdanie po polsku: jaka własna analiza autora stoi za tezą (lub czego zabrakło)"
}

"tickers": spółki, których dotyczy sygnał niedowartościowania (tickery GPW);
pusta lista gdy "undervalued" jest false. Nie dodawaj tekstu poza JSON."""


PROMPTS: dict[str, PromptSet] = {
    "uv1": PromptSet(filter_system=_FILTER_UV1, extract_system=_EXTRACT_UV1),
    # Stricter: flags only posts with an explicit, numeric valuation argument.
    "uv2": PromptSet(filter_system=_FILTER_UV1, extract_system=_EXTRACT_UV2),
    # Flags only undervaluation theses grounded in the author's OWN analysis
    # or research — repeating someone else's recommendation doesn't count.
    "uv3": PromptSet(filter_system=_FILTER_UV1, extract_system=_EXTRACT_UV3),
}

DEFAULT_PROMPT = "uv1"

# User-defined prompt sets live here as <name>.prompt files with [filter] and
# [extract] sections — no code edit needed. The FILE NAME is the stored
# prompt_version, so treat a file with scored rows as immutable: copy to a new
# name to iterate.
PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _load_prompt_file(path: Path) -> PromptSet:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        marker = line.strip().lower()
        if marker in ("[filter]", "[extract]"):
            current = marker[1:-1]
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    missing = {"filter", "extract"} - set(sections)
    if missing:
        raise KeyError(f"{path} is missing section(s): {', '.join(sorted(missing))}")
    return PromptSet(
        filter_system="\n".join(sections["filter"]).strip(),
        extract_system="\n".join(sections["extract"]).strip(),
    )


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


def extraction_user(topic_title: str, ticker_hint: str | None, post_time: str,
                    text: str, max_chars: int = 8000) -> str:
    return (
        f"Wątek: {topic_title}\n"
        f"Ticker wątku: {ticker_hint or 'nieznany'}\n"
        f"Data posta: {post_time}\n\n"
        f"Treść posta:\n{text[:max_chars]}"
    )


def filter_user(topic_title: str, text: str, max_chars: int = 4000) -> str:
    return f"Wątek: {topic_title}\n\nTreść posta:\n{text[:max_chars]}"
