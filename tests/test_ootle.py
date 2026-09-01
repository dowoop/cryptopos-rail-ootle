"""Ootle payment tests from recorded esmeralda 0.39.3 answers."""

import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import cryptopos_rail_ootle.chain as chain
from cryptopos_rail_ootle.chain import OotleReader
from cryptopos_core.errors import InvalidRailPlugin, RailProviderError
from cryptopos_rail_ootle import (
	MAX_SALE_REF_BYTES,
	OOTLE_DEPOSIT_TOPIC,
	OOTLE_PAYMENT_TOPIC,
	OOTLE_XTR_RESOURCE,
	OotleEsmeralda,
	_event_replay,
	_finalized_epoch,
	_summary_field,
)
from cryptopos_core.plugin import (
	ADDRESS_VALIDATION,
	CHARGE_CAPABILITIES,
	NEEDS_REVIEW,
	OBSERVATION,
	PAYMENT_REQUEST,
	PENDING,
	SETTLED,
	SETTLEMENT,
	ObservationBatch,
	PaymentIntent,
	RecipientBaseline,
	TransferObservation,
)

ENDPOINT = "https://ootle.example"
# A SYNTHETIC ACCOUNT, and it can be synthetic because an Ootle account
# address carries no checksum -- `_ACCOUNT` matches
# `(account|component)_[0-9a-f]{32,64}` and nothing else. It used to be the
# real merchant account these tests were written against, which was a live
# testnet address and therefore public, but publishing it here would tie this
# repository to it for no test benefit at all.
ACCOUNT = "component_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
VAULT = "vault_eec5267fad8a680632b447214a0728ec7d6aa8d275c81a377c2cd5296a387518"
TX = "157954d6ee23d88a450d2c544d77509f6a8912a141d195dc6c61ff6c10d76696"
TX_TWO = "2" * 64
TX_THREE = "3" * 64
FINALIZED = "2026-08-31 03:34:19.0"
FINALIZED_EPOCH = 1_788_147_259


def entry(hexed):
	return {"value": {"hex": hexed}}


def account_body():
	return {
		"substate": {
			"Component": {
				# The live state is a CBOR map pair: tag 131 resource bytes,
				# tag 132 vault bytes. The JSON projection leaves those bytes
				# in the hex values that _walk_for_resource resolves.
				"body": {
					"state": [
						[
							{"tag": 131, **entry(OOTLE_XTR_RESOURCE.removeprefix("resource_"))},
							{"tag": 132, **entry(VAULT.removeprefix("vault_"))},
						]
					]
				},
			}
		}
	}


def vault_body(amount="999997692", kind="Stealth"):
	container = {kind: {"revealed_amount": amount}}
	return {"substate": {"Vault": {"resource_container": container}}}


def event_frame(
	event_id,
	transaction_id=TX,
	amount="1000000000",
	*,
	resource=OOTLE_XTR_RESOURCE,
	vault=VAULT,
	topic=OOTLE_DEPOSIT_TOPIC,
	line_ending="\n",
):
	body = {
		"transaction_id": transaction_id,
		"event": {
			"substate_id": vault,
			"template_address": "0" * 64,
			"payload": {
				"amount": amount,
				"resource_address": resource,
				"resource_type": "Stealth",
			},
		},
	}
	lines = [
		f"event: {topic}",
		f"id: {event_id}",
		f"data: {json.dumps(body, separators=(',', ':'))}",
		"",
		"",
	]
	return line_ending.join(lines).encode()


class Response(io.BytesIO):
	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.close()


class Transport:
	def __init__(
		self,
		*,
		network="esmeralda",
		epoch=10,
		balance="999997692",
		kind="Stealth",
		streams=None,
		transactions=None,
		error=None,
	):
		self.network = network
		self.epoch = epoch
		self.balance = balance
		self.kind = kind
		self.streams = streams or {}
		self.transactions = transactions or {}
		self.error = error
		self.calls = []

	def __call__(self, request, timeout=None):
		if self.error is not None:
			raise self.error
		url = request.full_url
		self.calls.append((url, timeout, request.get_header("Accept")))
		parts = urllib.parse.urlsplit(url)
		if parts.path.endswith("/network"):
			body = {"network": self.network, "epoch": self.epoch}
			return Response(json.dumps(body).encode())
		if parts.path.endswith("/substates/" + ACCOUNT):
			return Response(json.dumps(account_body()).encode())
		if parts.path.endswith("/substates/" + VAULT):
			return Response(json.dumps(vault_body(self.balance, self.kind)).encode())
		if parts.path.endswith("/transactions/events/stream"):
			after_id = int(urllib.parse.parse_qs(parts.query)["after_id"][0])
			body = self.streams.get(after_id, b":\n")
			if body is None:
				# WHAT THE LIVE INDEXER DOES when there is nothing after the
				# cursor: it holds the connection open and sends nothing, so
				# the read hits its own timeout with an empty buffer. That is
				# an ANSWER -- the endpoint was reached and had nothing more --
				# and `chain._get_sse` returns the empty bytes rather than
				# re-raising, because it tracks whether the connection was
				# established. `None` in a stream table means exactly that.
				return Response(b"")
			if body == "unreachable":
				# The other kind of nothing: never connected. This is what must
				# stay a refusal, and conflating the two is what let a 503 end
				# a drain early and settle a sale on money that predated it.
				raise urllib.error.URLError("connection refused")
			return Response(body)
		transaction_id = parts.path.rsplit("/", 1)[-1]
		if "/transactions/" in parts.path and transaction_id in self.transactions:
			return Response(json.dumps(self.transactions[transaction_id]).encode())
		raise OSError("unmapped " + url)


