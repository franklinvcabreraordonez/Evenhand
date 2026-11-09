"""The checks that can be made before a purchase, from the mandate alone.

These are the hard constraints of evenhand.adjudicator, evaluated early. A purchase this blocks is one the
adjudicator would call exceeded_mandate for the same reason, and tests keep the two in step."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import List, Optional

from ..intent import IntentMatcher, RobustIntentMatcher
from ..mandate import Mandate

PURCHASE = "purchase"


@dataclass
class Decision:
    allowed: bool
    violations: List[str] = field(default_factory=list)
    intent_score: Optional[float] = None

    def explain(self) -> str:
        return "; ".join(self.violations)


class Policy:
    """Checks a purchase against a mandate and keeps count of what has been spent under it."""

    def __init__(self, mandate: Mandate, spent: float = 0.0, mode: str = "hard", matcher: Optional[IntentMatcher] = None,
                 hi: float = 0.75):
        self.mandate = mandate
        self.mode = mode
        self.hi = hi
        self._matcher = matcher or RobustIntentMatcher()
        self._spent = spent
        self._reserved = 0.0
        self._lock = threading.Lock()

    @property
    def spent(self) -> float:
        return self._spent

    def check(self, merchant: str, amount: float, now: float, category: str = "general", description: str = "",
              action: str = PURCHASE, currency: Optional[str] = None, reserve: bool = False) -> Decision:
        """Whether a purchase is allowed. With reserve=True, an allowed purchase's amount is held against the
        total cap until commit() or release(), so two purchases at once cannot both slip under it."""
        m, c = self.mandate, self.mandate.constraints
        problems: List[str] = []
        if action not in m.actions:
            problems.append(f"the mandate does not allow '{action}' actions")
        if not (m.issued_at <= now <= m.expires_at):
            problems.append("the mandate is not valid at this time")
        if currency is not None and currency != c.currency:
            problems.append(f"the mandate is in {c.currency}, not {currency}")
        if c.max_per_txn is not None and amount > c.max_per_txn:
            problems.append(f"{amount:.2f} is above the per-purchase limit of {c.max_per_txn:.2f}")
        if c.merchants_allow is not None and merchant not in c.merchants_allow:
            problems.append(f"merchant '{merchant}' is not on the user's list")
        if merchant in c.merchants_deny:
            problems.append(f"merchant '{merchant}' is on the user's deny list")
        if c.categories_allow is not None and category not in c.categories_allow:
            problems.append(f"category '{category}' is not one the user allowed")
        if c.step_up_above is not None and amount > c.step_up_above:
            problems.append(f"{amount:.2f} is above {c.step_up_above:.2f}, which needs the user's confirmation")
        score: Optional[float] = None
        with self._lock:
            if c.max_total is not None and self._spent + self._reserved + amount > c.max_total:
                total = self._spent + self._reserved + amount
                problems.append(f"this would bring spending to {total:.2f}, above the total limit of {c.max_total:.2f}")
            if self.mode == "strict" and not problems:
                score = self._matcher.score(m.intent, [{"desc": description, "price": amount}])
                if score < self.hi:
                    problems.append(f"the item does not clearly match what the user asked for (match score {score:.2f})")
            if not problems and reserve:
                self._reserved += amount
        return Decision(not problems, problems, score)

    def commit(self, amount: float) -> None:
        """A reserved purchase went through."""
        with self._lock:
            self._reserved = max(0.0, self._reserved - amount)
            self._spent += amount

    def release(self, amount: float) -> None:
        """A reserved purchase did not happen."""
        with self._lock:
            self._reserved = max(0.0, self._reserved - amount)

    def add_spent(self, amount: float) -> None:
        """Spending that was not reserved first, such as a purchase observed without enforcement."""
        with self._lock:
            self._spent += amount