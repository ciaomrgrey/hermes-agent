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

    def test_unconfigured_sender_refused_before_io_is_unavailable(self):
        # No Slack token in the sender profile: the native stack refuses before dispatch.
        (self.profiles['generalist'] / '.env').write_text('')
        event = {'event_id': 5, 'profile': 'generalist', 'target': 'gurney', 'task_id': 'c',
                 'reason': 'retry_ceiling', 'block_count': 2}
        self.assertEqual(self.send(event)['status'], 'unavailable')
        self.assertEqual(slack_fixture.requests(self.root), [])


class GateToNativeSenderComposition(unittest.TestCase):
    """Real Gate -> real escalation child -> native send_message_tool -> Slack standalone
    sender, two turns of the same chain and reason. Counts physical POST attempts."""

    def setUp(self):
        from metricsgate import gate
        self.gate_module = gate
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = slack_fixture.estate(self.td.name)
        self.profiles = {n: self.root / 'profiles' / n for n in ('generalist', 'gurney', 'cody')}

    def two_turns(self, mode):
        import subprocess as sp
        real = sp.run
        receipts = []

        def send(event):
            with patch('hermes_cli.profiles.get_profile_dir', side_effect=self.profiles.__getitem__), \
                    patch('tools.environments.local.served_profile_child_env',
                          return_value=slack_fixture.child_env(self.root)), \
                    patch.object(sp, 'run', side_effect=lambda argv, **kw: real(
                        [argv[0], str(slack_fixture.CHILD), argv[1], mode], **kw)):
                receipts.append(escalation.send(event, deadline=time.monotonic() + 20))
                return receipts[-1]

        settings = {'enabled': True, 'db_path': str(self.root / 'gate.db'), 'max_blocks': 0}
        g = self.gate_module.Gate(settings, extract=lambda _: [{
            'claim': 'file ready', 'artefact_kind': 'file', 'artefact_ref': str(self.root / 'missing')}],
            escalate=send)
        for turn in ('one', 'two'):
            g.evaluate('answer', profile='generalist', task_id='same-chain', turn_id=turn)
        actions = [r['action'] for r in g.events()]
        persisted = [r['status'] for r in slack_fixture.receipts(self.root, 'gurney')]
        return receipts, actions, persisted, len(slack_fixture.requests(self.root))

    def assert_single_attempt(self, mode, status):
        receipts, actions, persisted, posts = self.two_turns(mode)
        self.assertEqual(posts, 1, mode)
        self.assertEqual([r['status'] for r in receipts], [status])
        self.assertEqual(persisted, [status])
        self.assertEqual(actions, ['escalation_pending', 'fail_open', 'escalation_' + status, 'fail_open'])

    def test_response_lost_after_post_is_unverified_and_never_resent(self):
        self.assert_single_attempt('lost', 'unverified')

    def test_connection_dropped_after_request_is_unverified_and_never_resent(self):
        self.assert_single_attempt('reset', 'unverified')

    def test_success_control_sends_once(self):
        self.assert_single_attempt('ok', 'sent')

    def test_refusal_control_rearms(self):
        receipts, actions, persisted, posts = self.two_turns('refused')
        self.assertEqual(posts, 2)
        self.assertEqual([r['status'] for r in receipts], ['unavailable', 'unavailable'])
        self.assertEqual(persisted, ['unavailable', 'unavailable'])
        self.assertEqual(actions, ['escalation_pending', 'fail_open', 'escalation_unavailable'] * 2)


class Classification(unittest.TestCase):
    C = 'C0BTH5ZQH61'

    def test_table(self):
        cases = [
            ({'success': True, 'chat_id': self.C, 'message_id': '1.2'}, True, 'sent'),
            ({'success': True, 'chat_id': 'C0OTHER', 'message_id': '1.2'}, True, 'unverified'),
            ({'success': True, 'skipped': 'empty_text'}, True, 'unverified'),
            ({'error': 'Slack API error: channel_not_found'}, True, 'unavailable'),
            ({'error': 'Slack send failed: response lost'}, True, 'unverified'),
            ({'error': 'Send failed: ServerDisconnectedError'}, True, 'unverified'),
            ({'error': 'anything at all'}, True, 'unverified'),
            ({'error': 'Platform slack not configured'}, False, 'unavailable'),
            ({'error': 'x', 'partial_success': True}, True, 'unverified'),
            ({}, True, 'unverified'),
            (None, False, 'unverified'),
        ]
        for result, dispatched, want in cases:
            self.assertEqual(escalation.classify(result, dispatched, self.C), want, (result, dispatched))