class OotleRailTest(unittest.TestCase):
	def setUp(self):
		self.rail = OotleEsmeralda()

	def configuration(self):
		return {"endpoint": ENDPOINT, "timeout_seconds": 2}

	def baseline(self, transport=None):
		transport = transport or Transport()
		with mock.patch("cryptopos_rail_ootle.chain._urlopen", transport):
			return self.rail.capture_baseline(ACCOUNT, self.configuration())

	def intent(self, baseline, amount=1_000_000_000, expires=FINALIZED_EPOCH + 60):
		return PaymentIntent(
			"sale-1",
			self.rail.key,
			ACCOUNT,
			amount,
			FINALIZED_EPOCH - 60,
			expires,
			baseline=baseline,
		)

	def observe(self, intent, transport, previous=None):
		with mock.patch("cryptopos_rail_ootle.chain._urlopen", transport):
			return self.rail.observe(intent, self.configuration(), previous)

	def test_the_rail_declares_the_complete_charge_path_without_upgrading_binding(self):
		self.assertEqual(self.rail.capabilities, CHARGE_CAPABILITIES)
		self.assertEqual(
			self.rail.binding_category,
			"not-unconditional",
			"transaction attribution must not be mislabeled as per-sale receiving",
		)
		self.assertIsNot(OotleReader, OotleEsmeralda)

	def test_readiness_verifies_esmeralda_and_has_no_obsolete_refusals(self):
		with mock.patch("cryptopos_rail_ootle.chain._urlopen", Transport()):
			readiness = self.rail.readiness(self.configuration())
		self.assertTrue(readiness.chargeable)
		self.assertEqual(readiness.ready, CHARGE_CAPABILITIES)
		self.assertEqual(readiness.unavailable, ())
		self.assertEqual(readiness.reason_for(PAYMENT_REQUEST), "")
		self.assertEqual(readiness.reason_for(SETTLEMENT), "")

		with mock.patch("cryptopos_rail_ootle.chain._urlopen", Transport(network="localnet")):
			wrong = self.rail.readiness(self.configuration())
		self.assertNotIn(OBSERVATION, wrong.ready)
		self.assertIn("esmeralda", wrong.reason_for(OBSERVATION))
		self.assertIn(ADDRESS_VALIDATION, wrong.ready)
		self.assertIn(PAYMENT_REQUEST, wrong.ready)
		self.assertIn(SETTLEMENT, wrong.ready)

	def test_baseline_captures_the_last_historical_event_id_and_live_balance(self):
		transport = Transport(
			streams={0: event_frame(247500, amount="7") + b": keepalive\n"},
		)
		baseline = self.baseline(transport)
		self.assertEqual(baseline.tip, 247500)
		self.assertEqual(baseline.balance_native, 999997692)
		streams = [url for url, _timeout, _accept in transport.calls if "events/stream" in url]
		queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(url).query) for url in streams]
		self.assertEqual(queries[0]["substate_id"], [VAULT])
		self.assertEqual(queries[0]["topic"], [OOTLE_DEPOSIT_TOPIC])
		# DRAINED, not read once. The first page answered and the second
		# confirmed there was nothing after it; a baseline is only the tip if
		# something asked whether anything followed.
		self.assertEqual([query["after_id"] for query in queries], [["0"], ["247500"]])
		_url, timeout, accept = transport.calls[-1]
		self.assertEqual((timeout, accept), (2, "text/event-stream"))

	def test_a_baseline_is_the_end_of_the_history_not_the_end_of_one_read(self):
		"""The defect: a short read made "nothing before this is mine" false.

		`chain._get_sse` returns what it has when the budget runs out, which is
		right for observation and wrong here. A shared account with more
		history than four seconds of replay used to hand back a cursor in the
		MIDDLE of its past, and every deposit after that cursor -- money that
		arrived before the sale existed -- then looked like a payment for it.
		"""
		transport = Transport(
			streams={
				0: event_frame(10, amount="1"),
				10: event_frame(20, TX_TWO, "1"),
				20: event_frame(30, TX_THREE, "1"),
			},
		)
		self.assertEqual(self.baseline(transport).tip, 30)

	def test_a_history_that_never_ends_refuses_rather_than_guessing(self):
		"""A baseline that cannot be established is not a baseline of zero."""
		endless = {identifier: event_frame(identifier + 1) for identifier in range(0, 40)}
		with self.assertRaises(RailProviderError) as refused:
			self.baseline(Transport(streams=endless))
		self.assertIn("no baseline can be trusted", refused.exception.reason)

	def test_a_stream_that_ends_in_silence_still_drains(self):
		"""What esmeralda ACTUALLY does, measured 2026-08-31.

		`_get_sse` documents a `:` idle comment as the replay boundary. On the
		live indexer that comment never arrives: a read from `after_id=0`
		returned five events in 4.56 s ending on a complete data frame, and the
		next read from that cursor returned NO BYTES in 4.34 s. The first
		version of the drain treated the second as a failure and refused every
		baseline on the deployment it was written for.

		Silence is end-of-history only AFTER a page has answered, which is what
		the next test is the control for.
		"""
		transport = Transport(streams={0: event_frame(41, amount="3")})
		transport.streams[41] = None            # a read that returns nothing
		baseline = self.baseline(transport)
		self.assertEqual(baseline.tip, 41)

	def test_an_endpoint_that_is_simply_down_is_still_a_refusal(self):
		"""THE CONTROL, and the distinction the whole design rests on.

		"The endpoint had nothing more" and "the endpoint did not answer" are
		both an absence of bytes, and for one afternoon this code treated them
		as the same value. A review reproduced what that cost: an HTTP 503 on
		the drain's second page ended the replay early, and a deposit made
		before the sale existed then settled it.

		`chain._get_sse` separates them by whether the connection was ever
		established, so an unreachable endpoint can never look like an empty
		history.
		"""
		transport = Transport(streams={0: "unreachable"})
		with self.assertRaises(RailProviderError) as refused:
			self.baseline(transport)
		self.assertIn("event stream could not be read", refused.exception.reason)

	def test_a_transport_failure_part_way_through_a_drain_refuses(self):
		"""The reproduced defect, pinned as a regression.

		Page one answers, page two fails. Accepting page one's cursor as the
		end of the history is what let pre-existing money settle a new sale --
		so the baseline must refuse rather than return a cursor it cannot
		stand behind.
		"""
		transport = Transport(streams={0: event_frame(100, amount="1"), 100: "unreachable"})
		with self.assertRaises(RailProviderError) as refused:
			self.baseline(transport)
		self.assertIn("event stream could not be read", refused.exception.reason)

	def test_a_stream_that_answers_with_nonsense_refuses_at_every_attempt(self):
		"""Silence and nonsense are different answers and must not collapse."""
		transport = Transport(streams={0: event_frame(41), 41: b"id: 42\ndata: {}\n\n"})
		with self.assertRaises(RailProviderError) as refused:
			self.baseline(transport)
		self.assertIn("event, id, and data", refused.exception.reason)

	def test_the_page_bound_is_exactly_twelve_and_both_sides_are_asserted(self):
		"""Where the refusal starts, not merely that one exists somewhere.

		Eleven pages of history plus the empty page that ends them is twelve
		reads and must succeed; twelve pages that keep answering must refuse.
		Without both sides the bound can drift by one and nothing notices.
		"""
		eleven = {identifier: event_frame(identifier + 1) for identifier in range(0, 11)}
		self.assertEqual(self.baseline(Transport(streams=eleven)).tip, 11)

		twelve = {identifier: event_frame(identifier + 1) for identifier in range(0, 12)}
		with self.assertRaises(RailProviderError):
			self.baseline(Transport(streams=twelve))

	def test_observe_returns_the_recorded_attributed_final_deposit(self):
		baseline = self.baseline()
		transport = Transport(
			streams={0: event_frame(247574) + b":\n"},
			transactions={TX: {"transaction": {"created_at": FINALIZED, "summary": {"outcome": "Commit", "finalized_at": FINALIZED}}}},
		)
		observations = self.observe(self.intent(baseline), transport)
		self.assertTrue(observations.complete)
		self.assertEqual((observations.baseline_tip, observations.tip), (0, 247574))
		self.assertEqual(observations.unattributed_native, 0)
		self.assertEqual(observations.warnings, ())
		self.assertEqual(
			observations.transfers,
			(TransferObservation(TX, 1_000_000_000, True, 1, block_time_epoch=FINALIZED_EPOCH),),
		)

	def test_a_second_observation_resumes_exactly_after_the_first_cursor(self):
		baseline = self.baseline()
		first = self.observe(
			self.intent(baseline, amount=30),
			Transport(
				streams={0: event_frame(247574, amount="10") + b":\n"},
				transactions={TX: {"transaction": {"summary": {"outcome": "Commit", "finalized_at": FINALIZED}}}},
			),
		)
		second_transport = Transport(
			streams={247574: event_frame(247600, TX_TWO, "20") + b":\n"},
			transactions={TX_TWO: {"transaction": {"summary": {"outcome": "Commit", "finalized_at": FINALIZED}}}},
		)
		combined = self.observe(self.intent(baseline, amount=30), second_transport, first)
		self.assertEqual((combined.observed_after_tip, combined.observed_through_tip), (0, 247600))
		self.assertEqual([transfer.transaction_id for transfer in combined.transfers], [TX, TX_TWO])
		stream_url = next(url for url, _timeout, _accept in second_transport.calls if "events/stream" in url)
		self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(stream_url).query)["after_id"], ["247574"])

	def test_wrong_resource_deposits_are_ignored_without_losing_the_cursor(self):
		baseline = self.baseline()
		stream = event_frame(9, resource="resource_" + "ff" * 32) + b":\n"
		observed = self.observe(self.intent(baseline), Transport(streams={0: stream}))
		self.assertEqual(observed.transfers, ())
		self.assertEqual(observed.observed_through_tip, 9)

	def test_a_non_deposit_topic_is_ignored_without_losing_the_cursor(self):
		baseline = self.baseline()
		stream = event_frame(9, topic="std.vault.withdraw") + b":\n"
		observed = self.observe(self.intent(baseline), Transport(streams={0: stream}))
		self.assertEqual(observed.transfers, ())
		self.assertEqual(observed.observed_through_tip, 9)

	def test_duplicate_deposits_in_one_transaction_are_aggregated_for_atomic_claiming(self):
		baseline = self.baseline()
		stream = event_frame(7, amount="20") + event_frame(8, amount="22") + b":\n"
		observed = self.observe(
			self.intent(baseline, amount=42),
			Transport(streams={0: stream}, transactions={TX: {"transaction": {"summary": {"outcome": "Commit", "finalized_at": FINALIZED}}}}),
		)
		self.assertEqual(observed.transfers[0].amount_native, 42)
		self.assertEqual(len(observed.transfers), 1)

	def test_an_unreadable_finalized_timestamp_is_preserved_as_unknown(self):
		baseline = self.baseline()
		observed = self.observe(
			self.intent(baseline),
			Transport(
				streams={0: event_frame(7) + b":\n"},
				# COMMITTED, with a timestamp nothing can parse. The outcome is
				# spelled out so this test keeps testing what it is named for:
				# without it the transfer is refused for having no outcome and
				# the timestamp path is never reached.
				transactions={TX: {"transaction": {"summary": {"outcome": "Commit", "finalized_at": "yesterday"}}}},
			),
		)
		self.assertIsNone(observed.transfers[0].block_time_epoch)
		# STILL CONFIRMED, and confirmed once. The transaction committed -- only
		# its clock is unreadable -- so this is not the uncommitted case and
		# must not be recorded as though the money had not moved.
		self.assertEqual((observed.transfers[0].confirmed, observed.transfers[0].confirmations), (True, 1))
		self.assertIn("no trustworthy finalized_at", observed.warnings[0])
		self.assertEqual(self.rail.settle(self.intent(baseline), observed).state, NEEDS_REVIEW)

	def test_a_transaction_the_indexer_says_aborted_cannot_settle_a_sale(self):
		"""Reproduced 2026-08-31, on both paths, before it was fixed.

		`settle` has always answered "committed Ootle deposits are final", and
		nothing read the outcome. A summary saying `Abort` beside a valid
		timestamp settled a 5,000,000 microTari sale and booked it -- the
		guarantee was asserted over a transaction that had never been checked
		against it. The money is still REPORTED, so an operator sees it; it is
		reported unconfirmed, so nothing can count it.
		"""
		for outcome, named in (("Abort", "'Abort'"), ("Reject", "'Reject'"), (None, "no outcome at all")):
			with self.subTest(outcome=outcome):
				summary = {"finalized_at": FINALIZED}
				if outcome is not None:
					summary["outcome"] = outcome
				baseline = self.baseline()
				observed = self.observe(
					self.intent(baseline),
					Transport(
						streams={0: event_frame(7) + b":\n"},
						transactions={TX: {"transaction": {"summary": summary}}},
					),
				)
				self.assertEqual(observed.transfers[0].confirmed, False)
				self.assertEqual(observed.transfers[0].confirmations, 0)
				self.assertIsNone(observed.transfers[0].block_time_epoch)
				self.assertIn("not committed", observed.warnings[0])
				self.assertIn(named, observed.warnings[0])
				decision = self.rail.settle(self.intent(baseline), observed)
				self.assertEqual(decision.state, NEEDS_REVIEW)
				self.assertEqual(decision.credited_native, 0)
				self.assertIn("not committed", decision.reason)

	def test_payment_request_is_an_address_instruction_not_an_invented_uri(self):
		baseline = self.baseline()
		request = self.rail.create_request(self.intent(baseline, amount=1234567))
		self.assertEqual(request.uri, ACCOUNT)
		self.assertEqual(request.recipient, ACCOUNT)
		self.assertEqual(request.amount_native, 1234567)
		self.assertIn("no registered payment URI", request.payer_notice)
		self.assertIn("not a deeplink", request.payer_notice)
		self.assertIn("1234567 microTari", request.payer_notice)
		self.assertNotIn("://", request.uri)

	def test_request_requires_this_rail_a_valid_recipient_and_a_baseline(self):
		with self.assertRaises(InvalidRailPlugin):
			self.rail.create_request(PaymentIntent("sale", "other", ACCOUNT, 1, 1, 2))
		with self.assertRaises(RailProviderError):
			self.rail.create_request(PaymentIntent("sale", self.rail.key, "bad", 1, 1, 2))
		with self.assertRaises(InvalidRailPlugin):
			self.rail.create_request(PaymentIntent("sale", self.rail.key, ACCOUNT, 1, 1, 2))

	def test_observation_refuses_an_unknown_previous_shape_and_malformed_replay(self):
		baseline = self.baseline()
		with self.assertRaises(InvalidRailPlugin):
			self.observe(self.intent(baseline), Transport(), object())
		with self.assertRaises(RailProviderError) as caught:
			self.observe(self.intent(baseline), Transport(streams={0: b"not an SSE field\n\n"}))
		self.assertIn("colon", caught.exception.reason)

	def test_malformed_deposit_facts_are_refused_before_they_can_be_credited(self):
		reader = mock.Mock()
		cases = (
			(None, "transaction id"),
			({"transaction_id": 1}, "transaction id"),
			({"transaction_id": "bad"}, "transaction id"),
			({"transaction_id": TX, "event": None}, "requested vault"),
			({"transaction_id": TX, "event": {"substate_id": "vault_other"}}, "requested vault"),
			({"transaction_id": TX, "event": {"substate_id": VAULT, "payload": None}}, "payload"),
			(
				{
					"transaction_id": TX,
					"event": {
						"substate_id": VAULT,
						"payload": {"resource_address": OOTLE_XTR_RESOURCE, "amount": "not-an-int"},
					},
				},
				"positive integer",
			),
			(
				{
					"transaction_id": TX,
					"event": {
						"substate_id": VAULT,
						"payload": {"resource_address": OOTLE_XTR_RESOURCE, "amount": 0},
					},
				},
				"positive integer",
			),
		)
		for body, wording in cases:
			with self.subTest(wording=wording), self.assertRaises(RailProviderError) as caught:
				self.rail._transfers(reader, VAULT, ((1, OOTLE_DEPOSIT_TOPIC, body),))
			self.assertIn(wording, caught.exception.reason)

	def test_settlement_uses_exact_sums_and_needs_no_maturity_gate(self):
		baseline = RecipientBaseline(self.rail.key, ACCOUNT, ENDPOINT, 5)
		intent = self.intent(baseline, amount=60)
		observations = self.batch(
			baseline,
			TransferObservation(TX_TWO, 20, True, 1, block_time_epoch=FINALIZED_EPOCH),
			TransferObservation(TX, 40, True, 1, block_time_epoch=FINALIZED_EPOCH),
		)
		decision = self.rail.settle(intent, observations)
		self.assertEqual(decision.state, SETTLED)
		self.assertEqual((decision.credited_native, decision.sighted_native), (60, 60))
		self.assertEqual(decision.transaction_ids, (TX, TX_TWO))
		self.assertIn("final", decision.reason)
		at_expiry = TransferObservation(TX, 60, True, 1, block_time_epoch=intent.expires_at_epoch)
		self.assertEqual(self.rail.settle(intent, self.batch(baseline, at_expiry)).state, SETTLED)

	def test_settlement_routes_claimed_late_and_unknown_time_to_review(self):
		baseline = RecipientBaseline(self.rail.key, ACCOUNT, ENDPOINT, 5)
		intent = self.intent(baseline, amount=60)
		timely = TransferObservation(TX, 30, True, 1, block_time_epoch=FINALIZED_EPOCH)
		claimed = TransferObservation(TX_TWO, 30, True, 1, block_time_epoch=FINALIZED_EPOCH)
		claimed_decision = self.rail.settle(intent, self.batch(baseline, timely, claimed), frozenset({TX_TWO}))
		self.assertEqual(claimed_decision.state, NEEDS_REVIEW)
		self.assertEqual((claimed_decision.credited_native, claimed_decision.sighted_native), (30, 30))
		self.assertIn("already claimed", claimed_decision.reason)

		late = TransferObservation(TX_TWO, 30, True, 1, block_time_epoch=FINALIZED_EPOCH + 61)
		late_decision = self.rail.settle(intent, self.batch(baseline, timely, late))
		self.assertEqual(late_decision.state, NEEDS_REVIEW)
		# "outside the sale's window", not "after expiry". D57 bounded the
		# window at BOTH ends, so a deposit dated before the sale existed lands
		# here too and the old sentence was false for half of what reaches it.
		self.assertIn("outside the sale's window", late_decision.reason)

		early = TransferObservation(TX_TWO, 30, True, 1, block_time_epoch=intent.created_at_epoch - 7_200)
		early_decision = self.rail.settle(intent, self.batch(baseline, timely, early))
		self.assertEqual(early_decision.state, NEEDS_REVIEW)
		self.assertIn("outside the sale's window", early_decision.reason)

		unknown = TransferObservation(TX_TWO, 30, True, 1)
		unknown_decision = self.rail.settle(intent, self.batch(baseline, timely, unknown))
		self.assertEqual(unknown_decision.state, NEEDS_REVIEW)
		self.assertIn("trustworthy block time", unknown_decision.reason)

		insufficient_claimed = self.rail.settle(
			intent,
			self.batch(
				baseline,
				TransferObservation(TX, 20, True, 1, block_time_epoch=FINALIZED_EPOCH),
				TransferObservation(TX_TWO, 20, True, 1, block_time_epoch=FINALIZED_EPOCH),
			),
			frozenset({TX_TWO}),
		)
		self.assertEqual(insufficient_claimed.state, PENDING)

		late_underpayment = self.rail.settle(
			intent,
			self.batch(
				baseline,
				TransferObservation(TX, 59, True, 1, block_time_epoch=FINALIZED_EPOCH + 61),
			),
		)
		self.assertEqual(late_underpayment.state, PENDING)

	def test_settlement_keeps_empty_and_underpaid_observations_pending(self):
		baseline = RecipientBaseline(self.rail.key, ACCOUNT, ENDPOINT, 5)
		intent = self.intent(baseline, amount=60)
		empty = self.rail.settle(intent, self.batch(baseline))
		self.assertEqual((empty.state, empty.reason), (PENDING, "no payment observed"))
		under = self.rail.settle(
			intent,
			self.batch(
				baseline,
				TransferObservation(TX, 59, True, 1, block_time_epoch=FINALIZED_EPOCH),
			),
		)
		self.assertEqual((under.state, under.credited_native), (PENDING, 59))
		self.assertIn("below", under.reason)

	def test_settlement_refuses_unbound_incomplete_or_malformed_inputs(self):
		baseline = RecipientBaseline(self.rail.key, ACCOUNT, ENDPOINT, 5)
		intent = self.intent(baseline)
		complete = self.batch(baseline)
		with self.assertRaises(InvalidRailPlugin):
			self.rail.settle(intent, object())
		with self.assertRaises(InvalidRailPlugin):
			self.rail.settle(self.intent(RecipientBaseline(self.rail.key, ACCOUNT, "other", 5)), complete)
		incomplete = ObservationBatch(
			self.rail.key, intent.intent_id, ACCOUNT, ENDPOINT, 5, 7, 5, 6, ()
		)
		with self.assertRaises(InvalidRailPlugin):
			self.rail.settle(intent, incomplete)
		for claimed in ({TX}, frozenset({1})):
			with self.subTest(claimed=claimed), self.assertRaises(InvalidRailPlugin):
				self.rail.settle(intent, complete, claimed)

	def batch(self, baseline, *transfers):
		return ObservationBatch(
			self.rail.key,
			"sale-1",
			ACCOUNT,
			ENDPOINT,
			baseline.tip,
			9,
			baseline.tip,
			9,
			tuple(transfers),
			finalized_tip=9,
		)

	def test_confidential_balance_and_absent_vault_are_refused_at_baseline(self):
		with mock.patch("cryptopos_rail_ootle.chain._urlopen", Transport(kind="Confidential")):
			with self.assertRaises(RailProviderError) as caught:
				self.rail.capture_baseline(ACCOUNT, self.configuration())
		self.assertIn("confidential", caught.exception.reason)

		class NoVault(Transport):
			def __call__(self, request, timeout=None):
				if request.full_url.endswith("/substates/" + ACCOUNT):
					return Response(json.dumps({"substate": {"Component": {"body": {"state": []}}}}).encode())
				return super().__call__(request, timeout)

		with mock.patch("cryptopos_rail_ootle.chain._urlopen", NoVault()):
			with self.assertRaises(RailProviderError) as caught:
				self.rail.capture_baseline(ACCOUNT, self.configuration())
		self.assertIn("no XTR vault", caught.exception.reason)

	def test_resource_identity_and_timestamp_are_pinned(self):
		self.assertEqual(OOTLE_XTR_RESOURCE, "resource_" + "01" * 32)
		self.assertEqual(_finalized_epoch(FINALIZED), FINALIZED_EPOCH)
		self.assertEqual(_finalized_epoch("1970-01-01 00:00:00.0"), 0)
		for value in (None, "not a timestamp", "1969-12-31 23:59:59.0"):
			with self.subTest(value=value):
				self.assertIsNone(_finalized_epoch(value))


