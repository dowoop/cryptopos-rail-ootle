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
__version__ = "0.2.1"


import json
import re
import urllib.parse
from collections.abc import Mapping
from datetime import datetime, timezone

from .chain import OotleReader
from cryptopos_core.errors import InvalidRailPlugin, RailProviderError
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

#: The event a `Payments` component emits, and the whole of the per-sale
#: binding. **The prefix is the template's STRUCT name, not a namespace we
#: chose** -- measured on esmeralda, where the deployed loyalty contract's
#: `emit_event("PointsIssued", ...)` is indexed as `Loyalty.PointsIssued` and
#: filtering on the bare event name returns an empty stream. An empty stream
#: looks exactly like "custom events are not indexed", which is why this is
#: pinned here with the reason rather than assembled at a call site.
OOTLE_PAYMENT_TOPIC = "Payments.PaymentReceived"

#: How long a sale reference may be, matching the component's own bound. A
#: longer one cannot have come from that component, so it is a wrong-shaped
#: answer rather than a payment.
MAX_SALE_REF_BYTES = 128

#: The one transaction outcome whose deposits are money. Everything else --
#: `Abort`, `Reject`, a renamed field, an indexer that answered an error page
#: -- is a transaction that did not commit, and a deposit event from one is
#: not a payment. See :func:`_observed_transfer` for what this cost.
_COMMITTED = "Commit"

#: How many replay pages `capture_baseline` will read before refusing to
#: guess. Twelve, because each page is bounded by the reader's own timeout
#: (4 seconds by default) and a baseline that cost most of a minute has
#: already stopped being a till. See :meth:`OotleEsmeralda._drain`.
_MAX_BASELINE_PAGES = 12

#: How far a chain's clock may disagree with this host's before a deposit is
#: read as predating its sale. Generous on purpose -- D19 was a nine-hour
#: timezone error that made every payment look late and was invisible to a
#: fully green suite, so a tight bound here would be the same defect wearing
#: the opposite sign.
_CLOCK_SKEW_SECONDS = 3600
_ACCOUNT = re.compile(r"^(?:account|component)_[0-9a-f]{32,64}$")
_TRANSACTION_ID = re.compile(r"^[0-9a-f]{64}$")
_EVENT_ID = re.compile(r"^[0-9]+$")


# INLINED, NOT IMPORTED, and that is a packaging decision rather than a style
# one. This module used to do `from cryptopos_core.errors import
# _coerce_integer`: a leading-underscore name is by convention not part of
# core's public surface, so a resolver honouring this package's own
# `cryptopos-core>=2,<3` range is free to install a later 2.x that has moved
# or renamed it -- producing an install that succeeds and a rail that fails on
# import. A twenty-line helper is cheaper than a dependency on somebody else's
# private detail, and it is copied verbatim so the two cannot disagree about
# what an amount is.
def _coerce_integer(value):
	"""Return an exact integer form, or None without truncating a value.

	Host form fields arrive as strings, so integer strings are accepted. Floats
	are not: `int(1.9) == 1` is a lossy conversion a money boundary must never
	perform silently. Booleans are integers to Python and amounts to nobody.
	"""
	if isinstance(value, bool):
		return None
	if isinstance(value, int):
		return value
	if isinstance(value, str):
		try:
			return int(value.strip())
		except ValueError:
			return None
	return None


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


class _Unresolved:
	"""A transfer whose status could not be established, and why.

	Distinct from a REFUSED one on purpose. A refusal is a decision the chain
	made -- the transaction aborted, so no money moved. This is the absence of
	a decision: the money is real and the indexer did not say. Settlement must
	not turn the second into a terminal state, because `needs-review` never
	reopens (D10) and a transient 503 would otherwise cost a customer a sale
	that was correctly paid.
	"""

	__slots__ = ("reason", "transfer")

	def __init__(self, transfer, reason):
		self.transfer = transfer
		self.reason = reason


