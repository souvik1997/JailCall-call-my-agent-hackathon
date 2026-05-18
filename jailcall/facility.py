"""Caller-facility info — where the person on the line is physically held.

The caller is at a jail or police station and cannot take a callback at
a personal phone. Attorneys reach them through the facility: by calling
the facility's intake line or by showing up in person during attorney-
visit hours. The agent never asks the caller for a callback number;
this module is the source of truth for how attorneys get back to them.

For the demo, ``current_facility()`` always returns
``San Francisco County Jail`` (Jail #2). Future work: map the
AgentPhone ``data.from`` caller-ID to a real facility via lookup table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Facility:
    """A jail / detention facility the caller is held at."""

    name: str
    phone: str
    address: str
    visit_info: str


# Demo facility. The phone number uses the 555-01xx fictitious range so
# nothing real gets dialled during a demo run — swap with the verified
# attorney/intake line before production.
DEMO_FACILITY: Final[Facility] = Facility(
    name="San Francisco County Jail (Jail #2)",
    phone="+14155550100",
    address="425 7th Street, San Francisco, CA 94103",
    visit_info=(
        "Attorney visits available 24/7 in the professional visiting "
        "room with a valid bar card; in-person visits are the fastest "
        "way to reach the client."
    ),
)


def current_facility() -> Facility:
    """Return the facility for the active caller.

    Today: always the hardcoded demo facility. Future: AgentPhone
    caller-ID → facility lookup, keyed on the inbound ``data.from`` number.
    """
    return DEMO_FACILITY
