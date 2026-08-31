"""Complete Ootle Esmeralda XTR payment observation and settlement.

The rail attributes committed deposits into the recipient account's XTR vault
to their transaction IDs and exact integer amounts. Ootle commits are final,
so an attributed deposit needs no later confirmation-depth or reorg gate.
Loyalty policy and points reads remain the independent
:class:`cryptopos_core.chain.OotleReader` API.
"""

# The distribution version, declared here because a moved module asks for
# it. `chain._default_user_agent` does `from . import __version__` to name
# itself to an indexer, and when this code lived in cryptopos-core that
# resolved to core's version. It now resolves to this package's, which is
# the honest answer: the operator of an endpoint being polled should be told
# which rail is polling it, not which protocol library it links against.
__version__ = "0.1.0"


import json
import re
import urllib.parse
from collections.abc import Mapping
from datetime import datetime, timezone

from .chain import OotleReader
from cryptopos_core.errors import InvalidRailPlugin, RailProviderError, _coerce_integer
from cryptopos_core.plugin import (
	ADDRESS_VALIDATION,
	NEEDS_REVIEW,
	OBSERVATION,
	PAYMENT_REQUEST,
	PENDING,
	SETTLED,
	SETTLEMENT,
	Asset,
	Network,
	ObservationBatch,
	PaymentIntent,
	PaymentRequest,
	Readiness,
	RecipientBaseline,
	SettlementDecision,
	TransferObservation,
)
from cryptopos_core.rails import RAILS

OOTLE_XTR_RESOURCE = "resource_" + "01" * 32
OOTLE_DEPOSIT_TOPIC = "std.vault.deposit"
_ACCOUNT = re.compile(r"^(?:account|component)_[0-9a-f]{32,64}$")
_TRANSACTION_ID = re.compile(r"^[0-9a-f]{64}$")
_EVENT_ID = re.compile(r"^[0-9]+$")


def _event_replay(payload, after_id):
	"""Decode complete SSE frames and return (events, last monotonic id)."""
	try:
		text = payload.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
	except (AttributeError, UnicodeDecodeError) as exception:
		raise ValueError(f"event stream was not UTF-8 bytes: {exception}") from None
	events = []
	cursor = after_id
	for frame in text.split("\n\n"):
		lines = [line for line in frame.split("\n") if line and not line.startswith(":")]
		if not lines:
			continue
		fields = {}
		data_lines = []
		for line in lines:
			field, separator, value = line.partition(":")
			if not separator:
				raise ValueError("event stream contained a field without a colon")
			if value.startswith(" "):
				value = value[1:]
			if field == "data":
				data_lines.append(value)
			elif field in ("event", "id"):
				if field in fields:
					raise ValueError(f"event stream repeated its {field} field")
				fields[field] = value
		if set(fields) != {"event", "id"} or not data_lines:
			raise ValueError("event stream frame did not contain event, id, and data")
		event_id_text = fields["id"]
		if not _EVENT_ID.fullmatch(event_id_text):
			raise ValueError("event stream id was not a non-negative decimal integer")
		event_id = int(event_id_text)
		if event_id <= cursor:
			raise ValueError("event stream ids were not strictly increasing after the cursor")
		try:
			body = json.loads("\n".join(data_lines))
		except json.JSONDecodeError as exception:
			raise ValueError(f"event stream data was not JSON: {exception}") from None
		cursor = event_id
		events.append((event_id, fields["event"], body))
	return tuple(events), cursor


def _summary_field(body, name):
	"""One field out of a transaction's summary, or None if it is not there.

	Total by construction: an unreadable body, a missing summary and a missing
	field are all "no answer", because the caller's only safe response to each
	is the same -- leave the timestamp unset and let settlement route the
	payment to review rather than credit it on a guess.
	"""
	if not isinstance(body, dict):
		return None
	transaction = body.get("transaction")
	if not isinstance(transaction, dict):
		return None
	summary = transaction.get("summary")
	if not isinstance(summary, dict):
		return None
	return summary.get(name)


def _finalized_epoch(value):
	"""Parse esmeralda's naive transaction timestamp as an explicit UTC time."""
	if not isinstance(value, str):
		return None
	try:
		parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
	except ValueError:
		return None
	# Measured 2026-08-31: esmeralda 0.39.3 emits a naive timestamp whose
	# clock is UTC. Explicitly attach UTC; interpreting it as local time was
	# D19 and made every rail's payments appear late.
	epoch = int(parsed.replace(tzinfo=timezone.utc).timestamp())
	return epoch if epoch >= 0 else None


