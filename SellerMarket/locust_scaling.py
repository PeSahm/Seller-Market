"""Pure helper: how many locust users to pin per config section.

locust's default *weight-based* user distribution starves some user-classes when
the total user count is close to the number of classes. Live evidence: a
14-section Tebyan stack with ``users=42`` left one account's 4 classes at **0**
users → those customers never fired, while another class fired 10k+ times. We
pin each section's user-class with ``fixed_count = users // sections`` so locust
spawns an EQUAL, guaranteed share per section — no starvation.

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations


def per_section_user_count(users: int, num_sections: int) -> int:
    """Users to pin to EACH section's user-class via locust ``fixed_count``.

    ``max(1, users // sections)`` — every section gets at least one user and the
    same integer share, so none is starved to zero. With the mgmt auto-scale
    (``users = N × sections``) this is exactly ``N`` per section. Returns 1 for
    the degenerate / bad-input cases (no sections, non-numeric input).
    """
    try:
        u = int(users)
        n = int(num_sections)
    except (TypeError, ValueError):
        return 1
    if n <= 0:
        return 1
    return max(1, u // n)
