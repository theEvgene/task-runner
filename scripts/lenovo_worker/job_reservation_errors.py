"""The one refusal type of the reservation referee boundary.

Every sibling module raises this same class, so a caller catches one error
for "absent, unprovable, or conflicting" without knowing which seam
produced it. It subclasses ``ValueError`` so an existing ``except
ValueError`` keeps its meaning.
"""

from __future__ import annotations


class ReservationRefused(ValueError):
    """The reservation is absent, unprovable, or conflicts."""
