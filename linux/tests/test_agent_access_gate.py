import unittest
from zoneinfo import ZoneInfo

from weitblick.agent.access_gate import (ALLOWED, AccessDecision, AccessGate, AccessRequest, AgentAccessPolicy,
                                         PromptAnswer)

DENIED = AccessDecision.refused('The user denied access to "Lab PC".')


class FakeScheduler:
    """Timers the test fires by hand."""

    def __init__(self):
        self.timers = []

    def __call__(self, seconds, callback):
        timer = [seconds, callback, True]
        self.timers.append(timer)

        def cancel():
            timer[2] = False
        return cancel

    def fire_all(self):
        for timer in list(self.timers):
            if timer[2]:
                timer[2] = False
                timer[1]()


class Harness:
    def __init__(self, timeout=120.0):
        self.date = 1_792_000_000.0  # 2026-10-14 17:46:40 UTC = 19:46:40 Berlin
        self.shown = []
        self.closed = []
        self.persisted = []
        #: Answers for the next prompts, in order; None = leave it open.
        self.answers = []
        self.scheduler = FakeScheduler()
        self.gate = AccessGate(now=lambda: self.date, timezone=ZoneInfo("Europe/Berlin"), prompt_timeout=timeout,
                               schedule=self.scheduler)
        self.gate.show_prompt = self._show
        self.gate.close_prompt = lambda prompt: self.closed.append(prompt.id)
        self.gate.persist_always_allow = self.persisted.append

    def _show(self, prompt):
        self.shown.append(prompt)
        if self.answers:
            answer = self.answers.pop(0)
            if answer is not None:
                self.gate.answer(prompt.id, answer)

    def request(self, session="s1", connection="lab"):
        return AccessRequest(connection_id=connection, connection_name="Lab PC", host="10.211.55.9",
                             client="claude-code 2.1.306", session_id=session)

    def check(self, session="s1", connection="lab", policy=AgentAccessPolicy.ASK):
        """Starts a check; returns a list that receives the decision."""
        result = []
        self.gate.check(self.request(session, connection), policy, result.append)
        return result

    def decide(self, *args, **kwargs):
        result = self.check(*args, **kwargs)
        assert len(result) == 1, f"no decision yet: {result}"
        return result[0]


