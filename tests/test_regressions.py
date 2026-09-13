"""The Lemma loop, closed: a failure the auditor found once stays a test forever.

Every file in evals/regressions/ is a real trace that broke an agent's instruction. Each is replayed
through the current auditor, which must still flag the same instruction - so a later change to the
auditor can never quietly stop seeing a failure that already happened once.

Also here: the storage contract the whole system leans on - one external action, at most once, even
when two workers ask at the same moment.
"""

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from warmpath import audit, store
from warmpath.core import Leads, Ledger, Run

CASES = sorted((Path(__file__).resolve().parent.parent / "evals" / "regressions").glob("*.json"))


@pytest.mark.parametrize("case", CASES, ids=[c.stem for c in CASES])
def test_a_recorded_failure_is_still_caught(case):
    d = json.loads(case.read_text(encoding="utf-8"))
    run = Run.from_dict(d["trace"])
    assert d["invariant"] in {v.invariant for v in audit.audit_run(run)}, \
        f"the auditor no longer catches {d['invariant']}: {d['detail']}"


def test_two_workers_asking_at_once_get_one_yes(tmp_path):
    s = store.SQLiteStore(tmp_path / "race.db")
    answers = []
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        answers.append(s.ledger_begin("send:johnacme", f"run{i}", "send"))
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert answers.count(True) == 1


def test_a_failed_action_can_be_retried_but_a_done_one_cannot(tmp_path):
    led = Ledger(tmp_path / "l.db")
    assert led.begin("send:x", "r1", "send")
    led.failed("send:x", "network down")
    assert led.begin("send:x", "r2", "send")
    led.done("send:x", "sent")
    assert not led.begin("send:x", "r3", "send")


def test_lead_rows_keep_columns_and_extras(tmp_path):
    leads = Leads(tmp_path / "l.db")
    leads.save("john@acme.example", {"email": "john@acme.example", "stage": "Emailed", "offered": {"slot1": {"label": "Mon"}},
                                     "custom_field": "kept"})
    leads.save("john@acme.example", {"stage": "Times offered"})
    got = leads.get("john@acme.example")
    assert got["stage"] == "Times offered" and got["offered"]["slot1"]["label"] == "Mon" and got["custom_field"] == "kept"