class OotleEsmeralda:
	"""Attributed, final XTR deposits from one shared merchant account."""

	network = Network("ootle", "esmeralda", True)
	asset = Asset("native", "xtr", "EsmeraldaXTR", 6)
	key = f"{network.key}/{asset.key}"
	binding_category = RAILS["xtr"]["binding_category"]
	capabilities = frozenset({ADDRESS_VALIDATION, PAYMENT_REQUEST, OBSERVATION, SETTLEMENT})

	def validate_recipient(self, recipient):
		if not isinstance(recipient, str) or not _ACCOUNT.fullmatch(recipient):
			return "refused", "supported Ootle accounts are account_ or component_ plus 32-64 lowercase hex"
		return "unchecked", "the account shape is valid but Ootle account identifiers carry no local checksum"

	def readiness(self, configuration):
		ready = {ADDRESS_VALIDATION, PAYMENT_REQUEST, SETTLEMENT}
		unavailable = []
		try:
			reader = self._reader(configuration)
			self._network(reader)
		except RailProviderError as exception:
			unavailable.append((OBSERVATION, exception.reason))
		else:
			ready.add(OBSERVATION)
		return Readiness(self.key, frozenset(ready), tuple(unavailable))

	def capture_baseline(self, recipient, configuration):
		verdict, reason = self.validate_recipient(recipient)
		if verdict == "refused":
			raise RailProviderError(self.key, reason)
		reader = self._reader(configuration)
		self._network(reader)
		balance, balance_reason = reader.resource_balance(recipient, OOTLE_XTR_RESOURCE)
		if balance is None:
			raise RailProviderError(self.key, f"recipient balance could not be read: {balance_reason}")
		vault = self._vault(reader, recipient)
		_events, cursor = self._events(reader, vault, 0)
		return RecipientBaseline(self.key, recipient, reader.indexer, cursor, balance_native=balance)

	def create_request(self, intent):
		self._intent(intent)
		verdict, reason = self.validate_recipient(intent.recipient)
		if verdict == "refused":
			raise RailProviderError(self.key, reason)
		if intent.baseline is None:
			raise InvalidRailPlugin("Ootle requires an event-stream baseline before request creation")
		# Searched through 2026-08-30/31: Ootle has no registered payment URI.
		# Keep the old refusal's reason here as an explicit address instruction;
		# inventing a tari:// dialect would turn a truthful request into a fake one.
		notice = (
			"Ootle has no registered payment URI; this is an account address, not a deeplink. "
			f"Send exactly {intent.amount_native} microTari."
		)
		return PaymentRequest(self.key, intent.recipient, intent.recipient, intent.amount_native, notice)

	def observe(self, intent, configuration, previous=None):
		self._intent(intent)
		if intent.baseline is None or intent.baseline.tip is None:
			raise InvalidRailPlugin("Ootle observation requires a captured event-stream baseline")
		cursor = intent.baseline.tip
		if previous is not None:
			if not isinstance(previous, ObservationBatch):
				raise InvalidRailPlugin("previous observations have an unknown shape")
			previous.require_intent(intent)
			cursor = previous.observed_through_tip
		reader = self._reader(configuration)
		if intent.baseline.provider != reader.indexer:
			raise RailProviderError(self.key, "observation endpoint differs from the baseline endpoint")
		self._network(reader)
		vault = self._vault(reader, intent.recipient)
		events, through = self._events(reader, vault, cursor)
		transfers, warnings = self._transfers(reader, vault, events)
		page = ObservationBatch(
			self.key,
			intent.intent_id,
			intent.recipient,
			reader.indexer,
			intent.baseline.tip,
			through,
			cursor,
			through,
			transfers,
			warnings=warnings,
			finalized_tip=through,
		)
		return page if previous is None else previous.extend(page)

	def settle(self, intent, observations, claimed_transaction_ids=frozenset()):
		self._intent(intent)
		if not isinstance(observations, ObservationBatch):
			raise InvalidRailPlugin("observations have an unknown shape")
		observations.require_intent(intent)
		if not observations.complete:
			raise InvalidRailPlugin("settlement requires observations through the provider cursor")
		if not isinstance(claimed_transaction_ids, frozenset) or any(
			not isinstance(transaction_id, str) for transaction_id in claimed_transaction_ids
		):
			raise InvalidRailPlugin("claimed transaction ids must be a frozenset of text")
		claimed = [t for t in observations.transfers if t.transaction_id in claimed_transaction_ids]
		available = [t for t in observations.transfers if t.transaction_id not in claimed_transaction_ids]
		sighted = sum(transfer.amount_native for transfer in available)
		timely = [
			transfer
			for transfer in available
			if transfer.block_time_epoch is not None and transfer.block_time_epoch <= intent.expires_at_epoch
		]
		late = [transfer for transfer in available if transfer not in timely]
		credited = sum(transfer.amount_native for transfer in timely)
		if credited >= intent.amount_native:
			return SettlementDecision(
				SETTLED,
				credited,
				sighted,
				tuple(sorted(transfer.transaction_id for transfer in timely)),
				"committed Ootle deposits are final",
			)
		if claimed and sum(transfer.amount_native for transfer in claimed) + sighted >= intent.amount_native:
			return SettlementDecision(
				NEEDS_REVIEW,
				credited,
				sighted,
				reason="one or more observed transactions are already claimed by another intent",
			)
		if late and credited + sum(transfer.amount_native for transfer in late) >= intent.amount_native:
			return SettlementDecision(
				NEEDS_REVIEW,
				credited,
				sighted,
				reason="payment arrived after expiry or lacks a trustworthy block time",
			)
		reason = "payment is below the invoice amount" if sighted else "no payment observed"
		return SettlementDecision(PENDING, credited, sighted, reason=reason)

	def _events(self, reader, vault, cursor):
		query = urllib.parse.urlencode(
			{"substate_id": vault, "topic": OOTLE_DEPOSIT_TOPIC, "after_id": cursor}
		)
		payload, reason = reader._get_sse(f"transactions/events/stream?{query}")
		if payload is None:
			raise RailProviderError(self.key, f"deposit event stream could not be read: {reason}")
		try:
			return _event_replay(payload, cursor)
		except ValueError as exception:
			raise RailProviderError(self.key, str(exception)) from None

	def _transfers(self, reader, vault, events):
		# ObservationBatch and the host's claimed set identify money by unique
		# transaction ID. If one committed transaction emits multiple deposit
		# frames for this vault, preserve every exact amount by summing those
		# frames into its one atomically claimable transfer observation.
		amounts = {}
		for _event_id, topic, body in events:
			if topic != OOTLE_DEPOSIT_TOPIC:
				continue
			if (
				not isinstance(body, dict)
				or not isinstance(body.get("transaction_id"), str)
				or not _TRANSACTION_ID.fullmatch(body["transaction_id"])
			):
				raise RailProviderError(self.key, "deposit event transaction id was malformed")
			transaction_id = body["transaction_id"]
			event = body.get("event")
			if not isinstance(event, dict) or event.get("substate_id") != vault:
				raise RailProviderError(self.key, "deposit event was not bound to the requested vault")
			payload = event.get("payload")
			if not isinstance(payload, dict):
				raise RailProviderError(self.key, "deposit event payload was malformed")
			if payload.get("resource_address") != OOTLE_XTR_RESOURCE:
				continue
			amount = _coerce_integer(payload.get("amount"))
			if amount is None or amount <= 0:
				raise RailProviderError(self.key, "deposit event amount was not a positive integer")
			amounts[transaction_id] = amounts.get(transaction_id, 0) + amount

		transfers = []
		warnings = []
		for transaction_id, amount in amounts.items():
			body, _reason = reader._get(f"transactions/{transaction_id}")
			# MEASURED, not assumed. The indexer nests it two deep:
			#
			#   {"transaction": {"transaction_id": ..., "created_at": ...,
			#                    "summary": {"outcome": "Commit",
			#                                "total_fees_paid": ...,
			#                                "finalized_at": "2026-08-31 04:12:19.0"}}}
			#
			# Checked against three real esmeralda transactions on 2026-08-31 --
			# the faucet that opened this account and two customer payments --
			# and the shape was identical in all three. A flat
			# `body["finalized_at"]` reads None for every real transaction,
			# which routes an honest payment to review and never credits it.
			block_time = _finalized_epoch(_summary_field(body, "finalized_at"))
			if block_time is None:
				warnings.append(f"transaction {transaction_id} has no trustworthy finalized_at timestamp")
			transfers.append(TransferObservation(transaction_id, amount, True, 1, block_time_epoch=block_time))
		return tuple(transfers), tuple(warnings)

	def _vault(self, reader, recipient):
		vault, reason = reader.resource_vault(recipient, OOTLE_XTR_RESOURCE)
		if vault is None:
			detail = reason or "the recipient has no XTR vault"
			raise RailProviderError(self.key, f"recipient XTR vault could not be resolved: {detail}")
		return vault

	def _reader(self, configuration):
		if not isinstance(configuration, Mapping):
			raise RailProviderError(self.key, "configuration must be a mapping")
		indexer = configuration.get("endpoint")
		if not isinstance(indexer, str) or not indexer:
			raise RailProviderError(self.key, "an explicit Ootle indexer endpoint is required")
		timeout = configuration.get("timeout_seconds", 4.0)
		if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
			raise RailProviderError(self.key, "timeout_seconds must be greater than 0 and at most 30")
		return OotleReader(indexer=indexer, timeout=timeout)

	def _network(self, reader):
		body, reason = reader._get("network")
		if body is None:
			raise RailProviderError(self.key, reason)
		if not isinstance(body, dict) or body.get("network") != "esmeralda":
			raise RailProviderError(self.key, "indexer did not identify itself as esmeralda")
		epoch = body.get("epoch")
		if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
			raise RailProviderError(self.key, "indexer epoch was not a non-negative integer")
		return epoch

	def _intent(self, intent):
		if not isinstance(intent, PaymentIntent) or intent.rail_key != self.key:
			raise InvalidRailPlugin("payment intent belongs to another rail")


ootle_esmeralda = OotleEsmeralda()