class EventReplayBoundaries(unittest.TestCase):
	def test_crlf_multiline_data_unknown_fields_and_comments_follow_sse_rules(self):
		body = json.dumps(
			{
				"transaction_id": TX,
				"event": {
					"substate_id": VAULT,
					"payload": {"amount": "1", "resource_address": OOTLE_XTR_RESOURCE},
				},
			},
			indent=2,
		)
		data = "\r\n".join("data: " + line for line in body.splitlines())
		stream = f": hello\r\nevent: {OOTLE_DEPOSIT_TOPIC}\r\nid: 8\r\nretry: 1000\r\n{data}\r\n\r\n".encode()
		events, cursor = _event_replay(stream, 7)
		self.assertEqual(cursor, 8)
		self.assertEqual(events[0][2]["transaction_id"], TX)

	def test_malformed_streams_are_refused(self):
		cases = (
			("not bytes", 0),
			(b"\xff", 0),
			(b"event std.vault.deposit\nid: 1\ndata: {}\n\n", 0),
			(b"event: x\nevent: y\nid: 1\ndata: {}\n\n", 0),
			(b"event: x\nid: 1\n\n", 0),
			(b"event: x\nid: nope\ndata: {}\n\n", 0),
			(b"event: x\nid: 1\ndata: {}\n\n", 1),
			(b"event: x\nid: 1\ndata: nope\n\n", 0),
		)
		for payload, cursor in cases:
			with self.subTest(payload=payload), self.assertRaises(ValueError):
				_event_replay(payload, cursor)
		with self.assertRaises(ValueError) as caught:
			_event_replay(b"event: x\nid: 1\n\n", 0)
		self.assertIn("event, id, and data", str(caught.exception))

	def test_empty_keepalive_replay_preserves_cursor(self):
		self.assertEqual(_event_replay(b":\n", 12), ((), 12))

	def test_one_microtari_is_a_valid_exact_deposit(self):
		rail = OotleEsmeralda()
		reader = mock.Mock()
		reader._get.return_value = ({"transaction": {"summary": {"outcome": "Commit", "finalized_at": FINALIZED}}}, None)
		body = json.loads(event_frame(1, amount="1").split(b"data: ", 1)[1])
		transfers, warnings, _unresolved = rail._transfers(reader, VAULT, ((1, OOTLE_DEPOSIT_TOPIC, body),))
		self.assertEqual(transfers[0].amount_native, 1)
		self.assertEqual(warnings, ())


