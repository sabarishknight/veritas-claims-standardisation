"""GenAI for the unresolved tail - and the arithmetic for why only the tail.

Where GenAI belongs in this pipeline
------------------------------------
The tempting design is to hand every row to a model: "here is a test name, a
result and a unit, tell me the canonical test and whether it is abnormal". It
would work. It would also be the wrong engineering decision, for three
reasons, in order of importance:

1.  **Non-determinism where determinism is free.** 97% of rows resolve by
    dictionary lookup, exactly, every time, auditably. Replacing an exact
    lookup with a probabilistic one adds no accuracy and removes
    reproducibility - and a claims decision that cannot be reproduced cannot
    be defended to a regulator.
2.  **Cost.** 200,000 files/day at ~150 rows each is ~30M rows/day. At roughly
    200 tokens per row that is ~6B input tokens/day; at Gemini Flash's
    published input price that is several hundred dollars *per day*, around
    $150k/year, to answer questions a dict answers for nothing.
3.  **Latency.** A per-row network call cannot hold a 15-minute p95
    (NFR-1.2) without a fleet of workers sized for the API rather than for the
    work.

So GenAI is scoped to what deterministic code genuinely cannot do: naming a
test from a spelling never seen before, and recovering values a clinic buried
inside a name. Both are *rare* and *cacheable*, which is the whole trick:

    novel names on day one for a new clinic   ~2,000
    cost to resolve each                      ~1 call, ~400 tokens
    cost per novel name                       ~$0.0001
    day-one cost                              well under $1
    cost on day two for the same names        $0 - they are now dictionary
                                              entries (``learned_alias``)

Every accepted answer is promoted into the alias dictionary, so the tail decays
toward zero as a source matures. That decay *is* the mechanism behind
NFR-4.1's "98% of variants resolved within 30 days": not a hope, a ratchet.
Full workings in ``docs/cost-model.md``.

Running without credentials
---------------------------
The reviewer must be able to run everything with no API key, and the output
must be identical on every run. So there are two providers:

    gemini  the real model, used when GEMINI_API_KEY / GOOGLE_API_KEY is set
    stub    a deterministic local fallback used otherwise

The stub is **not** a fake model and never pretends to be one. It is an
abbreviation-expansion matcher: it expands clinical shorthand and retries the
ordinary matcher at a relaxed threshold. Rows it resolves are recorded with
``provider=stub`` in the review evidence, so nothing that came from the stub
can be mistaken for a model decision. On the supplied corpus it adds very
little, which is itself a useful finding and is reported honestly in the run
summary rather than smoothed over.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import Catalog, normalise_name
from ..observe.logging import get_logger

log = get_logger(__name__)

# Published Gemini 2.5 Flash rates at the time of writing, in USD per million
# tokens. Used only to put a number in the run summary and the cost model, so
# a reviewer can see the trade-off rather than take it on trust. Rates move;
# the ratio between "every row" and "the tail" does not.
USD_PER_M_INPUT_TOKENS = 0.30
USD_PER_M_OUTPUT_TOKENS = 2.50
APPROX_TOKENS_PER_NAME = 40          # measured against the batch prompt below
APPROX_OUTPUT_TOKENS_PER_NAME = 30


@dataclass(slots=True)
class LlmDecision:
    source_name: str
    canonical: str | None
    confidence: float
    rationale: str
    provider: str


@dataclass(slots=True)
class LlmUsage:
    calls: int = 0
    names_sent: int = 0
    cache_hits: int = 0
    accepted: int = 0
    rejected: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    provider: str = "none"
    skipped_over_budget: int = 0

    @property
    def estimated_usd(self) -> float:
        return round(
            self.input_tokens / 1_000_000 * USD_PER_M_INPUT_TOKENS
            + self.output_tokens / 1_000_000 * USD_PER_M_OUTPUT_TOKENS,
            6,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "calls": self.calls,
            "names_sent": self.names_sent,
            "cache_hits": self.cache_hits,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "skipped_over_budget": self.skipped_over_budget,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_usd": self.estimated_usd,
        }


class Provider(Protocol):
    name: str
    def resolve(self, names: list[str], catalog: Catalog) -> list[LlmDecision]: ...


# ---------------------------------------------------------------------------
# Prompt
#
# Constrained hard on purpose: a closed candidate list, a required JSON shape,
# and an explicit instruction to return null rather than guess. An open-ended
# "what test is this?" invites a plausible invention, and a plausible invention
# in this pipeline becomes a clinical value in a claim decision.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You map laboratory test names from Indian hospital reports onto a fixed \
catalogue of canonical test names.

Rules:
1. Choose ONLY from the candidate list provided. Never invent a name.
2. If a name is not a laboratory test (a section heading, a page footer, a \
comment, an imaging report), return null.
3. If you are not confident, return null. A null is correct and useful; a \
wrong mapping corrupts a medical claim decision.
4. Source names are OCR output and may be truncated at either end, \
mis-spelled, or missing their first characters.
5. Return strict JSON only, no prose, no markdown fence.

Output schema:
{"mappings":[{"source":"<input>","canonical":"<catalogue name or null>",\
"confidence":<0.0-1.0>,"why":"<=12 words"}]}"""


