"""Versioned prompt sets for the scoring pipeline.

PROMPTS is a registry of named, immutable prompt sets. Pick one via
SCORING_PROMPT in .env or `--prompt <name>` on the CLI; the name is stored as
post_scores.prompt_version. To experiment, ADD a new entry (e.g. "v2",
"v1-strict") — never edit an existing one that has scored rows, or stored
results stop meaning what they meant.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSet:
    filter_system: str
    extract_system: str


_FILTER_V1 = """\
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

_EXTRACT_V1 = """\
Jesteś analitykiem ekstrahującym dane z postów polskiego forum giełdowego (GPW).
Dostaniesz post uznany za analizę spółki. Wątek dotyczy spółki wskazanej w
nagłówku ("ticker wątku"), ale post może omawiać też inne spółki.

Odpowiedz WYŁĄCZNIE obiektem JSON o strukturze:
{
  "tickers": [
    {"ticker": "SNT", "name": "Synektik", "direction": "bullish|bearish|neutral", "is_primary": true}
  ],
  "claims": ["kluczowe twierdzenia: mnożniki wyceny, katalizatory, ceny docelowe, liczby"],
  "quality": 0,
  "summary": "1-2 zdania po polsku streszczające tezę posta"
}

Zasady:
- "tickers": każda spółka realnie OMAWIANA w poście (nie tylko wspomniana z nazwy);
  ticker GPW jeśli znany, inaczej null; "is_primary" = spółka wątku.
- "direction": kierunek tezy autora wobec danej spółki.
- "claims": konkretne, weryfikowalne twierdzenia (max 8), po polsku, z liczbami gdzie są.
- "quality" 0-100 — głębokość analizy:
  0-20: opinia bez poparcia; 21-40: pojedynczy argument lub liczba;
  41-60: kilka argumentów, podstawowe liczby; 61-80: solidna analiza z danymi
  finansowymi i uzasadnieniem; 81-100: dogłębna analiza z wyceną, źródłami,
  scenariuszami.
Nie dodawaj żadnego tekstu poza JSON."""


PROMPTS: dict[str, PromptSet] = {
    "v1": PromptSet(filter_system=_FILTER_V1, extract_system=_EXTRACT_V1),
}

DEFAULT_PROMPT = "v1"


def get_prompts(name: str) -> PromptSet:
    try:
        return PROMPTS[name]
    except KeyError:
        raise KeyError(
            f"unknown prompt set {name!r}; available: {', '.join(sorted(PROMPTS))}"
        ) from None


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
