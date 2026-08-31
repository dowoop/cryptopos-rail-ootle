"""Boundary cases for the Ootle indexer reader, moved out of cryptopos-core 2.0.

`chain.py` came to this package with the adapter that uses it, so the tests that
drive its defensive branches came too. Each one is a shape a broken or hostile
indexer can actually return, and every read here is total: nothing raises,
because a sale must never fail because a policy layer is down.
"""

import io
import unittest
from unittest import mock

from cryptopos_core.errors import CryptoPosError, InvalidRailPlugin, RailProviderError
from cryptopos_core.plugin import ObservationBatch, PaymentIntent, RecipientBaseline

from cryptopos_rail_ootle import OotleEsmeralda, chain
from cryptopos_rail_ootle.chain import OotleReader


class OotleBoundaries(unittest.TestCase):
	class Reader:
		def __init__(
			self,
			indexer="https://ootle.example",
			body=None,
			balance=100,
			reason=None,
			vault="vault_" + "e" * 64,
			stream=b":\n",
		):
			self.indexer = indexer
			self.body = {"network": "esmeralda", "epoch": 6} if body is None else body
			self.balance = balance
			self.reason = reason
			self.vault = vault
			self.stream = stream

		def _get(self, path):
			return self.body, self.reason

		def _get_sse(self, path):
			return self.stream, self.reason

		def resource_balance(self, recipient, resource):
			return self.balance, self.reason

		def resource_vault(self, recipient, resource):
			return self.vault, self.reason

	def setUp(self):
		self.rail = OotleEsmeralda()
		self.assertEqual(self.rail.asset.decimals, 6)
		self.account = "component_" + "a" * 32
		self.baseline = RecipientBaseline(
			self.rail.key, self.account, "https://ootle.example", 5, balance_native=90
		)
		self.intent = PaymentIntent("sale", self.rail.key, self.account, 10, 100, 200, baseline=self.baseline)

	def test_invalid_account_and_missing_balance_baseline_are_refused(self):
		self.assertEqual(self.rail.validate_recipient(1)[0], "refused")
		with self.assertRaises(RailProviderError):
			self.rail.capture_baseline("bad", {})
		without_baseline = PaymentIntent("sale", self.rail.key, self.account, 10, 100, 200)
		with self.assertRaises(InvalidRailPlugin):
			self.rail.observe(without_baseline, {})

	def test_observation_revalidates_previous_provider_and_event_stream(self):
		previous = ObservationBatch(
			self.rail.key,
			"sale",
			self.account,
			"https://ootle.example",
			5,
			6,
			5,
			6,
			(),
		)
		with mock.patch.object(self.rail, "_reader", return_value=self.Reader()):
			observed = self.rail.observe(self.intent, {}, previous)
		self.assertEqual(observed.observed_through_tip, 6)
		for reader in (
			self.Reader(indexer="https://other.example"),
			self.Reader(stream=None, reason="unreadable"),
			self.Reader(vault=None),
		):
			with self.subTest(reader=reader), mock.patch.object(self.rail, "_reader", return_value=reader):
				with self.assertRaises(RailProviderError):
					self.rail.observe(self.intent, {})

	def test_network_epoch_does_not_replace_the_event_cursor(self):
		for epoch in (5, 6):
			reader = self.Reader(body={"network": "esmeralda", "epoch": epoch})
			with (
				self.subTest(epoch=epoch),
				mock.patch.object(self.rail, "_reader", return_value=reader),
			):
				observed = self.rail.observe(self.intent, {})
			self.assertEqual(observed.observed_after_tip, 5)
			self.assertEqual(observed.observed_through_tip, 5)

	def test_reader_and_network_inputs_are_strict(self):
		for configuration in (
			None,
			{},
			{"endpoint": "https://ootle.example", "timeout_seconds": 0},
		):
			with self.subTest(configuration=configuration), self.assertRaises(RailProviderError):
				self.rail._reader(configuration)
		for reader in (
			self.Reader(body=None, reason="offline"),
			self.Reader(body={"network": "esmeralda", "epoch": True}),
		):
			if reader.reason:
				reader.body = None
			with self.subTest(reader=reader), self.assertRaises(RailProviderError):
				self.rail._network(reader)
		with self.assertRaises(InvalidRailPlugin):
			self.rail._intent(object())
		with self.assertRaises(RailProviderError):
			self.rail._reader({"endpoint": 1})
		self.assertEqual(
			self.rail._reader({"endpoint": "https://ootle.example", "timeout_seconds": 30}).timeout,
			30,
		)
		self.assertEqual(
			self.rail._reader({"endpoint": "https://ootle.example", "timeout_seconds": 1}).timeout,
			1,
		)
		with self.assertRaises(RailProviderError):
			self.rail._reader({"endpoint": "https://ootle.example", "timeout_seconds": 30.0001})
		self.assertEqual(self.rail._network(self.Reader(body={"network": "esmeralda", "epoch": 0})), 0)
		with self.assertRaises(RailProviderError):
			self.rail._network(self.Reader(body={"network": "esmeralda", "epoch": -1}))

class ChainDefensiveBranches(unittest.TestCase):
	def test_malformed_vault_mapping_returns_a_total_failure_reason(self):
		amount, reason = chain._balance_of({"substate": {"Vault": {"resource_container": {"Stealth": None}}}})
		self.assertIsNone(amount)
		self.assertIn("shape", reason)
	def test_resource_pair_walker_handles_the_resource_in_the_second_slot(self):
		pair = [{"value": {"hex": "vault"}}, {"value": {"hex": "resource"}}]
		self.assertEqual(chain._walk_for_resource(pair, "resource"), "vault_vault")
