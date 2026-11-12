"""Answer cache keyed on the *meaning* of a question, computed locally (no API call).

normalize() lower-cases, drops filler words, collapses synonyms (marks/percentage -> score,
iPad/phone -> mobile, cost/price -> fee ...) and crudely stems, producing a token set.
Same token set => exact hit. Otherwise Jaccard >= 0.75 on sets with >= 2 tokens => fuzzy hit.
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict

STOP = set(
    "a an the is are was be am do does did can could will would should may i me my we you your it its "
    "of for to in on at and or if so as with by from about this that these those what whats which "
    "please tell know want get got need needed needs required require requires minimum there any some "
    "also just course dpm 101 dpm101 thing like happen happens".split()
)
SYN = {
    "marks": "score", "mark": "score", "percentage": "score", "percent": "score", "points": "score",
    "grade": "score", "grades": "score", "threshold": "score", "cutoff": "score",
    "passing": "pass", "passed": "pass", "qualify": "pass", "certificate": "pass",
    "certification": "pass", "verified": "pass",
    "cost": "fee", "costs": "fee", "price": "fee", "pay": "fee", "paid": "fee", "charge": "fee",
    "charges": "fee", "fees": "fee",
    "submission": "submit", "submitted": "submit", "submit": "submit",
    "deadline": "due", "ipad": "mobile", "tablet": "mobile", "phone": "mobile", "iphone": "mobile",
    "laptop": "computer", "desktop": "computer", "quizzes": "quiz", "exams": "exam", "tests": "exam",
    "test": "exam", "finals": "final",
}
FOLLOWUP = re.compile(r"\b(it|its|that|this|those|they|them|then|there|also|same|above|earlier|previous)\b"
                      r"|^\s*(and|what about|how about|but)\b", re.I)


def _stem(t: str) -> str:
    if len(t) > 5 and t.endswith("ing"):
        return t[:-3]
    if len(t) > 4 and t.endswith("ed"):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def normalize(q: str) -> frozenset[str]:
    q = re.sub(r"\bhow (much|many)\b", " ", q.lower())
    out = set()
    for t in re.findall(r"[a-z0-9]+", q):
        if t in STOP:
            continue
        t = SYN.get(t, t)
        t = SYN.get(_stem(t), _stem(t))
        if t not in STOP:
            out.add(t)
    return frozenset(out)


def needs_context(q: str, tokens: frozenset[str]) -> bool:
    """True when the question can't be understood without the previous turn."""
    return len(tokens) < 2 or bool(FOLLOWUP.search(q))


def key_of(tokens: frozenset[str]) -> str:
    return " ".join(sorted(tokens))


class AnswerCache:
    def __init__(self, ttl: float = 86400, max_items: int = 2000, threshold: float = 0.75):
        self._d: OrderedDict[tuple[str, str], tuple[frozenset[str], dict, float]] = OrderedDict()
        self.ttl, self.max_items, self.threshold = ttl, max_items, threshold

    def get(self, ns: str, tokens: frozenset[str]) -> dict | None:
        if not tokens:
            return None
        now = time.time()
        e = self._d.get((ns, key_of(tokens)))
        if e and now - e[2] < self.ttl:
            self._d.move_to_end((ns, key_of(tokens)))
            return e[1]
        if len(tokens) < 2:
            return None
        best, best_score = None, 0.0
        for (n, _), (toks, payload, ts) in self._d.items():
            if n != ns or len(toks) < 2 or now - ts >= self.ttl:
                continue
            score = len(tokens & toks) / len(tokens | toks)
            if score >= self.threshold and score > best_score:
                best, best_score = payload, score
        return best

    def set(self, ns: str, tokens: frozenset[str], payload: dict) -> None:
        if not tokens:
            return
        self._d[(ns, key_of(tokens))] = (tokens, payload, time.time())
        self._d.move_to_end((ns, key_of(tokens)))
        while len(self._d) > self.max_items:
            self._d.popitem(last=False)

    def __len__(self) -> int:
        return len(self._d)
