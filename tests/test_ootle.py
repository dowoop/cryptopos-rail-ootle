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
	OOTLE_DEPOSIT_TOPIC,
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
			return Response(self.streams.get(after_id, b":\n"))
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
		stream_url, timeout, accept = transport.calls[-1]
		query = urllib.parse.parse_qs(urllib.parse.urlsplit(stream_url).query)
		self.assertEqual(query["substate_id"], [VAULT])
		self.assertEqual(query["topic"], [OOTLE_DEPOSIT_TOPIC])
		self.assertEqual(query["after_id"], ["0"])
		self.assertEqual((timeout, accept), (2, "text/event-stream"))

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
			Transport(streams={0: event_frame(7) + b":\n"}),
		)
		self.assertIsNone(observed.transfers[0].block_time_epoch)
		self.assertIn("no trustworthy finalized_at", observed.warnings[0])
		self.assertEqual(self.rail.settle(self.intent(baseline), observed).state, NEEDS_REVIEW)

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
		self.assertIn("after expiry", late_decision.reason)

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
		transfers, warnings = rail._transfers(reader, VAULT, ((1, OOTLE_DEPOSIT_TOPIC, body),))
		self.assertEqual(transfers[0].amount_native, 1)
		self.assertEqual(warnings, ())


class SseTransportBoundaries(unittest.TestCase):
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