class SseTransportBoundaries(unittest.TestCase):

	def test_a_connection_that_goes_quiet_answers_empty_and_one_that_fails_does_not(self):
		"""The distinction the drain rests on, at the layer that makes it.

		Both are an absence of bytes. An established connection that then goes
		quiet is the endpoint saying "nothing after your cursor" -- esmeralda
		never sends the `:` marker, so this is how EVERY read ends there. A
		connection that was never established is a failure, and returning
		empty bytes for it would let a dead endpoint look like an empty
		history: an HTTP 503 mid-drain then ends the replay early and money
		that predates a sale settles it. Reproduced 2026-08-31.
		"""

		class Quiet(Response):
			def readline(self, *_args):
				raise OSError("The read operation timed out")

		with mock.patch.object(chain, "_urlopen", lambda *_a, **_k: Quiet(b"")):
			payload, reason = OotleReader(indexer=ENDPOINT)._get_sse("events")
		self.assertEqual((payload, reason), (b"", None))

		def refuse(*_args, **_kwargs):
			raise urllib.error.URLError("connection refused")

		with mock.patch.object(chain, "_urlopen", refuse):
			payload, reason = OotleReader(indexer=ENDPOINT)._get_sse("events")
		self.assertIsNone(payload)
		self.assertIn("did not answer", reason)

	def test_stream_read_stops_at_idle_comment_and_never_reads_again(self):
		class IdleResponse(Response):
			def __init__(self):
				super().__init__(event_frame(1) + b": idle\n")
				self.saw_idle = False
				self.sizes = []

			def readline(self, size=-1):
				if self.saw_idle:
					raise AssertionError("reader continued after idle")
				self.sizes.append(size)
				line = super().readline(size)
				if line.startswith(b":"):
					self.saw_idle = True
				return line

		response = IdleResponse()
		with mock.patch("cryptopos_rail_ootle.chain._urlopen", lambda request, timeout=None: response):
			payload, reason = OotleReader(indexer=ENDPOINT)._get_sse("events")
		self.assertIsNone(reason)
		self.assertTrue(payload.endswith(b": idle\n"))
		self.assertEqual(response.sizes[0], chain.MAX_RESPONSE_BYTES + 1)
		self.assertEqual(response.sizes[1], chain.MAX_RESPONSE_BYTES + 1 - len(event_frame(1).splitlines()[0]) - 1)

	def test_stream_transport_is_total_for_limits_http_and_bad_answers(self):
		oversized = Response(b"x" * (chain.MAX_RESPONSE_BYTES + 1))
		with mock.patch("cryptopos_rail_ootle.chain._urlopen", lambda request, timeout=None: oversized):
			payload, reason = OotleReader(indexer=ENDPOINT)._get_sse("events")
		self.assertIsNone(payload)
		self.assertIn("exceeded", reason)

		http_failure = urllib.error.HTTPError(ENDPOINT, 503, "down", {}, Response(b""))
		for error, wording in (
			(http_failure, "503"),
			(urllib.error.URLError("down"), "did not answer"),
			(TypeError("bad response"), "invalid event stream"),
		):
			with self.subTest(error=type(error).__name__):
				with mock.patch("cryptopos_rail_ootle.chain._urlopen", side_effect=error):
					payload, reason = OotleReader(indexer=ENDPOINT)._get_sse("events")
				self.assertIsNone(payload)
				self.assertIn(wording, reason)
		http_failure.close()

	def test_a_stream_exactly_at_the_byte_ceiling_is_allowed(self):
		response = Response(b"x" * chain.MAX_RESPONSE_BYTES)
		with mock.patch("cryptopos_rail_ootle.chain._urlopen", lambda request, timeout=None: response):
			payload, reason = OotleReader(indexer=ENDPOINT)._get_sse("events")
		self.assertEqual(len(payload), chain.MAX_RESPONSE_BYTES)
		self.assertIsNone(reason)

	def test_plain_http_streams_work_only_when_explicitly_allowed(self):
		with mock.patch("cryptopos_rail_ootle.chain._urlopen", lambda request, timeout=None: Response(b":\n")):
			payload, reason = OotleReader(
				indexer="http://localhost:18000",
				allow_insecure=True,
			)._get_sse("events")
		self.assertEqual(payload, b":\n")
		self.assertIsNone(reason)

	def test_stream_transport_reuses_the_reader_url_refusals(self):
		for reader, wording in (
			(OotleReader(indexer=1), "must be text"),
			(OotleReader(indexer="http://example.test"), "https"),
			(OotleReader(indexer="https://user:pass@example.test"), "credentials"),
			(OotleReader(indexer="https://[bad"), "valid URL"),
		):
			with self.subTest(indexer=reader.indexer):
				payload, reason = reader._get_sse("events")
				self.assertIsNone(payload)
				self.assertIn(wording, reason)