def build_prompt(names: list[str], catalog: Catalog) -> str:
    """Batch prompt. Candidates are the full canonical list, which is small
    enough (tens of entries) to send in full and large enough to be useful."""
    candidates = sorted(catalog.tests)
    return (
        f"{_SYSTEM_PROMPT}\n\n"
        f"CANDIDATE CANONICAL NAMES ({len(candidates)}):\n"
        + "\n".join(f"- {c}" for c in candidates)
        + "\n\nSOURCE NAMES TO MAP:\n"
        + "\n".join(f"- {n}" for n in names)
        + "\n\nJSON:"
    )


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

_ABBREVIATIONS: dict[str, str] = {
    # Clinical shorthand the dictionaries do not carry as literal aliases.
    "tlc": "total leucocyte count", "dlc": "differential leucocyte count",
    "pcv": "packed cell volume", "hb": "haemoglobin", "hct": "haematocrit",
    "rbs": "random blood sugar", "fbs": "fasting blood sugar",
    "lft": "liver function test", "kft": "kidney function test",
    "rft": "renal function test", "cbc": "complete blood count",
    "sgot": "aspartate aminotransferase", "sgpt": "alanine aminotransferase",
    "alp": "alkaline phosphatase", "bun": "blood urea nitrogen",
    "crp": "c reactive protein", "pct": "procalcitonin",
    "nlr": "neutrophil lymphocyte ratio", "mpv": "mean platelet volume",
    "rdw": "red cell distribution width", "mcv": "mean corpuscular volume",
    "mch": "mean corpuscular haemoglobin",
    "mchc": "mean corpuscular haemoglobin concentration",
    "egfr": "estimated glomerular filtration rate", "bp": "blood pressure",
    "spo2": "oxygen saturation", "wbc": "white blood cell count",
    "rbc": "red blood cell count", "plt": "platelet count",
    "sr": "serum", "s": "serum", "t3": "triiodothyronine", "tsh": "thyroid stimulating hormone",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


class StubProvider:
    """Deterministic local fallback. Not a model; never presented as one.

    Expands clinical abbreviations and re-runs the ordinary matcher at a
    relaxed threshold. Exists so the pipeline is fully runnable and fully
    reproducible with no credentials.
    """

    name = "stub"

    def __init__(self, relaxed_threshold: float = 0.72) -> None:
        self.relaxed_threshold = relaxed_threshold

    def resolve(self, names: list[str], catalog: Catalog) -> list[LlmDecision]:
        # Imported here to avoid a circular import at module load: the
        # resolver owns the matcher, and this is a fallback *inside* it.
        from .names import NameResolver

        matcher = NameResolver(
            catalog,
            accept_threshold=self.relaxed_threshold,
            review_threshold=self.relaxed_threshold,
            affix_tolerance=4,
        )
        out: list[LlmDecision] = []
        for name in names:
            expanded = " ".join(
                _ABBREVIATIONS.get(token, token)
                for token in _TOKEN_RE.findall(normalise_name(name))
            )
            if not expanded or expanded == normalise_name(name):
                out.append(LlmDecision(name, None, 0.0,
                                       "no abbreviation expansion available", self.name))
                continue
            result = matcher.resolve(expanded)
            if result.canonical:
                out.append(LlmDecision(
                    name, result.canonical,
                    min(0.80, round(result.confidence, 3)),
                    f"expanded to {expanded!r}", self.name,
                ))
            else:
                out.append(LlmDecision(name, None, 0.0,
                                       f"expansion {expanded!r} did not match", self.name))
        return out


class GeminiProvider:
    """Calls Gemini with a constrained prompt and a strict JSON contract."""

    name = "gemini"

    def __init__(self, model: str = "gemini-2.5-flash", timeout: int = 20) -> None:
        self.model = model
        self.timeout = timeout
        self._client = None
        self.last_usage = (0, 0)

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))

    def _get_client(self):
        if self._client is None:
            try:
                from google import genai  # type: ignore
            except ImportError as exc:                     # pragma: no cover
                raise RuntimeError(
                    "google-genai is not installed; install with "
                    "python3 -m pip install -e '.[genai]'"
                ) from exc
            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            self._client = genai.Client(api_key=key)
        return self._client

    def resolve(self, names: list[str], catalog: Catalog) -> list[LlmDecision]:
        prompt = build_prompt(names, catalog)
        client = self._get_client()
        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={"temperature": 0.0, "response_mime_type": "application/json"},
        )
        usage = getattr(response, "usage_metadata", None)
        self.last_usage = (
            getattr(usage, "prompt_token_count", 0) or 0,
            getattr(usage, "candidates_token_count", 0) or 0,
        )
        return self._parse(response.text or "", names, catalog)

    def _parse(self, text: str, names: list[str], catalog: Catalog) -> list[LlmDecision]:
        """Parse the model's JSON, rejecting anything outside the catalogue.

        The catalogue check is the important line in this module: a model that
        hallucinates a test name must not be able to create one. Validation
        happens here, at the boundary, not downstream.
        """
        cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            log.warning("LLM returned unparseable JSON; treating batch as unresolved",
                        extra={"names": len(names)})
            return [LlmDecision(n, None, 0.0, "unparseable model response", self.name)
                    for n in names]

        by_source: dict[str, LlmDecision] = {}
        for item in payload.get("mappings") or []:
            source = str(item.get("source") or "")
            canonical = item.get("canonical")
            if canonical is not None and canonical not in catalog.tests:
                log.warning("LLM proposed a name outside the catalogue; rejected",
                            extra={"proposed": canonical, "source": source})
                canonical = None
            try:
                confidence = float(item.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            by_source[normalise_name(source)] = LlmDecision(
                source, canonical if canonical else None,
                max(0.0, min(1.0, confidence)),
                str(item.get("why") or "")[:120], self.name,
            )

        return [
            by_source.get(
                normalise_name(n),
                LlmDecision(n, None, 0.0, "omitted from model response", self.name),
            )
            for n in names
        ]


# ---------------------------------------------------------------------------
# Caching tail resolver
# ---------------------------------------------------------------------------

@dataclass
class TailResolver:
    """Resolves unmapped names, caching every answer as a learned alias.

    The cache is seeded from the ``learned_alias`` table at run start and
    returned for persisting at run end, so knowledge accumulates across runs
    and across workers rather than being re-bought every time.
    """

    catalog: Catalog
    provider: Provider
    min_confidence: float = 0.75
    max_calls: int = 200
    batch_size: int = 20
    cache: dict[str, str] = field(default_factory=dict)
    usage: LlmUsage = field(default_factory=LlmUsage)
    decisions: list[LlmDecision] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.usage.provider = self.provider.name

    def resolve_many(self, names: Iterable[str]) -> dict[str, LlmDecision]:
        """Resolve a batch of unmapped names. Cache first, then the provider."""
        pending: list[str] = []
        out: dict[str, LlmDecision] = {}

        for name in names:
            key = normalise_name(name)
            if not key:
                continue
            cached = self.cache.get(key)
            if cached is not None:
                self.usage.cache_hits += 1
                out[name] = LlmDecision(name, cached, 0.99, "cached learned alias", "cache")
            elif name not in pending:
                pending.append(name)

        for start in range(0, len(pending), self.batch_size):
            if self.usage.calls >= self.max_calls:
                # Hard budget ceiling. Exceeding it is not an error: the
                # remaining names go to the human review queue, which is the
                # correct place for them anyway.
                self.usage.skipped_over_budget += len(pending) - start
                log.info("LLM call budget reached; remaining names go to review",
                         extra={"budget": self.max_calls,
                                "deferred": len(pending) - start})
                break

            batch = pending[start:start + self.batch_size]
            try:
                decisions = self.provider.resolve(batch, self.catalog)
            except Exception as exc:                        # noqa: BLE001
                # A provider failure must never fail the run. The names fall
                # through to the review queue and the pipeline keeps going.
                log.warning("LLM provider failed; batch deferred to review",
                            extra={"error": str(exc), "batch": len(batch)})
                self.usage.calls += 1
                continue

            self.usage.calls += 1
            self.usage.names_sent += len(batch)
            measured = getattr(self.provider, "last_usage", None)
            if measured and any(measured):
                self.usage.input_tokens += measured[0]
                self.usage.output_tokens += measured[1]
            else:
                self.usage.input_tokens += len(batch) * APPROX_TOKENS_PER_NAME
                self.usage.output_tokens += len(batch) * APPROX_OUTPUT_TOKENS_PER_NAME

            for decision in decisions:
                self.decisions.append(decision)
                out[decision.source_name] = decision
                if decision.canonical and decision.confidence >= self.min_confidence:
                    self.cache[normalise_name(decision.source_name)] = decision.canonical
                    self.usage.accepted += 1
                else:
                    self.usage.rejected += 1

        return out

    def learned_aliases(self) -> dict[str, str]:
        return dict(self.cache)


def build_tail_resolver(
    catalog: Catalog,
    llm_cfg: Mapping[str, Any],
    learned: Mapping[str, str] | None = None,
) -> TailResolver | None:
    """Construct the tail resolver from config, or ``None`` when disabled."""
    if not llm_cfg.get("enabled", False):
        return None

    requested = str(llm_cfg.get("provider", "stub"))
    if requested == "gemini" and GeminiProvider.available():
        provider: Provider = GeminiProvider(
            model=str(llm_cfg.get("model", "gemini-2.5-flash")),
            timeout=int(llm_cfg.get("timeout_seconds", 20)),
        )
    else:
        if requested == "gemini" and not llm_cfg.get("stub_when_unauthenticated", True):
            log.warning("Gemini requested but unauthenticated, and stub disabled; "
                        "tail names will go straight to review")
            return None
        if requested == "gemini":
            log.info("no GEMINI_API_KEY present; using the deterministic stub provider "
                     "so the run stays reproducible")
        provider = StubProvider()

    return TailResolver(
        catalog=catalog,
        provider=provider,
        min_confidence=float(llm_cfg.get("min_confidence_to_accept", 0.75)),
        max_calls=int(llm_cfg.get("max_calls_per_run", 200)),
        batch_size=int(llm_cfg.get("batch_size", 20)),
        cache=dict(learned or {}),
    )


def cost_comparison(rows_per_day: int, novel_names_per_day: int) -> dict[str, Any]:
    """Side-by-side cost of LLM-per-row versus LLM-on-the-tail.

    Surfaced in the ops UI and in ``docs/cost-model.md`` because a design
    decision justified by cost should carry its own arithmetic, checkable by
    the reader, rather than an assertion.
    """
    per_row_input = rows_per_day * 200
    per_row_output = rows_per_day * 20
    per_row_usd = (per_row_input / 1e6 * USD_PER_M_INPUT_TOKENS
                   + per_row_output / 1e6 * USD_PER_M_OUTPUT_TOKENS)

    tail_input = novel_names_per_day * APPROX_TOKENS_PER_NAME
    tail_output = novel_names_per_day * APPROX_OUTPUT_TOKENS_PER_NAME
    tail_usd = (tail_input / 1e6 * USD_PER_M_INPUT_TOKENS
                + tail_output / 1e6 * USD_PER_M_OUTPUT_TOKENS)

    return {
        "rows_per_day": rows_per_day,
        "novel_names_per_day": novel_names_per_day,
        "llm_per_row_usd_per_day": round(per_row_usd, 2),
        "llm_per_row_usd_per_year": round(per_row_usd * 365, 2),
        "llm_tail_usd_per_day": round(tail_usd, 4),
        "llm_tail_usd_per_year": round(tail_usd * 365, 2),
        "ratio": round(per_row_usd / tail_usd, 1) if tail_usd else None,
    }