def _observed_transfer(reader, transaction_id, amount):
	"""One transfer fact, and the reason it may not be creditable.

	ONE implementation on purpose. The shared-account path and the
	payment-component path both need this rule, and D35 is what a rule in two
	places costs: the copy nobody searched for kept the defect.

	The indexer nests the answer two deep, MEASURED and not assumed::

		{"transaction": {
			"transaction_id": ..., "created_at": ...,
			"summary": {
				"outcome": "Commit",
				"total_fees_paid": ...,
				"finalized_at": "2026-08-31 04:12:19.0"}}}

	Checked against three real esmeralda transactions on 2026-08-31 -- the
	faucet that opened this account and two customer payments -- and the shape
	was identical in all three. A flat ``body["finalized_at"]`` reads None for
	every real transaction, which routes an honest payment to review and never
	credits it.

	**The outcome is read, and until 2026-08-31 it was not.** ``settle`` says
	"committed Ootle deposits are final" -- and finality is a property of a
	COMMITTED transaction, which nothing here had ever checked. Reproduced on
	both paths: a summary reading ``{"outcome": "Abort"}`` beside a valid
	timestamp settled a 5,000,000 microTari sale and booked it, under a reason
	asserting the very thing that was false. That is this project's recurring
	shape -- a true condition wearing a false sentence (D25, D38, D39, D40).

	A transaction the indexer does not call committed is still REPORTED, never
	silently dropped: an indexer wrong about an abort would otherwise rob a
	customer who really paid. It is reported unconfirmed and carries no block
	time, so settlement cannot count it, and the warning names the outcome so
	an operator reviewing the sale sees why.

	A missing or unreadable outcome is treated the same as a refused one. That
	is deliberately fail-safe in the direction of not booking goods: if a
	future indexer renames the field, every payment goes to review with the
	reason on it, rather than every payment settling on an unread guarantee.
	"""
	body, reason = reader._get(f"transactions/{transaction_id}")
	if body is None:
		# COULD NOT READ, WHICH IS NOT A VERDICT. Until this was separated out
		# a transport failure and an `Abort` produced the same answer under the
		# same sentence -- "an uncommitted transaction moved no money" -- which
		# the code had no evidence for. One HTTP 503 on this read turned an
		# honest committed payment into a permanent refusal, because a
		# `needs-review` never reopens (D10) and the indexer recovering a
		# second later changes nothing.
		#
		# `unresolved` says the money is real and its status is unknown, so
		# settlement keeps polling instead of deciding. The sale's own expiry
		# ends it if the reads never recover, which is the honest outcome: a
		# decision was never available, so none was taken.
		return _Unresolved(
			TransferObservation(transaction_id, amount, False, 0),
			f"transaction {transaction_id} could not be read, so whether it committed is "
			f"unknown: {reason}",
		)
	# THE BODY MUST BE THE TRANSACTION THAT WAS ASKED FOR. A cache or a proxy
	# answering with a different transaction would otherwise pair this event's
	# amount with that transaction's outcome and timestamp, and the check above
	# would certify money it had never looked at.
	inner = body.get("transaction") if isinstance(body, dict) else None
	answered = inner.get("transaction_id") if isinstance(inner, dict) else None
	if isinstance(answered, str) and answered != transaction_id:
		return _Unresolved(
			TransferObservation(transaction_id, amount, False, 0),
			f"the indexer was asked for transaction {transaction_id} and answered with "
			f"{answered}, so this deposit's outcome is unknown",
		)
	outcome = _summary_field(body, "outcome")
	if outcome != _COMMITTED:
		named = repr(outcome) if isinstance(outcome, str) else "no outcome at all"
		return (
			TransferObservation(transaction_id, amount, False, 0),
			f"the indexer reports transaction {transaction_id} as {named}, not committed; "
			"an uncommitted transaction moved no money and cannot settle a sale",
		)
	block_time = _finalized_epoch(_summary_field(body, "finalized_at"))
	if block_time is None:
		return (
			TransferObservation(transaction_id, amount, True, 1),
			f"transaction {transaction_id} has no trustworthy finalized_at timestamp",
		)
	return TransferObservation(transaction_id, amount, True, 1, block_time_epoch=block_time), None