if __name__ == "__main__":
	unittest.main()

class SummaryField(unittest.TestCase):
	"""`_summary_field` is TOTAL: every unusable shape is the same "no answer".

	The real body nests it two deep --
	`{"transaction": {"summary": {"finalized_at": ...}}}` -- checked against
	three esmeralda transactions on 2026-08-31. Each guard below is a shape a
	broken or hostile indexer can actually return, and the caller's response to
	all of them is identical: leave the timestamp unset, so settlement routes
	the payment to review instead of crediting it on a guess.
	"""

	def test_the_measured_shape(self):
		body = {"transaction": {"summary": {"outcome": "Commit", "finalized_at": "2026-08-31 04:12:19.0"}}}
		self.assertEqual(_summary_field(body, "finalized_at"), "2026-08-31 04:12:19.0")

	def test_a_body_that_is_not_a_mapping(self):
		for body in (None, [], "finalized", 7):
			with self.subTest(body=body):
				self.assertIsNone(_summary_field(body, "finalized_at"))

	def test_a_transaction_that_is_not_a_mapping(self):
		for transaction in (None, [], "no", 0):
			with self.subTest(transaction=transaction):
				self.assertIsNone(_summary_field({"transaction": transaction}, "finalized_at"))

	def test_a_summary_that_is_not_a_mapping(self):
		for summary in (None, [], "no", 0):
			with self.subTest(summary=summary):
				self.assertIsNone(
					_summary_field({"transaction": {"summary": summary}}, "finalized_at")
				)

	def test_a_summary_without_the_field(self):
		self.assertIsNone(
			_summary_field({"transaction": {"summary": {"outcome": "Commit"}}}, "finalized_at")
		)


# ---------------------------------------------------------------------------
# The payment component: a per-sale binding, and the failure it removes.
# ---------------------------------------------------------------------------

_COMPONENT = "component_" + "1d" * 32
_INDEXER = "https://indexer.example"


def _payment_stream(*rows):
	"""An SSE body in the shape esmeralda really emits.

	Measured 2026-08-31 against the deployed loyalty contract, whose
	`emit_event("PointsIssued", ...)` is indexed as `Loyalty.PointsIssued`
	with the metadata verbatim in `event.payload`. Filtering on the bare event
	name returns an EMPTY stream, which is indistinguishable from "this chain
	does not index custom events" -- so the namespaced topic is the thing
	under test here, not an incidental detail.
	"""
	frames = []
	for event_id, sale_ref, amount in rows:
		frames.append(
			"event: %s\nid: %d\ndata: %s\n"
			% (
				OOTLE_PAYMENT_TOPIC,
				event_id,
				json.dumps(
					{
						"transaction_id": "%064x" % event_id,
						"event": {
							"substate_id": _COMPONENT,
							"template_address": "985d07cc",
							"payload": {
								"epoch": "10766",
								"sale_ref": sale_ref,
								"amount": str(amount),
							},
						},
					}
				),
			)
		)
	return ("\n".join(frames) + "\n").encode("utf-8")


class _StubReader:
	"""Just enough indexer to drive the component path, offline."""

	indexer = _INDEXER

	def __init__(self, stream, outcome="Commit"):
		self._stream = stream
		self._outcome = outcome

	def _get_sse(self, _path):
		return self._stream, None

	def _get(self, path):
		if path == "network":
			return {"network": "esmeralda", "epoch": 10776}, None
		# The measured envelope: `summary` sits beside the inner transaction,
		# not under it. A flat or over-nested read returns None for every real
		# transaction, which routes an honest payment to review.
		return (
			{
				"transaction": {
					"transaction_id": path.rsplit("/", 1)[-1],
					"transaction": {},
					"summary": {"outcome": self._outcome, "finalized_at": "2026-08-31 04:12:19.0"},
				}
			},
			None,
		)


