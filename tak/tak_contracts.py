"""The two bus topics the TAK integration adds. They live here, not in code/afterglow/contracts.py, so
the TAK add-on changes nothing that already exists. To make them official, move these lines into
contracts.py and add the two rows below to docs/CONTRACTS.md, in the same PR.

| topic       | who publishes                         | fields                                                    |
|-------------|---------------------------------------|-----------------------------------------------------------|
| `ag/team`   | tak_link.py, from the TAK phones      | t, uid, callsign, kind (pos / chat / report); pos: x, y, lat, lon; chat: text; report: pin, verdict (ack / check / found / nobody), text |
| `ag/rescue` | tak_dashboard.py, on the ranger's click | t, pin, pts [[x, y], ...] (car park to person, metres); optional secs, label. pts [] takes the route away |

Neither carries case details (name, age, what they carry): the case stays on the laptop, as always.
"""
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent / "code"
if str(CODE) not in sys.path:
    sys.path.insert(0, str(CODE))

from afterglow.contracts import validate as validate_core  # noqa: E402

TEAM = "ag/team"  # what TAK phones send back: position, chat, a field report on a pin
RESCUE = "ag/rescue"  # the ranger sent a ground team: their route, for the TAK phones
REQUIRED = {
    TEAM: {"t", "uid", "callsign", "kind"},
    RESCUE: {"t", "pin", "pts"},
}
TEAM_KINDS = ["pos", "chat", "report"]
# a field team's word on a pin: seen it, asks for the drone, a person, no one. Only the ranger acts on it.
REPORT_VERDICTS = ["ack", "check", "found", "nobody"]


def validate(topic: str, msg: dict) -> list[str]:
    """Problems with a message, [] if fine. The two TAK topics here; every other topic as contracts.py says."""
    if topic not in REQUIRED:
        return validate_core(topic, msg)
    if not isinstance(msg, dict):
        return ["message must be a JSON object"]
    problems = [f"missing {k}" for k in sorted(REQUIRED[topic] - msg.keys())]
    if topic == TEAM and msg.get("kind") not in TEAM_KINDS:
        problems.append(f"bad team kind {msg.get('kind')!r}")
    if topic == TEAM and msg.get("kind") == "report" and (msg.get("verdict") not in REPORT_VERDICTS or not msg.get("pin")):
        problems.append(f"a team report needs pin and verdict {' / '.join(REPORT_VERDICTS)}")
    if topic == RESCUE and not isinstance(msg.get("pts"), list):
        problems.append("rescue pts must be a list of [x, y]")
    return problems