def _observed_transfers(reader, amounts):
	"""Every summed transaction, resolved through the one rule above.

	Returns (transfers, warnings, unresolved_transaction_ids). The third value
	is what stops a read failure becoming a verdict: settlement keeps those
	sales pending rather than sending them to a review they can never leave.
	"""
	transfers = []
	warnings = []
	unresolved = []
	for transaction_id, amount in amounts.items():
		answer = _observed_transfer(reader, transaction_id, amount)
		if isinstance(answer, _Unresolved):
			transfers.append(answer.transfer)
			warnings.append(answer.reason)
			unresolved.append(answer.transfer.transaction_id)
			continue
		transfer, warning = answer
		transfers.append(transfer)
		if warning is not None:
			warnings.append(warning)
	return tuple(transfers), tuple(warnings), tuple(unresolved)


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
		component = self._payment_component(configuration)
		if component:
			# ONE PAGE, and the asymmetry with the branch below is the point.
			#
			# On this path the money names the sale: a payer passes the sale
			# reference to the component's `pay` method and `_referenced_payments`
			# credits nothing else. A reference is minted per sale with 57 bits
			# of entropy, so no event that already existed can carry the one
			# this sale is about to use. The cursor is therefore an OPTIMISATION
			# here -- it saves re-reading history -- and not the thing that
			# decides whose money this is. A short one costs a little repeated
			# work and cannot misattribute a payment.
			#
			# Draining here was actively harmful: every baseline replays from
			# zero, so a component would have hit the page bound after roughly
			# its sixtieth event and then refused EVERY later sale. That gave
			# the rail a finite operational lifetime, on the one path this
			# project actually publishes. Reproduced 2026-08-31.
			_events, cursor = self._payment_events(reader, component, 0)
		else:
			# DRAINED, because here the cursor is the whole of the attribution.
			# Deposits into a shared account carry no sale reference, so
			# `settle` credits by running total and a cursor that stops short
			# hands a new sale money that arrived before it existed.
			vault = self._vault(reader, recipient)
			cursor = self._drain(lambda after: self._events(reader, vault, after))
		return RecipientBaseline(
			self.key, recipient, reader.indexer, cursor,
			balance_native=balance, payment_component=component,
		)

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
		# From the BASELINE, not from configuration: `create_request` receives
		# only an intent, and the adapter that captured the baseline is the one
		# that knew the component.
		component = getattr(intent.baseline, "payment_component", "") or ""
		if component:
			# A DIFFERENT INSTRUCTION, because a plain transfer to this address
			# would land in the component's vault carrying no sale reference and
			# would therefore never be observed. Saying "send to this address"
			# here would take real money and never credit it.
			sale_ref = self._sale_reference(intent)
			notice = (
				"Pay by calling this component's `pay` method with the sale reference "
				f"{sale_ref!r}; a plain transfer to this address names no sale and will "
				f"not be credited. Send exactly {intent.amount_native} microTari."
			)
			return PaymentRequest(self.key, component, component, intent.amount_native, notice)
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
		# RE-ASK ABOUT WHAT WAS UNREADABLE LAST TIME, before reading anything new.
		#
		# Without this the fix for a transient read failure is only half a fix.
		# The cursor advances past an event once it has been seen, and `extend`
		# refuses a page that repeats a transaction id, so a transaction whose
		# status could not be established is never looked at again: the sale
		# stops being wrongly REFUSED and starts quietly EXPIRING instead,
		# which costs the customer exactly as much. The doubt has to be retried
		# where it was recorded.
		previous = self._resolve_outstanding(reader, previous)
		component = self._payment_component(configuration)
		if component:
			sale_ref = self._sale_reference(intent)
			events, through = self._payment_events(reader, component, cursor)
			transfers, warnings, unresolved = self._referenced_payments(
				reader, component, events, sale_ref
			)
		else:
			vault = self._vault(reader, intent.recipient)
			events, through = self._events(reader, vault, cursor)
			transfers, warnings, unresolved = self._transfers(reader, vault, events)
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
			unresolved_transaction_ids=unresolved,
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
		# BOTH ENDS OF THE WINDOW. Until 2026-08-31 this tested only the upper
		# one, so a deposit dated a DAY BEFORE the sale existed settled it --
		# reproduced. The cursor is supposed to make that impossible, but
		# `_get_sse` treats a timeout with frames in hand as the end of a
		# replay and cannot tell a finished replay from a truncated one, so a
		# short baseline over a long history puts pre-existing money after the
		# cursor. A payment made before a sale existed cannot be a payment for
		# it, whatever the cursor says.
		#
		# `_CLOCK_SKEW_SECONDS` and not zero, because this is exactly D19's
		# shape: a wall-clock comparison between a chain's timestamp and a
		# host's, which was nine hours out once and made every payment look
		# late. An hour is far wider than any real skew and far narrower than
		# the history this is defending against.
		earliest = intent.created_at_epoch - _CLOCK_SKEW_SECONDS
		timely = [
			transfer
			for transfer in available
			if transfer.block_time_epoch is not None
			and earliest <= transfer.block_time_epoch <= intent.expires_at_epoch
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
		# AN UNREAD TRANSACTION IS NOT A VERDICT, so it must not produce one.
		#
		# `needs-review` is terminal (D10) and the sweep never reopens it, so a
		# single HTTP 503 on the transaction read used to cost a customer a
		# sale they had correctly paid -- the indexer recovering a second later
		# changed nothing. Reproduced 2026-08-31 by a review of this session's
		# own work.
		#
		# Checked BEFORE the late branch, because an unresolved transfer has no
		# block time and would otherwise be swept up as "late" and given a
		# reason that names a cause nobody established. Pending is the honest
		# answer: keep polling, and let the sale's own expiry end it if the
		# reads never recover. Nothing is credited either way.
		unresolved = [
			transfer
			for transfer in available
			if transfer.transaction_id in observations.unresolved_transaction_ids
		]
		# No `credited < amount` guard: the settled branch above has already
		# returned by this point, so it could only ever be true. It was there,
		# it was redundant, and a mutation gate is what said so.
		if unresolved:
			return SettlementDecision(
				PENDING,
				credited,
				sighted,
				reason=(
					"the provider did not answer whether "
					f"{len(unresolved)} observed transaction(s) committed, so nothing is "
					"decided yet; this is retried, not refused"
				),
			)
		if late and credited + sum(transfer.amount_native for transfer in late) >= intent.amount_native:
			# NOT the house phrase the other three rails use, and deliberately.
			# "arrived after expiry" was already false for half of what lands
			# here -- D57 bounded the window at BOTH ends, so a deposit dated
			# before the sale existed is `late` too -- and the outcome check
			# adds a third cause. The specific one per transaction is in
			# `observations.warnings`; this sentence must not name only one.
			return SettlementDecision(
				NEEDS_REVIEW,
				credited,
				sighted,
				reason=(
					"payment falls outside the sale's window, is not committed, "
					"or lacks a trustworthy block time"
				),
			)
		reason = "payment is below the invoice amount" if sighted else "no payment observed"
		return SettlementDecision(PENDING, credited, sighted, reason=reason)

	def _resolve_outstanding(self, reader, previous):
		"""Re-read the transactions a previous poll could not establish.

		Returns a batch with those transfers replaced by whatever the provider
		says now, or `previous` unchanged when there is nothing outstanding --
		which is every poll on a healthy endpoint, so this costs nothing in the
		ordinary case.

		It replaces rather than appends because the transaction is the same
		one: `extend` refuses a page repeating a transaction id, and rightly,
		since two entries for one transaction would be counted twice.
		"""
		if previous is None or not previous.unresolved_transaction_ids:
			return previous
		outstanding = set(previous.unresolved_transaction_ids)
		amounts = {
			transfer.transaction_id: transfer.amount_native
			for transfer in previous.transfers
			if transfer.transaction_id in outstanding
		}
		refreshed, warnings, still_unresolved = _observed_transfers(reader, amounts)
		replaced = {transfer.transaction_id: transfer for transfer in refreshed}
		return ObservationBatch(
			previous.rail_key,
			previous.intent_id,
			previous.recipient,
			previous.provider,
			previous.baseline_tip,
			previous.tip,
			previous.observed_after_tip,
			previous.observed_through_tip,
			tuple(replaced.get(t.transaction_id, t) for t in previous.transfers),
			previous.unattributed_native,
			# The old doubts go with the old answers: a warning naming a read
			# that has since succeeded is a warning about nothing.
			tuple(
				value
				for value in previous.warnings
				if not any(identifier in value for identifier in outstanding)
			)
			+ warnings,
			previous.finalized_tip,
			still_unresolved,
		)

	def _drain(self, page):
		"""The true end of a SHARED account's history, or a refusal.

		Only the shared-account path calls this, and only because that path
		has no other attribution: deposits carry no sale reference, `settle`
		credits by running total, and a baseline cursor is the whole of the
		claim *nothing before this point belongs to this sale*. A single
		bounded read cannot make that claim true -- `_get_sse` returns what it
		has when its budget runs out, so a busy account hands back a cursor in
		the middle of its own past and the next poll reads pre-existing money
		as a payment. So: read, advance, read again, and stop when a page adds
		nothing.

		**A page that adds nothing is now an empty page, not a failed read.**
		The first version of this stopped on `payload is None`, which
		`_get_sse` returned for an idle stream AND for an HTTP 503 -- so a
		transient 503 on page two ended the drain early and a 30-minute-old
		deposit settled a new sale. Reproduced. `chain._get_sse` distinguishes
		them now and `_replay` refuses anything that failed, so this loop can
		only ever be ended by a real answer.

		The bound stays, and it is a refusal rather than a guess: a history
		this rail cannot get to the end of is one where no cursor can be
		trusted, and D5 already says a shared account cannot be made safe by
		bookkeeping. The remedy is named in the message because it exists --
		configure a payment component and attribution stops depending on a
		cursor at all.
		"""
		cursor = 0
		for _page in range(_MAX_BASELINE_PAGES):
			events, cursor = page(cursor)
			if not events:
				return cursor
		raise RailProviderError(
			self.key,
			f"the shared account's deposit history did not end within {_MAX_BASELINE_PAGES} "
			"replay pages, so no baseline can be trusted and nothing may be charged against "
			"it; configure a payment component, which binds each payment to its own sale and "
			"needs no history at all",
		)

	def _events(self, reader, vault, cursor):
		return self._replay(reader, vault, OOTLE_DEPOSIT_TOPIC, cursor, "deposit")

	def _replay(self, reader, substate_id, topic, cursor, noun):
		"""One bounded page of an event stream, resumed from `cursor`.

		**A read that fails is a refusal, always, at every attempt.** This
		method used to take a `silence_ok` flag that let a drain treat
		`payload is None` as the end of a history once a page had come back.
		That conflated two different answers, because `_get_sse` returned the
		same `None` for an idle stream and for an HTTP 503 -- and the drain
		then accepted a 503 on its second page as the end of the account's
		history. A deposit that predated the sale arrived after the short
		cursor and settled it. Reproduced 2026-08-31, found by a review of
		this session's own work.

		The distinction now lives where it belongs, in `chain._get_sse`: a
		connection that was established and then went quiet returns empty
		bytes, and one that was never established returns None. So an empty
		history is an empty PAGE here, and there is nothing left for a flag
		to be wrong about.
		"""
		query = urllib.parse.urlencode(
			{"substate_id": substate_id, "topic": topic, "after_id": cursor}
		)
		payload, reason = reader._get_sse(f"transactions/events/stream?{query}")
		if payload is None:
			raise RailProviderError(self.key, f"{noun} event stream could not be read: {reason}")
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

		return _observed_transfers(reader, amounts)

	def _payment_component(self, configuration):
		"""The `Payments` component this rail observes, or "" for the old path.

		Configuration, not a constant, because one deployment publishes one
		component and another publishes its own. When it is absent the rail
		keeps its shared-account behaviour unchanged -- and that behaviour is
		`not-unconditional`, which is what D48 measured and what the README
		warns about in a box.
		"""
		component = configuration.get("payment_component")
		if component is None or component == "":
			return ""
		if not isinstance(component, str) or not _ACCOUNT.fullmatch(component):
			raise RailProviderError(self.key, "payment_component must be an Ootle component address")
		return component

	def _payment_events(self, reader, component, cursor):
		"""Deposits into the payment component, each naming its own sale."""
		return self._replay(reader, component, OOTLE_PAYMENT_TOPIC, cursor, "payment")

	def _referenced_payments(self, reader, component, events, sale_ref):
		"""Only the payments that NAME this sale. Nothing is inferred.

		This is the whole difference from `_transfers`. There, every unclaimed
		deposit into a shared vault was a candidate and the running total
		decided; a payment made for another sale could settle this one purely
		by polling first. Here a payment carries the reference its payer put
		on it, so a payment for another sale is simply not in this list.

		A malformed event is a REFUSAL, not a skip. An event this build cannot
		read might be a payment for this sale, and silently dropping it would
		under-credit a customer who really paid.
		"""
		amounts = {}
		for _event_id, topic, body in events:
			if topic != OOTLE_PAYMENT_TOPIC:
				continue
			if (
				not isinstance(body, dict)
				or not isinstance(body.get("transaction_id"), str)
				or not _TRANSACTION_ID.fullmatch(body["transaction_id"])
			):
				raise RailProviderError(self.key, "payment event transaction id was malformed")
			event = body.get("event")
			if not isinstance(event, dict) or event.get("substate_id") != component:
				raise RailProviderError(self.key, "payment event was not bound to the requested component")
			payload = event.get("payload")
			if not isinstance(payload, dict):
				raise RailProviderError(self.key, "payment event payload was malformed")
			reference = payload.get("sale_ref")
			if not isinstance(reference, str) or not reference:
				raise RailProviderError(self.key, "payment event carried no sale reference")
			if len(reference.encode("utf-8")) > MAX_SALE_REF_BYTES:
				raise RailProviderError(self.key, "payment event sale reference was oversized")
			amount = _coerce_integer(payload.get("amount"))
			if amount is None or amount <= 0:
				raise RailProviderError(self.key, "payment event amount was not a positive integer")
			# THE BINDING, in one line. A payment for another sale is skipped
			# here and can never reach settlement, whatever its amount and
			# whichever sale polls first.
			if reference != sale_ref:
				continue
			transaction_id = body["transaction_id"]
			amounts[transaction_id] = amounts.get(transaction_id, 0) + amount

		return _observed_transfers(reader, amounts)

	def _sale_reference(self, intent):
		"""What the payer must name. The host's own reference for the sale."""
		reference = getattr(intent, "payment_reference", "") or ""
		if not isinstance(reference, str) or not reference:
			raise RailProviderError(
				self.key,
				"a payment component requires the sale's payment_reference; without one "
				"the payer has nothing to name and the binding does not exist",
			)
		if len(reference.encode("utf-8")) > MAX_SALE_REF_BYTES:
			raise RailProviderError(self.key, "payment_reference is longer than the component accepts")
		return reference

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