class PaymentComponentBinding(unittest.TestCase):
	def setUp(self):
		self.rail = OotleEsmeralda()
		self.configuration = {"endpoint": _INDEXER, "payment_component": _COMPONENT}

	def _with_stream(self, stream, outcome="Commit"):
		reader = _StubReader(stream, outcome)
		patch = mock.patch.object(OotleEsmeralda, "_reader", lambda _self, _cfg: reader)
		patch.start()
		self.addCleanup(patch.stop)
		return reader

	def _intent(self, name, amount):
		return PaymentIntent(
			name,
			self.rail.key,
			_COMPONENT,
			amount,
			1_000,
			9_999_999_999,
			payment_reference=name,
			baseline=RecipientBaseline(
				self.rail.key, _COMPONENT, _INDEXER, 0, payment_component=_COMPONENT
			),
		)

	def test_a_payment_naming_this_sale_still_cannot_settle_it_if_it_aborted(self):
		"""The binding says WHOSE money it is; the outcome says whether it moved.

		The per-sale binding is the stronger guarantee and it is not this one.
		A payment event naming exactly this sale, for exactly the invoiced
		amount, from a transaction the indexer itself reports as aborted, used
		to settle and book. Both checks are needed and neither substitutes for
		the other.
		"""
		self._with_stream(_payment_stream((201, "SALE-B", 5_000_000)), outcome="Abort")
		paid = self._intent("SALE-B", 5_000_000)
		observed = self.rail.observe(paid, self.configuration)
		self.assertIn("not committed", observed.warnings[0])
		decision = self.rail.settle(paid, observed, frozenset())
		self.assertEqual(decision.state, NEEDS_REVIEW)
		self.assertEqual(decision.credited_native, 0)

	def test_a_payment_settles_only_the_sale_it_names(self):
		"""D48's exact scenario, which the shared account got wrong.

		Sale A invoiced 100,000 uT and its customer paid nothing. Sale B
		invoiced 5,000,000 uT and its customer paid. On the shared account A
		settled on B's money because A polled first, and B ended
		`needs-review` credited nothing. The reference removes the inference.
		"""
		self._with_stream(_payment_stream((201, "SALE-B", 5_000_000)))

		unpaid = self._intent("SALE-A", 100_000)
		decision = self.rail.settle(unpaid, self.rail.observe(unpaid, self.configuration), frozenset())
		self.assertEqual(decision.state, PENDING)
		self.assertEqual(decision.credited_native, 0)

		paid = self._intent("SALE-B", 5_000_000)
		decision = self.rail.settle(paid, self.rail.observe(paid, self.configuration), frozenset())
		self.assertEqual(decision.state, SETTLED)
		self.assertEqual(decision.credited_native, 5_000_000)

	def test_two_sales_paid_at_once_credit_their_own_payments(self):
		self._with_stream(
			_payment_stream((301, "SALE-A", 100_000), (302, "SALE-B", 5_000_000))
		)
		for name, amount in (("SALE-A", 100_000), ("SALE-B", 5_000_000)):
			intent = self._intent(name, amount)
			decision = self.rail.settle(
				intent, self.rail.observe(intent, self.configuration), frozenset()
			)
			self.assertEqual(decision.state, SETTLED, name)
			self.assertEqual(decision.credited_native, amount, name)

	def test_an_event_carrying_no_sale_reference_is_refused(self):
		body = json.dumps(
			{
				"transaction_id": "%064x" % 401,
				"event": {
					"substate_id": _COMPONENT,
					"template_address": "985d07cc",
					"payload": {"amount": "5000000"},
				},
			}
		)
		self._with_stream(("event: %s\nid: 401\ndata: %s\n" % (OOTLE_PAYMENT_TOPIC, body)).encode())
		intent = self._intent("SALE-A", 5_000_000)
		with self.assertRaises(RailProviderError):
			self.rail.observe(intent, self.configuration)

	def test_an_event_for_another_component_is_refused(self):
		body = json.dumps(
			{
				"transaction_id": "%064x" % 501,
				"event": {
					"substate_id": "component_" + "ee" * 32,
					"template_address": "985d07cc",
					"payload": {"sale_ref": "SALE-A", "amount": "5000000"},
				},
			}
		)
		self._with_stream(("event: %s\nid: 501\ndata: %s\n" % (OOTLE_PAYMENT_TOPIC, body)).encode())
		intent = self._intent("SALE-A", 5_000_000)
		with self.assertRaises(RailProviderError):
			self.rail.observe(intent, self.configuration)

	def test_a_sale_with_no_reference_cannot_use_a_payment_component(self):
		self._with_stream(_payment_stream((601, "SALE-A", 5_000_000)))
		intent = PaymentIntent(
			"SALE-A",
			self.rail.key,
			_COMPONENT,
			5_000_000,
			1_000,
			9_999_999_999,
			baseline=RecipientBaseline(
				self.rail.key, _COMPONENT, _INDEXER, 0, payment_component=_COMPONENT
			),
		)
		with self.assertRaises(RailProviderError):
			self.rail.observe(intent, self.configuration)

	def test_a_malformed_payment_component_is_refused(self):
		for component in ("not-a-component", "component_zz", 7, []):
			with self.subTest(component=component):
				with self.assertRaises(RailProviderError):
					self.rail._payment_component({"payment_component": component})

	def test_the_request_tells_the_payer_to_name_the_sale(self):
		intent = self._intent("SALE-A", 5_000_000)
		request = self.rail.create_request(intent)
		self.assertEqual(request.recipient, _COMPONENT)
		self.assertEqual(request.uri, _COMPONENT)
		self.assertIn("SALE-A", request.payer_notice)
		self.assertIn("pay", request.payer_notice)
		# The warning that stops a plain transfer, which would land in the
		# component's vault naming no sale and never be credited.
		self.assertIn("not be credited", request.payer_notice)

	def test_the_shared_path_is_unchanged_when_no_component_is_configured(self):
		self.assertEqual(self.rail._payment_component({}), "")
		self.assertEqual(self.rail._payment_component({"payment_component": ""}), "")


class MoneyOlderThanItsSale(unittest.TestCase):
	"""Settlement tested BOTH ends of the window, not one.

	Until 2026-08-31 `settle` checked only `block_time <= expires_at`, so a
	deposit dated a day before the sale existed settled it. The cursor is what
	is supposed to make that unreachable -- but `chain._get_sse` treats a
	timeout with frames already in hand as the end of a replay and cannot tell
	a finished replay from a truncated one, so a short baseline over a long
	history puts pre-existing money after the cursor. Found by an adversarial
	review and reproduced before it was believed.
	"""

	CREATED = 1_000_000
	EXPIRES = 1_000_900

	def _decision(self, block_time):
		rail = OotleEsmeralda()
		recipient = "component_" + "e" * 54
		intent = PaymentIntent(
			"SALE", rail.key, recipient, 5_000_000, self.CREATED, self.EXPIRES,
			baseline=RecipientBaseline(rail.key, recipient, "https://i.example", 100),
		)
		transfer = TransferObservation("d" * 64, 5_000_000, True, 1, block_time_epoch=block_time)
		batch = ObservationBatch(
			rail.key, intent.intent_id, recipient, "https://i.example",
			100, 200, 100, 200, (transfer,), finalized_tip=200,
		)
		return rail.settle(intent, batch, frozenset())

	def test_money_that_arrived_during_the_sale_settles_it(self):
		self.assertEqual(self._decision(self.CREATED + 100).state, SETTLED)

	def test_money_older_than_the_sale_does_not_settle_it(self):
		decision = self._decision(self.CREATED - 86_400)
		self.assertEqual(decision.state, NEEDS_REVIEW)
		self.assertEqual(decision.credited_native, 0)

	def test_ordinary_clock_skew_still_settles(self):
		"""THE CONTROL, and the reason the bound is not zero.

		D19 was a nine-hour timezone error that made every real payment look
		late and was invisible to a fully green suite. A tight lower bound here
		would be that defect wearing the opposite sign, so half an hour of
		disagreement between a chain's clock and this host's must still settle.
		"""
		self.assertEqual(self._decision(self.CREATED - 1800).state, SETTLED)

	def test_the_skew_bound_is_exactly_an_hour_and_the_edge_is_inside_it(self):
		"""The bound is a decision, so it is pinned to the second.

		Both of these survived mutation before they were written: an hour that
		was silently 3,601 seconds, and a `<=` that was silently `<`, are both
		invisible to a test that only ever asks about half an hour and a day.
		A boundary nobody asserts is a boundary that can move on its own.
		"""
		self.assertEqual(self._decision(self.CREATED - 3600).state, SETTLED)
		self.assertEqual(self._decision(self.CREATED - 3601).state, NEEDS_REVIEW)


