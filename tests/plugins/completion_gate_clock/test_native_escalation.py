"""Live-shaped escalation against a temporary estate: real child, real `hermes send` stack.

Nothing is stubbed between ``escalation.send`` and aiohttp's POST: the child runs with
the sender profile's own .env/config, resolves the bridge target, passes relay egress
authorization, and posts through the Slack plugin's standalone sender. Only the HTTP
response is synthetic (no network). Proves a durable receipt and at-most-once.
"""
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from test_metrics import pkg  # noqa: F401  (loads metricsgate)
from metricsgate import escalation
import slack_fixture


class SlackBridgeEscalation(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = slack_fixture.estate(self.td.name)
        self.profiles = {n: self.root / 'profiles' / n for n in ('generalist', 'gurney', 'cody')}

    def send(self, event, mode='ok', seconds=20):
        # Only the child's entry script is swapped for the offline-network wrapper, which
        # then runs the real escalation.child_main.
        import subprocess as sp
        real = sp.run
        with patch('hermes_cli.profiles.get_profile_dir', side_effect=self.profiles.__getitem__), \
                patch('tools.environments.local.served_profile_child_env',
                      return_value=slack_fixture.child_env(self.root)), \
                patch.object(sp, 'run', side_effect=lambda argv, **kw: real(
                    [argv[0], str(slack_fixture.CHILD), argv[1], mode], **kw)):
            return escalation.send(event, deadline=time.monotonic() + seconds)

    def test_receipt_to_each_bridge_and_at_most_once(self):
        for target, channel, profile in (('gurney', 'C0BTH5ZQH61', 'generalist'),
                                         ('generalist', 'C0BTEFMAAJX', 'cody')):
            event = {'event_id': 4139, 'profile': profile, 'target': target,
                     'task_id': '20260924_064429_1e3e717f', 'reason': 'retry_ceiling', 'block_count': 2}
            first = self.send(event)
            self.assertEqual((first['status'], first['channel']), ('sent', channel))
            self.assertTrue(first['message_id'])
            receipt = escalation.read_receipt(self.profiles[target], first['delivery_id'])
            self.assertEqual(receipt['status'], 'sent')
            self.assertEqual(receipt['message'], escalation.message(event))
            self.assertEqual(receipt['message_id'], first['message_id'])
            # Re-send of the identical escalation returns the durable receipt, posts nothing.
            again = self.send(event)
            self.assertEqual(again, first)
        posted = slack_fixture.requests(self.root)
        self.assertEqual([r['json']['channel'] for r in posted], ['C0BTH5ZQH61', 'C0BTEFMAAJX'])
        self.assertTrue(all(r['url'] == 'https://slack.com/api/chat.postMessage' for r in posted))
        self.assertTrue(all(r['bearer'] == 'Bearer xoxb-offline-fixture' for r in posted))
        print(json.dumps({'posted': len(posted), 'receipts': {
            t: [r['status'] for r in slack_fixture.receipts(self.root, t)] for t in ('gurney', 'generalist')}}))

    def test_platform_refusal_is_unavailable_and_rearms(self):
        event = {'event_id': 1, 'profile': 'generalist', 'target': 'gurney', 'task_id': 'c',
                 'reason': 'retry_ceiling', 'block_count': 2}
        self.assertEqual(self.send(event, mode='refused')['status'], 'unavailable')

    def test_unresolved_reservation_is_never_resent(self):
        event = {'event_id': 2, 'profile': 'generalist', 'target': 'gurney', 'task_id': 'c',
                 'reason': 'repeat_reason', 'block_count': 1}
        text = escalation.message(event)
        key = escalation.delivery_id('C0BTH5ZQH61', text)
        path = escalation.receipt_path(self.profiles['gurney'], key)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'status': 'reserved', 'channel': 'C0BTH5ZQH61', 'message': text}))
        self.assertEqual(self.send(event)['status'], 'unverified')
        self.assertEqual(slack_fixture.requests(self.root), [])

    def test_unknown_target_refused(self):
        with self.assertRaises(RuntimeError):
            self.send({'event_id': 3, 'profile': 'x', 'target': 'lars', 'task_id': 'c',
                       'reason': 'retry_ceiling', 'block_count': 2})
        self.assertEqual(slack_fixture.requests(self.root), [])
