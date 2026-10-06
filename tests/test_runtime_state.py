import unittest

from ocdeck.runtime_state import RuntimeProducer, reconcile_runtime_producers


class RuntimeReconciliationTests(unittest.TestCase):
    def producer(self, *, owns=False, updated=100, status="busy", questions=None,
                 include_questions=True, permissions=None):
        payload = {
            "statuses": [{"sessionID": "ses_shared", "status": status, "updated": updated}],
            "permissions": permissions or [],
        }
        if include_questions:
            payload["questions"] = questions if questions is not None else []
        return RuntimeProducer(payload, frozenset({"ses_shared"}) if owns else frozenset(), 9000)

    def question(self, updated=100):
        return {"id": "que_old", "sessionID": "ses_shared", "question": "Old question", "updated": updated}

    def test_resumed_owner_supersedes_abandoned_question_in_another_instance(self):
        old = self.producer(questions=[self.question()])
        resumed = self.producer(owns=True, updated=200)
        for producers in ((old, resumed), (resumed, old)):
            state = reconcile_runtime_producers(producers)
            self.assertEqual(state.statuses, {"ses_shared": "busy"})
            self.assertEqual(state.permissions, {})

    def test_missing_malformed_or_unowned_snapshots_do_not_clear_questions(self):
        old = self.producer(questions=[self.question()])
        for resumed in (
            self.producer(owns=True, updated=200, include_questions=False),
            self.producer(owns=True, updated=200, questions=[None]),
            self.producer(owns=False, updated=200),
        ):
            state = reconcile_runtime_producers((old, resumed))
            self.assertEqual(state.permissions["ses_shared"][0]["id"], "que_old")

    def test_newer_or_still_present_question_is_retained(self):
        for original, resumed in (
            (self.producer(updated=300, questions=[self.question(300)]), self.producer(owns=True, updated=200)),
            (self.producer(questions=[self.question()]), self.producer(owns=True, updated=200, questions=[self.question()])),
        ):
            state = reconcile_runtime_producers((original, resumed))
            self.assertEqual(state.permissions["ses_shared"][0]["id"], "que_old")

    def test_file_heartbeat_is_not_an_authoritative_session_update(self):
        old = self.producer(questions=[self.question()])
        resumed = RuntimeProducer({"statuses": [{"sessionID": "ses_shared", "status": "busy"}],
                                   "questions": [], "permissions": []}, frozenset({"ses_shared"}), 9000)
        self.assertIn("ses_shared", reconcile_runtime_producers((old, resumed)).permissions)

    def test_request_authority_is_per_kind_and_session(self):
        old = self.producer(questions=[self.question()], permissions=[{
            "id": "per_old", "sessionID": "ses_shared", "permission": "bash", "updated": 100,
        }])
        resumed = self.producer(owns=True, updated=200, include_questions=False)
        state = reconcile_runtime_producers((old, resumed))
        self.assertEqual([item["id"] for item in state.permissions["ses_shared"]], ["que_old"])
        other = RuntimeProducer(resumed.payload, frozenset({"ses_other"}), 200)
        self.assertEqual(len(reconcile_runtime_producers((old, other)).permissions["ses_shared"]), 2)