class _PagedStubReader(_StubReader):
	"""A stub that honours `after_id`, so a drain can actually terminate.

	`_StubReader` returns one fixed payload whatever the cursor, which is fine
	for a single observation and wrong for a baseline: the second page would
	replay ids the first already delivered and `_event_replay` would refuse
	them. Pages keyed by cursor is what a real endpoint does.
	"""

	def __init__(self, pages, outcome="Commit"):
		super().__init__(b":\n", outcome)
		self._pages = pages
		self.asked = []

	def _get_sse(self, path):
		after = int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["after_id"][0])
		self.asked.append(after)
		return self._pages.get(after, b":\n"), None


class PaymentComponentBoundaries(unittest.TestCase):
	"""What the component path REFUSES, which is most of what it is for.

	A payment component is the per-sale binding (D49-D54) and its refusals had
	no test at all when this file's coverage gate was first run against it --
	nine lines of the newest and most safety-bearing code in the rail, every
	one of them a refusal. A refusal nobody has seen fire is a refusal nobody
	knows is there.
	"""

	def setUp(self):
		self.rail = OotleEsmeralda()
		self.configuration = {"endpoint": _INDEXER, "payment_component": _COMPONENT}

	def _reader(self, reader):
		patch = mock.patch.object(OotleEsmeralda, "_reader", lambda _self, _cfg: reader)
		patch.start()
		self.addCleanup(patch.stop)
		return reader

	def _event(self, body, topic=OOTLE_PAYMENT_TOPIC):
		return ((1, topic, body),)

	def _payment_body(self, **payload):
		merged = {"sale_ref": "SALE-A", "amount": "5000000"}
		merged.update(payload)
		return {
			"transaction_id": "a" * 64,
			"event": {"substate_id": _COMPONENT, "payload": merged},
		}

	def test_a_baseline_over_a_component_reads_one_page_and_has_no_lifetime(self):
		"""The component path does NOT drain, and that is the fix, not a gap.

		Here the money names the sale: `_referenced_payments` credits only
		deposits carrying this sale's reference, and a reference is minted per
		sale with 57 bits of entropy, so no event that already existed can
		carry it. The cursor is an optimisation, not the attribution.

		Draining it was actively harmful. Every baseline replays from zero, so
		a component would have hit the twelve-page bound after roughly its
		sixtieth event and then refused **every** later sale -- a finite
		operational lifetime, on the one path this project publishes. Found by
		a review of this session's own work and reproduced before it was
		believed.
		"""
		history = _payment_stream(*[(index + 1, "OLD-%d" % index, 1) for index in range(200)])
		reader = self._reader(_PagedStubReader({0: history}))
		reader.resource_balance = lambda _account, _resource: (0, None)
		baseline = self.rail.capture_baseline(_COMPONENT, self.configuration)
		self.assertEqual(baseline.tip, 200)
		self.assertEqual(baseline.payment_component, _COMPONENT)
		# One read, not a drain: the second page was never asked for.
		self.assertEqual(reader.asked, [0])

	def test_an_unreadable_payment_stream_refuses_rather_than_reading_empty(self):
		reader = self._reader(_StubReader(b""))
		reader._get_sse = lambda _path: (None, "the indexer did not answer")
		with self.assertRaises(RailProviderError) as refused:
			self.rail._payment_events(reader, _COMPONENT, 0)
		self.assertIn("payment event stream could not be read", refused.exception.reason)

	def test_an_unparseable_payment_stream_refuses(self):
		reader = self._reader(_StubReader(b"id: 1\ndata: {}\n\n"))
		with self.assertRaises(RailProviderError) as refused:
			self.rail._payment_events(reader, _COMPONENT, 0)
		self.assertIn("event, id, and data", refused.exception.reason)

	def test_a_foreign_topic_on_the_component_stream_is_skipped_not_credited(self):
		reader = _StubReader(b":\n")
		events = self._event(self._payment_body(), topic="std.vault.deposit")
		transfers, warnings, _unresolved = self.rail._referenced_payments(reader, _COMPONENT, events, "SALE-A")
		self.assertEqual((transfers, warnings), ((), ()))

	def test_every_malformed_payment_event_refuses_rather_than_being_dropped(self):
		"""Dropping would UNDER-credit a customer who really paid.

		This is the opposite failure to D48's. There, inference credited a sale
		from money that was not its own; here, silence would deny a sale money
		that was. Both are wrong and the refusal is the only answer that is not
		a guess in one direction or the other.
		"""
		cases = (
			({"transaction_id": 7, "event": {}}, "transaction id was malformed"),
			({"transaction_id": "a" * 64, "event": {"substate_id": _COMPONENT}}, "payload was malformed"),
			(self._payment_body(sale_ref=""), "carried no sale reference"),
			(self._payment_body(sale_ref="x" * 129), "sale reference was oversized"),
			(self._payment_body(amount="0"), "amount was not a positive integer"),
		)
		reader = _StubReader(b":\n")
		for body, wording in cases:
			with self.subTest(wording=wording), self.assertRaises(RailProviderError) as refused:
				self.rail._referenced_payments(reader, _COMPONENT, self._event(body), "SALE-A")
			self.assertIn(wording, refused.exception.reason)

	def test_a_sale_reference_longer_than_the_component_accepts_refuses(self):
		"""Refused HERE, not sent and lost.

		The component bounds its own argument, so an oversized reference is a
		payment instruction the payer could never satisfy. Refusing at request
		time means nobody is told to send money that can never be attributed.
		"""
		intent = PaymentIntent(
			"sale-long",
			self.rail.key,
			_COMPONENT,
			5_000_000,
			1_000,
			9_999_999_999,
			payment_reference="x" * 129,
			baseline=RecipientBaseline(
				self.rail.key, _COMPONENT, _INDEXER, 0, payment_component=_COMPONENT
			),
		)
		with self.assertRaises(RailProviderError) as refused:
			self.rail.create_request(intent)
		self.assertIn("longer than the component accepts", refused.exception.reason)

	def test_a_baseline_payment_component_must_be_text(self):
		with self.assertRaises(InvalidRailPlugin):
			RecipientBaseline(self.rail.key, _COMPONENT, _INDEXER, 0, payment_component=7)

	def test_the_accepted_edge_of_every_bound_is_asserted_not_only_the_refused_one(self):
		"""A bound with only its refusing side tested can move inwards silently.

		All three survived mutation: a 128-byte limit that had quietly become
		127 would refuse references the component itself accepts, and an
		amount test of `<= 1` would drop a one-microTari payment. Nothing in
		the suite would have said a word, because every case it asked about
		was far from the edge.
		"""
		longest = "x" * MAX_SALE_REF_BYTES
		reader = _StubReader(b":\n")
		transfers, _warnings, _unresolved = self.rail._referenced_payments(
			reader, _COMPONENT, self._event(self._payment_body(sale_ref=longest)), longest
		)
		self.assertEqual(len(transfers), 1)

		smallest, _warnings, _unresolved = self.rail._referenced_payments(
			reader, _COMPONENT, self._event(self._payment_body(amount="1")), "SALE-A"
		)
		self.assertEqual(smallest[0].amount_native, 1)

		intent = PaymentIntent(
			"sale-edge",
			self.rail.key,
			_COMPONENT,
			5_000_000,
			1_000,
			9_999_999_999,
			payment_reference=longest,
			baseline=RecipientBaseline(
				self.rail.key, _COMPONENT, _INDEXER, 0, payment_component=_COMPONENT
			),
		)
		self.assertIn(longest, self.rail.create_request(intent).payer_notice)


