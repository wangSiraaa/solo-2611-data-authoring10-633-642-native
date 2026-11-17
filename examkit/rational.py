"""Exact rational arithmetic for the whole project.

No floats are ever used to compare score proportions: every ratio is kept as a
pair of non-negative integers (numerator / denominator), reduced by gcd.
"""

from __future__ import annotations

from math import gcd


def is_int(x) -> bool:
    """True only for genuine ints (``bool`` is rejected on purpose)."""
    return isinstance(x, int) and not isinstance(x, bool)


class Rat:
    """Immutable reduced rational number, denominator always positive."""

    __slots__ = ("n", "d")

    def __init__(self, n: int, d: int = 1):
        if not (is_int(n) and is_int(d)):
            raise TypeError("Rat parts must be plain ints")
        if d == 0:
            raise ZeroDivisionError("zero denominator")
        if d < 0:
            n, d = -n, -d
        g = gcd(abs(n), d)
        if g:
            n //= g
            d //= g
        self.n = n
        self.d = d

    # --- construction / serialisation -------------------------------------
    @staticmethod
    def from_pair(pair) -> "Rat":
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("expected [numerator, denominator]")
        return Rat(pair[0], pair[1])

    def to_pair(self) -> list[int]:
        return [self.n, self.d]

    # --- arithmetic --------------------------------------------------------
    def __add__(self, other: "Rat") -> "Rat":
        return Rat(self.n * other.d + other.n * self.d, self.d * other.d)

    def __mul__(self, other: "Rat") -> "Rat":
        return Rat(self.n * other.n, self.d * other.d)

    def __truediv__(self, other: "Rat") -> "Rat":
        if other.n == 0:
            raise ZeroDivisionError("division by zero rational")
        return Rat(self.n * other.d, self.d * other.n)

    # --- comparison --------------------------------------------------------
    def _key(self):
        return self.n, self.d

    def __eq__(self, other) -> bool:
        if not isinstance(other, Rat):
            return NotImplemented
        return self.n * other.d == other.n * self.d

    def __lt__(self, other: "Rat") -> bool:
        return self.n * other.d < other.n * self.d

    def __le__(self, other: "Rat") -> bool:
        return self.n * other.d <= other.n * self.d

    def __gt__(self, other: "Rat") -> bool:
        return self.n * other.d > other.n * self.d

    def __ge__(self, other: "Rat") -> bool:
        return self.n * other.d >= other.n * self.d

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        return f"Rat({self.n}, {self.d})"

    def __str__(self) -> str:
        return f"{self.n}/{self.d}" if self.d != 1 else str(self.n)


ZERO = Rat(0)
ONE = Rat(1)