class AccessGateTests(unittest.TestCase):
    def test_never_refuses_without_asking(self):
        harness = Harness()
        self.assertEqual(harness.decide(policy=AgentAccessPolicy.NEVER),
                         AccessDecision.refused(AccessGate.never_message("Lab PC")))
        self.assertEqual(harness.shown, [])

    def test_always_allows_without_asking(self):
        harness = Harness()
        self.assertEqual(harness.decide(policy=AgentAccessPolicy.ALWAYS), ALLOWED)
        self.assertEqual(harness.shown, [])

    def test_ask_then_deny_refuses_and_remembers_nothing(self):
        harness = Harness()
        harness.answers = [PromptAnswer.DENY, PromptAnswer.DENY]
        self.assertEqual(harness.decide(), DENIED)
        self.assertEqual(harness.decide(), DENIED)
        self.assertEqual(len(harness.shown), 2)
        self.assertEqual(harness.closed, [prompt.id for prompt in harness.shown])
        self.assertEqual(harness.gate.active_grants, [])

    def test_prompt_names_client_and_connection(self):
        harness = Harness()
        harness.answers = [PromptAnswer.DENY]
        harness.decide()
        request = harness.shown[0].request
        self.assertEqual((request.client, request.connection_name, request.host),
                         ("claude-code 2.1.306", "Lab PC", "10.211.55.9"))

    def test_allow_once_covers_that_session_until_it_ends(self):
        harness = Harness()
        harness.answers = [PromptAnswer.ALLOW_ONCE]
        self.assertEqual(harness.decide("s1"), ALLOWED)
        self.assertEqual(harness.decide("s1"), ALLOWED)
        self.assertEqual(len(harness.shown), 1)
        # Another session on the same connection asks for itself.
        harness.answers = [PromptAnswer.DENY]
        self.assertEqual(harness.decide("s2"), DENIED)
        self.assertEqual(len(harness.shown), 2)
        # Disconnecting ends the grant.
        harness.gate.session_ended("s1")
        harness.answers = [PromptAnswer.DENY]
        self.assertEqual(harness.decide("s1"), DENIED)
        self.assertEqual(len(harness.shown), 3)

    def test_ten_minutes_cover_the_connection_then_ask_again(self):
        harness = Harness()
        harness.answers = [PromptAnswer.ALLOW_TEN_MINUTES]
        self.assertEqual(harness.decide("s1"), ALLOWED)
        harness.date += 9 * 60
        self.assertEqual(harness.decide("s2"), ALLOWED)
        grant = next(grant for grant in harness.gate.active_grants if grant.connection_id == "lab")
        self.assertEqual(grant.until, harness.date + 60)
        self.assertIsNone(grant.session_id)
        harness.date += 60
        harness.answers = [PromptAnswer.DENY]
        prompts_before = len(harness.shown)
        self.assertEqual(harness.decide("s1"), DENIED)
        self.assertEqual(len(harness.shown), prompts_before + 1)
        self.assertEqual(harness.gate.active_grants, [])

    def test_injected_grant_duration_is_used(self):
        harness = Harness()
        harness.gate.timed_grant_duration = 3
        harness.answers = [PromptAnswer.ALLOW_TEN_MINUTES]
        self.assertEqual(harness.decide(), ALLOWED)
        harness.date += 2.9
        self.assertTrue(harness.gate.is_granted(harness.request()))
        harness.date += 0.1
        self.assertFalse(harness.gate.is_granted(harness.request()))

    def test_today_lasts_until_local_midnight(self):
        harness = Harness()
        harness.answers = [PromptAnswer.ALLOW_TODAY]
        self.assertEqual(harness.decide(), ALLOWED)
        grant = harness.gate.active_grants[0]
        # 2026-10-15 00:00 in Berlin (CEST, +02:00) = 2026-10-14 22:00 UTC.
        self.assertEqual(grant.until, 1_792_015_200)
        harness.date = 1_792_015_199
        self.assertTrue(harness.gate.is_granted(harness.request()))
        harness.date = 1_792_015_200
        self.assertFalse(harness.gate.is_granted(harness.request()))

    def test_midnight_across_the_daylight_saving_switch(self):
        harness = Harness()
        # 2026-10-25 01:00 Berlin (CEST); clocks go back at 03:00. Next midnight = 2026-10-26 00:00 CET = 23:00 UTC.
        self.assertEqual(harness.gate.next_midnight(1_792_882_800), 1_792_969_200)

    def test_local_midnight_without_an_injected_time_zone(self):
        gate = AccessGate(schedule=FakeScheduler())
        midnight = gate.next_midnight(1_792_000_000)
        self.assertGreater(midnight, 1_792_000_000)
        self.assertLessEqual(midnight - 1_792_000_000, 25 * 3600)

    def test_always_allow_persists_the_policy(self):
        harness = Harness()
        harness.answers = [PromptAnswer.ALWAYS_ALLOW]
        self.assertEqual(harness.decide(), ALLOWED)
        self.assertEqual(harness.persisted, ["lab"])
        self.assertEqual(harness.gate.active_grants, [])

    def test_revoke_asks_again(self):
        harness = Harness()
        harness.answers = [PromptAnswer.ALLOW_TODAY]
        self.assertEqual(harness.decide(), ALLOWED)
        harness.gate.revoke(harness.gate.active_grants[0].id)
        harness.answers = [PromptAnswer.DENY]
        self.assertEqual(harness.decide(), DENIED)
        self.assertEqual(len(harness.shown), 2)

    def test_parallel_requests_share_one_prompt(self):
        harness = Harness()
        first, second = harness.check("s1"), harness.check("s2")
        third = harness.check("s3", connection="other")
        self.assertEqual(len(harness.shown), 2)
        self.assertEqual({prompt.request.connection_id for prompt in harness.gate.open_prompts}, {"lab", "other"})
        lab = next(prompt for prompt in harness.shown if prompt.request.connection_id == "lab")
        other = next(prompt for prompt in harness.shown if prompt.request.connection_id == "other")
        harness.gate.answer(lab.id, PromptAnswer.ALLOW_TEN_MINUTES)
        harness.gate.answer(other.id, PromptAnswer.DENY)
        self.assertEqual(first + second + third, [ALLOWED, ALLOWED, DENIED])

    def test_allow_once_for_one_session_makes_a_waiting_session_ask_itself(self):
        harness = Harness()
        first, second = harness.check("s1"), harness.check("s2")
        prompt = harness.shown[0]
        harness.answers = [PromptAnswer.DENY]
        harness.gate.answer(prompt.id, PromptAnswer.ALLOW_ONCE)
        self.assertEqual(first, [ALLOWED])
        self.assertEqual(second, [DENIED])
        self.assertEqual(len(harness.shown), 2)

    def test_no_answer_times_out(self):
        harness = Harness(timeout=120)
        result = harness.check()
        self.assertEqual(result, [])
        self.assertEqual(harness.scheduler.timers[0][0], 120)
        harness.scheduler.fire_all()
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].allowed)
        self.assertTrue(result[0].message.startswith("No answer from the user"))
        self.assertIn("2 minutes", result[0].message)
        self.assertEqual(harness.gate.open_prompts, [])
        self.assertEqual(len(harness.closed), 1)

    def test_an_answer_cancels_the_timeout(self):
        harness = Harness()
        result = harness.check()
        harness.gate.answer(harness.shown[0].id, PromptAnswer.ALLOW_ONCE)
        harness.scheduler.fire_all()
        self.assertEqual(result, [ALLOWED])

    def test_cancelling_refuses_open_prompts(self):
        harness = Harness()
        result = harness.check()
        harness.gate.cancel_all_prompts("AI agents were turned off.")
        self.assertEqual(result, [AccessDecision.refused("AI agents were turned off.")])

    def test_on_change_fires_for_prompts_and_grants(self):
        harness = Harness()
        changes = []
        harness.gate.on_change = lambda: changes.append(len(harness.gate.open_prompts))
        harness.check()
        harness.gate.answer(harness.shown[0].id, PromptAnswer.ALLOW_TODAY)
        self.assertGreaterEqual(len(changes), 3)  # prompt opened, grant added, prompt closed
        harness.gate.revoke_all()
        self.assertEqual(harness.gate.active_grants, [])

    def test_timeout_description(self):
        self.assertEqual(AccessGate.describe(120), "2 minutes")
        self.assertEqual(AccessGate.describe(60), "1 minute")
        self.assertEqual(AccessGate.describe(0.5), "0.5 seconds")
        self.assertEqual(AccessGate.describe(1), "1 second")


if __name__ == "__main__":
    unittest.main()