class WhatTheProviderDidNotSay(unittest.TestCase):
	"""An absence of an answer is not an answer, and must never become one.

	Every case here was found by a cold review of the session that introduced
	the outcome check, and every one was reproduced before it was fixed. They
	share a shape: two different facts arriving as the same value, and code
	that picked the more convenient reading.
	"""

	def setUp(self):
		self.rail = OotleEsmeralda()

	def _settle(self, reader, stream):
		patch = mock.patch.object(OotleEsmeralda, "_reader", lambda _self, _cfg: reader)
		patch.start()
		self.addCleanup(patch.stop)
		reader._stream = stream
		intent = PaymentIntent(
			"SALE", self.rail.key, _COMPONENT, 5_000_000, 1_000, 9_999_999_999,
			payment_reference="SALE",
			baseline=RecipientBaseline(
				self.rail.key, _COMPONENT, _INDEXER, 0, payment_component=_COMPONENT
			),
		)
		configuration = {"endpoint": _INDEXER, "payment_component": _COMPONENT}
		observed = self.rail.observe(intent, configuration)
		return observed, self.rail.settle(intent, observed, frozenset())

	def test_an_unreadable_transaction_keeps_the_sale_pending_not_refused(self):
		"""One HTTP 503 used to cost a customer a sale they had paid for.

		`needs-review` is terminal (D10) and the sweep never reopens it, so a
		transient failure on the transaction read was a permanent verdict --
		and the indexer recovering a second later changed nothing. Pending is
		the honest state: nothing was decided, so keep polling and let the
		sale's own expiry end it if the reads never recover.
		"""
		reader = _StubReader(b":\n")
		reader._get = lambda _path: (
			({"network": "esmeralda", "epoch": 10776}, None)
			if _path == "network"
			else (None, "the indexer answered 503 for " + _path)
		)
		observed, decision = self._settle(reader, _payment_stream((201, "SALE", 5_000_000)))
		self.assertEqual(decision.state, PENDING)
		self.assertEqual(decision.credited_native, 0)
		self.assertIn("did not answer whether", decision.reason)
		self.assertIn("retried, not refused", decision.reason)
		# And the warning must not claim a fact nobody established.
		self.assertIn("could not be read", observed.warnings[0])
		self.assertNotIn("moved no money", observed.warnings[0])

	def test_a_body_for_a_different_transaction_certifies_nothing(self):
		"""The outcome check must prove it looked at the right transaction.

		A cache or proxy answering with another transaction would otherwise
		pair THIS event's amount with THAT transaction's `Commit` and
		timestamp, and settle on a body it had never checked.
		"""
		reader = _StubReader(b":\n")
		reader._get = lambda _path: (
			({"network": "esmeralda", "epoch": 10776}, None)
			if _path == "network"
			else (
				{"transaction": {
					"transaction_id": "f" * 64,
					"summary": {"outcome": "Commit", "finalized_at": "2026-08-31 04:12:19.0"},
				}},
				None,
			)
		)
		observed, decision = self._settle(reader, _payment_stream((201, "SALE", 5_000_000)))
		self.assertEqual(decision.state, PENDING)
		self.assertEqual(decision.credited_native, 0)
		self.assertIn("answered with", observed.warnings[0])

	def test_a_recovered_read_resolves_a_sale_that_was_pending(self):
		"""THE CONTROL. Doubt must not be permanent either.

		If an unresolved transaction stayed unresolved once recorded, the
		remedy for a terminal refusal would be a sale that never settles --
		the same defect wearing the other sign.
		"""
		reader = _StubReader(b":\n")
		observed, decision = self._settle(reader, _payment_stream((201, "SALE", 5_000_000)))
		self.assertEqual(decision.state, SETTLED)
		self.assertEqual(observed.unresolved_transaction_ids, ())

	def test_doubt_about_extra_money_does_not_hold_back_a_paid_sale(self):
		"""`credited < amount` is strict, and the strictness is the point.

		A sale fully covered by transactions that DID resolve is settled. An
		unresolved extra deposit alongside it is somebody else's problem --
		holding the sale pending for it would let one unreadable stranger's
		transaction freeze a customer who has paid in full.
		"""
		rail = OotleEsmeralda()
		baseline = RecipientBaseline(rail.key, ACCOUNT, ENDPOINT, 5)
		intent = PaymentIntent(
			"sale-1", rail.key, ACCOUNT, 60, FINALIZED_EPOCH - 60, FINALIZED_EPOCH + 60,
			baseline=baseline,
		)
		paid = TransferObservation(TX, 60, True, 1, block_time_epoch=FINALIZED_EPOCH)
		doubtful = TransferObservation(TX_TWO, 5, False, 0)
		batch = ObservationBatch(
			rail.key, "sale-1", ACCOUNT, ENDPOINT, 5, 7, 5, 7,
			(paid, doubtful), unresolved_transaction_ids=(TX_TWO,),
		)
		self.assertEqual(rail.settle(intent, batch).state, SETTLED)


class DoubtIsNotPermanent(unittest.TestCase):
	"""A transaction that could not be read once and reads cleanly later.

	The remedy for "a transient failure is a terminal refusal" must not be
	"a transient failure is a sale that never settles". `extend` therefore
	DROPS a transaction from the unresolved set the moment a later page
	reports it confirmed, rather than accumulating every doubt ever held.
	"""

	def test_the_next_poll_re_reads_what_the_last_one_could_not(self):
		"""The whole point, and it is an outcome and not a wording.

		The cursor advances past an event once seen, and `extend` refuses a
		page repeating a transaction id -- so without an explicit retry the
		sale merely stops being wrongly REFUSED and starts quietly EXPIRING,
		which costs the customer exactly the same. The second poll must ask
		about the transaction again and settle it.
		"""
		rail = OotleEsmeralda()
		unread = ObservationBatch(
			rail.key, "SALE", _COMPONENT, _INDEXER, 0, 201, 0, 201,
			(TransferObservation("%064x" % 201, 5_000_000, False, 0),),
			warnings=("transaction %064x could not be read, so whether it committed is unknown: 503" % 201,),
			finalized_tip=201,
			unresolved_transaction_ids=("%064x" % 201,),
		)
		intent = PaymentIntent(
			"SALE", rail.key, _COMPONENT, 5_000_000, 1_000, 9_999_999_999,
			payment_reference="SALE",
			baseline=RecipientBaseline(
				rail.key, _COMPONENT, _INDEXER, 0, payment_component=_COMPONENT
			),
		)
		self.assertEqual(rail.settle(intent, unread, frozenset()).state, PENDING)

		# The indexer recovers. Nothing new arrives -- the stream is empty --
		# and the sale must still settle on the money it already saw.
		reader = _StubReader(b":\n")
		with mock.patch.object(OotleEsmeralda, "_reader", lambda _s, _c: reader):
			again = rail.observe(intent, {"endpoint": _INDEXER, "payment_component": _COMPONENT}, unread)
			decision = rail.settle(intent, again, frozenset())
		self.assertEqual(again.unresolved_transaction_ids, ())
		self.assertEqual(again.warnings, ())
		self.assertEqual(decision.state, SETTLED)
		self.assertEqual(decision.credited_native, 5_000_000)

	def test_a_still_unreadable_transaction_stays_pending_rather_than_settling(self):
		"""THE CONTROL. A retry that always resolves would be no check at all."""
		rail = OotleEsmeralda()
		unread = ObservationBatch(
			rail.key, "SALE", _COMPONENT, _INDEXER, 0, 201, 0, 201,
			(TransferObservation("%064x" % 201, 5_000_000, False, 0),),
			finalized_tip=201,
			unresolved_transaction_ids=("%064x" % 201,),
		)
		intent = PaymentIntent(
			"SALE", rail.key, _COMPONENT, 5_000_000, 1_000, 9_999_999_999,
			payment_reference="SALE",
			baseline=RecipientBaseline(
				rail.key, _COMPONENT, _INDEXER, 0, payment_component=_COMPONENT
			),
		)
		reader = _StubReader(b":\n")
		reader._get = lambda path: (
			({"network": "esmeralda", "epoch": 10776}, None)
			if path == "network"
			else (None, "the indexer answered 503 for " + path)
		)
		with mock.patch.object(OotleEsmeralda, "_reader", lambda _s, _c: reader):
			again = rail.observe(intent, {"endpoint": _INDEXER, "payment_component": _COMPONENT}, unread)
			decision = rail.settle(intent, again, frozenset())
		self.assertEqual(again.unresolved_transaction_ids, ("%064x" % 201,))
		self.assertEqual(decision.state, PENDING)

