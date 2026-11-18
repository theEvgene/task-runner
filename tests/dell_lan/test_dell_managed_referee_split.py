"""#89 on the current #90 split referee: one module surface, one admission.

The Dell managed submit is only worth its receipt while the reservation it
proves comes from the one queue-side referee the worker also reads. Here the
split is pinned: the producer imports ``job_reservations`` (the lock and the
admission decision), which re-exports the record shape, normalization, and
authenticated reads from ``job_reservation_records`` and the queue-local
quota authority from ``job_capacity``. The second case proves a *new*
managed footprint inherits that referee's fail-closed quota admission, and
that a refusal publishes neither the reservation nor the job.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from dell_fixture import SCRIPTS
from test_dell_managed_submit import (
    Fixture, job_ids, reservation_ids, submit, transfer_request,
)
import dell_managed_submit  # noqa: E402  (the producer under test)


class SplitRefereeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_producer_binds_the_current_split_referee_modules(self):
        lifecycle, referee = dell_managed_submit._reservations()
        worker = (Path(SCRIPTS).parent / "lenovo_worker").resolve()
        self.assertEqual(Path(lifecycle.__file__).resolve(),
                         worker / "job_lifecycle.py")
        self.assertEqual(Path(referee.__file__).resolve(),
                         worker / "job_reservations.py")
        import job_capacity
        import job_reservation_records
        # One import surface: every name the producer uses is the split
        # module's own object, so it cannot drift from the worker's referee.
        self.assertIs(referee.normalize_resources,
                      job_reservation_records.normalize_resources)
        self.assertIs(referee.read, job_reservation_records.read)
        self.assertIs(referee.configured_capacity_limits,
                      job_capacity.configured_capacity_limits)
        self.assertEqual(referee.referee_publish.__module__, "job_reservations")
        self.assertEqual(referee.reserve_and_submit.__module__,
                         "job_reservations")

    def test_new_footprint_inherits_the_queue_quota_admission(self):
        fx = self.fx
        request = transfer_request(fx, "aaaa0000000000c1")
        authority = fx.queue / "capacity-quotas.json"
        authority.parent.mkdir(parents=True, exist_ok=True)
        authority.write_text("{not json\n", encoding="utf-8")

        # A broken queue authority refuses a new footprint before publication.
        code, output, receipt = submit(fx, request)
        self.assertEqual(code, 2, output)
        self.assertIn("reservation refused", output)
        self.assertIn("capacity quota configuration", output)
        self.assertIsNone(receipt)
        self.assertEqual(job_ids(fx), [])
        self.assertEqual(reservation_ids(fx), [])

        # The same request is admitted once the authority is readable: the
        # refusal came from the admission path, not from the request.
        authority.unlink()
        code, output, receipt = submit(fx, request)
        self.assertEqual(code, 0, output)
        self.assertEqual(receipt["outcome"], "reserved")
        self.assertEqual(job_ids(fx), [receipt["jobId"]])
        self.assertEqual(reservation_ids(fx), [receipt["jobId"]])


if __name__ == "__main__":
    unittest.main()
