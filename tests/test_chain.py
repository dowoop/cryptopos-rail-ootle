"""Chain reads: the slot mapping, and the promise that nothing here raises.

Every test uses a fake transport. There is no network in this file, and there
must never be one -- a suite that needs an indexer cannot assert the thing
that matters most, which is what happens when the indexer is gone.

The contract under test is one sentence: a sale must never fail because the
policy layer is down. In code that means every method returns a sentinel and
a reason, for every shape of failure, always.
"""

import io
import json
import unittest
import urllib.error
import urllib.request
from unittest import mock

import cryptopos_rail_ootle.chain as chain
from cryptopos_rail_ootle.chain import OotleReader

COMPONENT = "component_" + "ab" * 16
POINTS_HEX = "cc" * 16
ENTITLEMENT_HEX = "dd" * 16
VAULT_HEX = "ee" * 16
ACCOUNT = "account_" + "ff" * 16

# A non-zero, distinctive figure: a slot read one position off would return
# the window epoch or a ceiling, and both are numbers too.
COMMITTED_THIS_EPOCH = 4242


def resource_entry(hexed):
	return {"value": {"hex": hexed}}


def component_body(
	rate=100,
	per_issue=1_000_000,
	per_epoch=10_000_000,
	epoch=10387,
	points_hex=POINTS_HEX,
	slots=None,
):
	"""A component substate in the shape the deployed K1 component answers in.

	The numbers are the ones the live contract returns, so a fixture that
	drifts from reality is visible as a wrong number rather than as an
	abstract shape nobody recognises.
	"""
	# Positions written as LITERALS, deliberately. Filling this from
	# `chain.SLOT_*` would move the fixture whenever a constant moved, so the
	# constants would be checked against themselves and any of them could be
	# wrong by one without a single test noticing. These indices are the
	# layout confirmed on chain; `SlotLayout` below is what ties the two
	# together.
	state = slots
	if state is None:
		state = [None] * 15
		state[5] = resource_entry(points_hex)  # points resource
		state[6] = resource_entry(ENTITLEMENT_HEX)  # entitlement resource
		state[7] = resource_entry("11" * 16)  # enrolment resource
		state[8] = resource_entry("22" * 16)  # vault-claim resource
		state[9] = rate  # redemption rate
		state[10] = [per_issue, 0]  # per-award ceiling
		state[11] = [per_epoch, 0]  # per-epoch ceiling
		state[12] = epoch  # window epoch
		state[13] = [COMMITTED_THIS_EPOCH, 0]  # committed this epoch
	return {
		"version": 7,
		"substate": {
			"Component": {
				"header": {"owner_rule": "OwnerRule::None"},
				"body": {"state": state},
			}
		},
	}


def account_body(pairs):
	"""An account's vault map: (resource, vault) entries the walker looks for."""
	return {"substate": {"Component": {"body": {"state": [list(pair) for pair in pairs]}}}}


def vault_body(amount):
	return {"substate": {"Vault": {"resource_container": {"Fungible": {"amount": [amount, 0]}}}}}


class FakeTransport:
	"""Answers by URL suffix. Anything unmapped raises, as a dead host would."""

	def __init__(self, routes=None, error=None):
		self.routes = routes or {}
		self.error = error
		self.requested = []

	def __call__(self, request, timeout=None):
		url = request.full_url if hasattr(request, "full_url") else request
		self.requested.append(url)
		if self.error is not None:
			raise self.error
		for suffix, body in self.routes.items():
			if url.endswith(suffix):
				if isinstance(body, Exception):
					raise body
				payload = body if isinstance(body, (bytes, str)) else json.dumps(body)
				if isinstance(payload, str):
					payload = payload.encode("utf-8")
				return _Response(payload)
		raise urllib.error.URLError("nothing mapped for " + url)


class _Response(io.BytesIO):
	def __enter__(self):
		return self

	def __exit__(self, *exc):
		self.close()
		return False


def http_error(code, message):
	# Closed immediately: HTTPError backs itself with a temp file, and one
	# left open surfaces later as a ResourceWarning from tempfile that reads
	# like a leak in this package rather than an artefact of the fixture.
	# Only .code is read downstream, and that survives the close.
	error = urllib.error.HTTPError("https://x.example", code, message, {}, io.BytesIO(b""))
	error.close()
	return error


def reading(transport):
	# Patches the package's own seam, not urllib's. `chain._urlopen` is the
	# single door this module reaches a network through, so patching it is
	# what makes "no test here touches a network" a fact rather than a hope.
	return mock.patch("cryptopos_rail_ootle.chain._urlopen", transport)


class Construction(unittest.TestCase):
	def test_defaults_to_the_public_indexer(self):
		self.assertEqual(OotleReader().indexer, chain.DEFAULT_INDEXER)

	def test_strips_a_trailing_slash(self):
		# Otherwise every URL this builds carries a double slash, which some
		# reverse proxies answer with a redirect and some with a 404.
		self.assertEqual(OotleReader(indexer="https://x.example/").indexer, "https://x.example")

	def test_embedded_credentials_are_refused_before_they_can_be_returned_as_provenance(self):
		reader = OotleReader(indexer="https://user:secret@x.example", loyalty_component=COMPONENT)
		facts, reason = reader.promise()
		self.assertIsNone(facts)
		self.assertIn("credentials", reason)

	def test_allow_insecure_permits_http_only_not_arbitrary_url_schemes(self):
		for indexer in ("file:///etc/passwd", "ftp://x.example/policy"):
			with self.subTest(indexer=indexer):
				reader = OotleReader(indexer=indexer, allow_insecure=True)
				available, reason = reader.available()
				self.assertFalse(available)
				self.assertIn("https://", reason)

	def test_a_malformed_url_is_a_read_failure_not_an_exception(self):
		reader = OotleReader(indexer="https://[broken", loyalty_component=COMPONENT)
		facts, reason = reader.promise()
		self.assertIsNone(facts)
		self.assertIn("not a valid URL", reason)

	def test_blank_configuration_falls_back_rather_than_producing_none(self):
		reader = OotleReader(indexer="", loyalty_component=None, loyalty_points_resource=None)
		self.assertEqual(reader.indexer, chain.DEFAULT_INDEXER)
		self.assertEqual(reader.loyalty_component, "")
		self.assertEqual(reader.loyalty_points_resource, "")

	def test_malformed_configuration_degrades_without_raising(self):
		reader = OotleReader(indexer=7, loyalty_component=[], loyalty_points_resource={"bad": "shape"})
		self.assertEqual(reader.loyalty_points_resource, "")
		self.assertIn("must be text", reader.available()[1])
		self.assertIn("must be text", reader.promise()[1])

	def test_malformed_user_agent_falls_back_to_text(self):
		reader = OotleReader(user_agent=7)
		self.assertIsInstance(reader.user_agent, str)
		self.assertTrue(reader.user_agent.startswith("cryptopos-rail-ootle/"))

	def test_identifies_itself(self):
		# An indexer operator gets to see who is reading.
		from cryptopos_rail_ootle import __version__

		self.assertEqual(OotleReader().user_agent, "cryptopos-rail-ootle/" + __version__)


class Availability(unittest.TestCase):
	def test_reports_the_network_when_the_indexer_answers(self):
		with reading(FakeTransport({"/network": {"network": "testnet"}})):
			self.assertEqual(OotleReader().available(), (True, "testnet"))

	def test_reports_unreachable_without_raising(self):
		with reading(FakeTransport(error=urllib.error.URLError("refused"))):
			ok, reason = OotleReader().available()
		self.assertFalse(ok)
		self.assertIn("did not answer", reason)


class Promise(unittest.TestCase):
	def reader(self, **kwargs):
		kwargs.setdefault("loyalty_component", COMPONENT)
		return OotleReader(**kwargs)

	def test_reads_every_slot_the_surface_must_display(self):
		with reading(FakeTransport({COMPONENT: component_body()})):
			facts, reason = self.reader().promise()
		self.assertIsNone(reason)
		self.assertEqual(facts["redemption_rate"], 100)
		self.assertEqual(facts["per_issue_ceiling"], 1_000_000)
		self.assertEqual(facts["per_epoch_ceiling"], 10_000_000)
		self.assertEqual(facts["window_epoch"], 10387)
		# A distinctive figure, not 0: reading one slot over would land on the
		# window epoch or a ceiling, and a zero would have matched those too.
		self.assertEqual(facts["committed_this_epoch"], COMMITTED_THIS_EPOCH)
		self.assertEqual(facts["points_resource"], "resource_" + POINTS_HEX)
		self.assertEqual(facts["entitlement_resource"], "resource_" + ENTITLEMENT_HEX)
		self.assertEqual(facts["component"], COMPONENT)
		self.assertEqual(facts["version"], 7)
		self.assertEqual(facts["owner_rule"], "OwnerRule::None")

	def test_says_so_when_nothing_is_configured(self):
		facts, reason = OotleReader().promise()
		self.assertIsNone(facts)
		self.assertEqual(reason, "no loyalty component is configured")

	def test_does_not_touch_the_network_when_nothing_is_configured(self):
		transport = FakeTransport()
		with reading(transport):
			OotleReader().promise()
		self.assertEqual(transport.requested, [])

	def test_refuses_a_shape_it_does_not_recognise(self):
		# A wrong rate on a screen is worse than no rate, because it will be
		# believed. Every one of these must refuse, not guess.
		unrecognised = {
			"not a component": {"substate": {"Resource": {}}},
			"state is not a list": component_body(slots={"nope": True}),
			"state is too short": component_body(slots=[None] * (chain.EXPECTED_MIN_SLOTS - 1)),
			"rate is not an int": component_body(rate="100"),
			"ceiling is not an amount": component_body(per_issue="lots"),
			"points resource missing": component_body(points_hex=None),
			"answered a list": [],
		}
		for label, body in unrecognised.items():
			with self.subTest(label):
				with reading(FakeTransport({COMPONENT: body})):
					facts, reason = self.reader().promise()
				self.assertIsNone(facts, label)
				self.assertEqual(reason, "the component answered in a shape this build does not recognise")

	def test_refuses_when_the_configured_resource_is_not_the_one_named(self):
		# Superseded components still resolve and still answer. That is
		# exactly how a stale address gets believed, so a mismatch refuses.
		with reading(FakeTransport({COMPONENT: component_body()})):
			facts, reason = self.reader(loyalty_points_resource="resource_" + "99" * 16).promise()
		self.assertIsNone(facts)
		self.assertIn("refusing rather than reading the wrong ledger", reason)

	def test_accepts_a_matching_configured_resource(self):
		with reading(FakeTransport({COMPONENT: component_body()})):
			facts, reason = self.reader(loyalty_points_resource="resource_" + POINTS_HEX).promise()
		self.assertIsNone(reason)
		self.assertEqual(facts["points_resource"], "resource_" + POINTS_HEX)

	def test_survives_every_transport_failure(self):
		failures = {
			"unreachable": (urllib.error.URLError("refused"), "did not answer"),
			"socket error": (OSError("reset by peer"), "did not answer"),
			"http error": (
				http_error(503, "busy"),
				"answered 503",
			),
		}
		for label, (exception, expected) in failures.items():
			with self.subTest(label):
				with reading(FakeTransport(error=exception)):
					facts, reason = self.reader().promise()
				self.assertIsNone(facts)
				self.assertIn(expected, reason)

	def test_survives_a_body_that_is_not_json(self):
		with reading(FakeTransport({COMPONENT: b"<html>maintenance</html>"})):
			facts, reason = self.reader().promise()
		self.assertIsNone(facts)
		self.assertIn("not JSON", reason)


class PointsBalance(unittest.TestCase):
	def test_reads_a_balance_through_the_vault(self):
		transport = FakeTransport(
			{
				ACCOUNT: account_body([(resource_entry(POINTS_HEX), resource_entry(VAULT_HEX))]),
				"vault_" + VAULT_HEX: vault_body(4200),
			}
		)
		with reading(transport):
			points, reason = OotleReader().points_balance(ACCOUNT, "resource_" + POINTS_HEX)
		self.assertIsNone(reason)
		self.assertEqual(points, 4200)

	def test_never_awarded_is_zero_and_not_an_error(self):
		# The distinction this method exists to keep: a customer with no vault
		# has zero points. An unreadable balance is a different answer and is
		# returned differently.
		transport = FakeTransport(
			{ACCOUNT: account_body([(resource_entry("77" * 16), resource_entry("88" * 16))])}
		)
		with reading(transport):
			points, reason = OotleReader().points_balance(ACCOUNT, "resource_" + POINTS_HEX)
		self.assertEqual(points, 0)
		self.assertIsNone(reason)

	def test_unreadable_is_none_and_not_zero(self):
		with reading(FakeTransport(error=OSError("down"))):
			points, reason = OotleReader().points_balance(ACCOUNT, "resource_" + POINTS_HEX)
		self.assertIsNone(points)
		self.assertIn("did not answer", reason)

	def test_requires_an_account(self):
		self.assertEqual(OotleReader().points_balance("", "resource_x"), (None, "no account given"))

	def test_requires_a_text_resource(self):
		for resource in (None, 7, [], ""):
			with self.subTest(resource=resource):
				points, reason = OotleReader().points_balance(ACCOUNT, resource)
				self.assertIsNone(points)
				self.assertIn("resource", reason)

	def test_refuses_an_account_shape_it_does_not_recognise(self):
		with reading(FakeTransport({ACCOUNT: {"substate": {"Vault": {}}}})):
			points, reason = OotleReader().points_balance(ACCOUNT, "resource_" + POINTS_HEX)
		self.assertIsNone(points)
		self.assertIn("shape this build does not recognise", reason)

	def test_refuses_a_vault_shape_it_does_not_recognise(self):
		transport = FakeTransport(
			{
				ACCOUNT: account_body([(resource_entry(POINTS_HEX), resource_entry(VAULT_HEX))]),
				"vault_" + VAULT_HEX: {"substate": {"Vault": {"resource_container": {}}}},
			}
		)
		with reading(transport):
			points, reason = OotleReader().points_balance(ACCOUNT, "resource_" + POINTS_HEX)
		self.assertIsNone(points)
		self.assertIn("shape this build does not recognise", reason)

	def test_a_malformed_amount_is_not_reported_as_zero(self):
		transport = FakeTransport(
			{
				ACCOUNT: account_body([(resource_entry(POINTS_HEX), resource_entry(VAULT_HEX))]),
				"vault_" + VAULT_HEX: vault_body("not-a-number"),
			}
		)
		with reading(transport):
			points, reason = OotleReader().points_balance(ACCOUNT, "resource_" + POINTS_HEX)
		self.assertIsNone(points)
		self.assertIn("shape this build does not recognise", reason)

	def test_accepts_a_bare_hex_resource(self):
		# Callers pass either form; the walker strips the prefix itself.
		transport = FakeTransport(
			{
				ACCOUNT: account_body([(resource_entry(POINTS_HEX), resource_entry(VAULT_HEX))]),
				"vault_" + VAULT_HEX: vault_body(7),
			}
		)
		with reading(transport):
			self.assertEqual(OotleReader().points_balance(ACCOUNT, POINTS_HEX), (7, None))


class Totality(unittest.TestCase):
	"""The contract, asserted directly rather than inferred from the cases above."""

	def test_the_response_ceiling_is_four_mebibytes(self):
		self.assertEqual(chain.MAX_RESPONSE_BYTES, 4_194_304)

	def test_no_read_raises_no_matter_what_answers(self):
		bodies = [
			b"",
			b"null",
			b"[]",
			b"{}",
			b'{"substate": null}',
			b'{"substate": {"Component": {"body": {"state": null}}}}',
			b"<html>502 Bad Gateway</html>",
			b'{"substate": {"Component": {"body": {"state": [1, 2, 3]}}}}',
		]
		failures = [
			urllib.error.URLError("refused"),
			http_error(500, "boom"),
			OSError("reset"),
			TimeoutError("slow"),
		]
		reader = OotleReader(loyalty_component=COMPONENT, loyalty_points_resource="")
		for body in bodies:
			with self.subTest(body=body[:24]):
				with reading(FakeTransport({"": body})):
					self.assertEqual(len(reader.available()), 2)
					self.assertEqual(len(reader.promise()), 2)
					self.assertEqual(len(reader.points_balance(ACCOUNT, "resource_x")), 2)
		for failure in failures:
			with self.subTest(failure=type(failure).__name__):
				with reading(FakeTransport(error=failure)):
					self.assertEqual(len(reader.available()), 2)
					self.assertEqual(len(reader.promise()), 2)
					self.assertEqual(len(reader.points_balance(ACCOUNT, "resource_x")), 2)

	def test_an_oversized_indexer_response_is_refused_before_json_decoding(self):
		body = b" " * (chain.MAX_RESPONSE_BYTES + 1)
		reader = OotleReader()
		with reading(FakeTransport({"network": body})):
			available, reason = reader.available()
		self.assertFalse(available)
		self.assertIn("exceeded", reason)

	def test_a_response_exactly_at_the_ceiling_is_allowed(self):
		body = b"{}" + b" " * (chain.MAX_RESPONSE_BYTES - 2)
		reader = OotleReader()
		with reading(FakeTransport({"network": body})):
			available, network = reader.available()
		self.assertTrue(available)
		self.assertEqual(network, "")


class Wording(unittest.TestCase):
	"""Charter §2 rule 3: a ceiling ships beside the promise it bounds."""

	def facts(self):
		return {
			"redemption_rate": 100,
			"per_issue_ceiling": 1_000_000,
			"per_epoch_ceiling": 10_000_000,
		}

	def test_needs_no_chain_and_no_configuration(self):
		# A surface must be able to state the limits with the indexer down.
		with reading(FakeTransport(error=OSError("down"))):
			sections = chain.ceilings_wording(self.facts())
		self.assertTrue(sections)

	def test_states_both_ceilings_as_numbers(self):
		body = " ".join(body for _heading, body in chain.ceilings_wording(self.facts()))
		self.assertIn("1,000,000", body)
		self.assertIn("10,000,000", body)
		self.assertIn("100 points buy one cent", body)

	def test_does_not_promise_that_points_keep_their_value(self):
		# The rate is locked; prices are not. Conflating the two is the
		# overclaim this wording exists to prevent.
		body = " ".join(body for _heading, body in chain.ceilings_wording(self.facts()))
		self.assertIn("the merchant still sets prices", body)

	def test_earning_only_notice_says_spending_does_not_work(self):
		notice = chain.earning_only_notice()
		self.assertIn("EARNING ONLY", notice)
		self.assertIn("DOES NOT", notice)

	def test_check_it_yourself_points_at_the_configured_indexer(self):
		reader = OotleReader(indexer="https://x.example")
		lines = reader.check_it_yourself(
			{"component": COMPONENT, "points_resource": "resource_" + POINTS_HEX}, ACCOUNT
		)
		urls = [url for _label, url in lines]
		self.assertEqual(len(urls), 3)
		self.assertTrue(all(url.startswith("https://x.example/substates/") for url in urls))
		self.assertIn(ACCOUNT, urls[2])

	def test_check_it_yourself_omits_the_account_when_there_is_none(self):
		lines = OotleReader().check_it_yourself({"component": COMPONENT, "points_resource": "resource_x"})
		self.assertEqual(len(lines), 2)


if __name__ == "__main__":
	unittest.main()


class TransportSecurity(unittest.TestCase):
	"""A policy read is the evidence behind what a customer is told. Over a
	connection anyone on the path can rewrite, they choose what it says."""

	def test_a_plain_http_indexer_is_refused(self):
		reader = OotleReader(indexer="http://indexer.example", loyalty_component=COMPONENT)
		facts, reason = reader.promise()
		self.assertIsNone(facts)
		self.assertIn("https", reason)

	def test_the_refusal_arrives_as_a_reason_not_an_exception(self):
		# The module's contract holds even for a misconfiguration: nothing
		# here raises, so a bad setting degrades exactly like a dead indexer
		# rather than taking a render down.
		reader = OotleReader(indexer="ftp://nope.example", loyalty_component=COMPONENT)
		self.assertEqual(reader.available()[0], False)
		self.assertIsNone(reader.points_balance(ACCOUNT, "resource_" + POINTS_HEX)[0])

	def test_it_never_reaches_the_network_to_find_out(self):
		# The check is a precondition, not a failed request: a refused scheme
		# must not produce a connection attempt at all.
		transport = FakeTransport(routes={"network": {"network": "igor"}})
		reader = OotleReader(indexer="http://indexer.example", loyalty_component=COMPONENT)
		with reading(transport):
			reader.available()
		self.assertEqual(transport.requested, [])

	def test_insecure_is_allowed_only_when_asked_for(self):
		# The one good reason is a local indexer on a development machine.
		transport = FakeTransport(routes={"network": {"network": "local"}})
		reader = OotleReader(
			indexer="http://localhost:18000", loyalty_component=COMPONENT, allow_insecure=True
		)
		with reading(transport):
			available, network = reader.available()
		self.assertTrue(available)
		self.assertEqual(network, "local")

	def test_https_is_the_default_without_being_asked_for(self):
		self.assertFalse(OotleReader().allow_insecure)
		self.assertTrue(chain.DEFAULT_INDEXER.startswith("https://"))

	def test_a_redirect_from_https_to_http_is_refused(self):
		# Without this the scheme check is decoration: ask over https, get a
		# 302 to http, and urllib follows it without comment. The downgrade
		# is the attack.
		handler = chain._NoDowngradeRedirects()

		class Request:
			full_url = "https://indexer.example/substates/x"

		with self.assertRaises(urllib.error.URLError) as caught:
			handler.redirect_request(Request(), None, 302, "Found", {}, "http://evil.example/x")
		self.assertIn("https to http", str(caught.exception.reason))

	def test_redirect_schemes_are_normalized_and_only_https_is_allowed(self):
		handler = chain._NoDowngradeRedirects()
		request = urllib.request.Request("https://indexer.example/substates/x")
		for target in ("HTTP://evil.example/x", "ftp://evil.example/x"):
			with self.subTest(target=target), self.assertRaises(urllib.error.URLError):
				handler.redirect_request(request, None, 302, "Found", {}, target)

	def test_a_redirect_that_stays_on_https_is_left_to_urllib(self):
		# A real Request, because delegating to urllib means urllib's own
		# rules still apply -- the only thing added here is the refusal
		# above, and a stub thin enough to hide that would prove nothing.
		handler = chain._NoDowngradeRedirects()
		request = urllib.request.Request("https://indexer.example/substates/x")
		result = handler.redirect_request(
			request, io.BytesIO(b""), 302, "Found", {}, "https://other.example/x"
		)
		self.assertIsNotNone(result)
		self.assertEqual(result.full_url, "https://other.example/x")

	def test_a_relative_redirect_inherits_https_and_is_allowed(self):
		handler = chain._NoDowngradeRedirects()
		request = urllib.request.Request("https://indexer.example/substates/x")
		result = handler.redirect_request(request, io.BytesIO(b""), 302, "Found", {}, "/network")
		self.assertEqual(result.full_url, "https://indexer.example/network")


class Provenance(unittest.TestCase):
	"""A ceiling read from a test network and one read from a real network
	are different claims, and the facts have to carry which."""

	def test_the_facts_name_the_indexer_that_answered(self):
		transport = FakeTransport(routes={f"substates/{COMPONENT}": component_body()})
		reader = OotleReader(indexer="https://indexer-b.example", loyalty_component=COMPONENT)
		with reading(transport):
			facts, reason = reader.promise()
		self.assertIsNone(reason)
		self.assertEqual(facts["indexer"], "https://indexer-b.example")

	def test_the_default_indexer_is_the_testnet_one(self):
		# Documented rather than discovered. There is no published mainnet
		# policy tier, so the default cannot be a mainnet endpoint -- and a
		# deployment that never sets one is reading testnet.
		self.assertEqual(chain.DEFAULT_INDEXER, chain.TESTNET_INDEXER)
		self.assertEqual(OotleReader().indexer, chain.TESTNET_INDEXER)


class TheVaultRead(unittest.TestCase):
	"""The second hop, which can fail on its own.

	`points_balance` makes two requests: the account, then the vault the
	account pointed at. The suite covered the first one failing and the
	second one answering nonsense, but not the second one being unreachable
	-- and that is the likeliest of the three, because it is a live indexer
	going away between two calls milliseconds apart.
	"""

	def test_a_vault_that_cannot_be_fetched_is_unreadable_not_zero(self):
		transport = FakeTransport(
			{
				ACCOUNT: account_body([(resource_entry(POINTS_HEX), resource_entry(VAULT_HEX))]),
				# The vault is simply not there when asked for.
			}
		)
		with reading(transport):
			points, reason = OotleReader().points_balance(ACCOUNT, "resource_" + POINTS_HEX)
		self.assertIsNone(points, "an unreachable vault must never read as a zero balance")
		self.assertIsNotNone(reason)

	def test_it_asked_for_the_vault_the_account_named(self):
		transport = FakeTransport(
			{
				ACCOUNT: account_body([(resource_entry(POINTS_HEX), resource_entry(VAULT_HEX))]),
				"vault_" + VAULT_HEX: vault_body(7),
			}
		)
		with reading(transport):
			OotleReader().points_balance(ACCOUNT, "resource_" + POINTS_HEX)
		self.assertEqual(len(transport.requested), 2)
		self.assertTrue(transport.requested[1].endswith("vault_" + VAULT_HEX))

	def test_a_negative_vault_amount_is_refused(self):
		transport = FakeTransport(
			{
				ACCOUNT: account_body([(resource_entry(POINTS_HEX), resource_entry(VAULT_HEX))]),
				"vault_" + VAULT_HEX: vault_body(-1),
			}
		)
		with reading(transport):
			points, reason = OotleReader().points_balance(ACCOUNT, "resource_" + POINTS_HEX)
		self.assertIsNone(points)
		self.assertIn("shape", reason)


class TheOpener(unittest.TestCase):
	"""`_urlopen` itself — the one door, built once and reused.

	Every other test in this file patches this function out, which is exactly
	right for them and leaves the function itself unexecuted. What is checked
	here is the wiring: that the opener carries the no-downgrade handler, and
	that it is built once rather than per request.
	"""

	def setUp(self):
		self.saved = chain._OPENER
		chain._OPENER = None

	def tearDown(self):
		chain._OPENER = self.saved

	def test_it_installs_the_no_downgrade_handler(self):
		# The scheme check is decoration without this: urllib's own global
		# opener follows an https -> http redirect without comment.
		built = []

		class FakeOpener:
			def open(self, request, timeout=None):
				return "answered"

		def fake_build_opener(*handlers):
			built.append(handlers)
			return FakeOpener()

		with mock.patch("urllib.request.build_opener", fake_build_opener):
			self.assertEqual(chain._urlopen("https://indexer.example/x"), "answered")

		self.assertEqual(len(built), 1)
		self.assertTrue(
			any(isinstance(handler, chain._NoDowngradeRedirects) for handler in built[0]),
			"the opener must carry the redirect handler or the https promise is empty",
		)

	def test_it_builds_the_opener_once_and_reuses_it(self):
		built = []

		class FakeOpener:
			def __init__(self):
				self.calls = []

			def open(self, request, timeout=None):
				self.calls.append((request, timeout))
				return "answered"

		def fake_build_opener(*handlers):
			opener = FakeOpener()
			built.append(opener)
			return opener

		with mock.patch("urllib.request.build_opener", fake_build_opener):
			chain._urlopen("https://indexer.example/a", timeout=5)
			chain._urlopen("https://indexer.example/b", timeout=5)

		self.assertEqual(len(built), 1, "a per-request opener would drop connection reuse")
		self.assertEqual(len(built[0].calls), 2)

	def test_it_passes_the_timeout_through(self):
		# A policy read with no timeout is a policy read that can hang a
		# request handler for as long as the indexer feels like.
		class FakeOpener:
			def __init__(self):
				self.seen = None

			def open(self, request, timeout=None):
				self.seen = timeout
				return "answered"

		opener = FakeOpener()
		with mock.patch("urllib.request.build_opener", lambda *h: opener):
			chain._urlopen("https://indexer.example/x", timeout=9)
		self.assertEqual(opener.seen, 9)


class AmountDecoding(unittest.TestCase):
	"""`_amount` — every shape the indexer has been seen to use, and the rest.

	This is the function that turns a CBOR-ish slot into a number, and it is
	documented as returning None rather than raising, because a raise here
	escapes `promise()`, which is documented never to raise.
	"""

	def test_the_two_element_array_form(self):
		self.assertEqual(chain._amount([1234, "some-tag"]), 1234)

	def test_a_bare_integer_form(self):
		# Not hypothetical: the same slot is serialised bare when the value
		# is small enough, and reading it as None would report a real ceiling
		# as unreadable.
		self.assertEqual(chain._amount(7), 7)

	def test_a_numeric_string_inside_the_array(self):
		self.assertEqual(chain._amount(["42"]), 42)

	def test_an_empty_array_is_none(self):
		self.assertIsNone(chain._amount([]))

	def test_a_non_numeric_first_element_is_none_rather_than_a_raise(self):
		self.assertIsNone(chain._amount(["not a number"]))
		self.assertIsNone(chain._amount([{"nested": "thing"}]))

	def test_a_bare_decimal_string(self):
		"""The shape esmeralda's 0.39.3 indexer actually answers with.

		This assertion is the reverse of what this class asserted until
		2026-08-30, and the reversal is the finding. A bare string used to be
		refused deliberately, on the reasoning that an unrecognised shape must
		not be truncated into a number. Measured against the live network, a
		funded account's vault returns

			{"Stealth": {"revealed_amount": "999997692", "locked_amount": "0"}}

		so refusing it made the library reject its own live answer as "a shape
		this build does not recognise", and the Ootle rail could not read a
		balance at all. `_exact_integer` parses a decimal string exactly and
		refuses anything else, so nothing is truncated by accepting this --
		the float, dict, bool and None cases below still refuse.
		"""
		self.assertEqual(chain._amount("999997692"), 999997692)
		self.assertEqual(chain._amount("100"), 100)
		self.assertEqual(chain._amount(" 42 "), 42)

	def test_a_string_that_is_not_an_exact_integer_is_still_refused(self):
		for entry in ("1.5", "0x10", "", "12abc", "1e6"):
			with self.subTest(entry=entry):
				self.assertIsNone(chain._amount(entry))

	def test_shapes_it_does_not_recognise_are_none(self):
		for entry in (None, {"value": 100}, 1.5, True):
			with self.subTest(entry=entry):
				got = chain._amount(entry)
				self.assertIsNone(got, f"{entry!r} must be refused rather than truncated, got {got!r}")

	def test_array_values_are_never_truncated_or_treated_as_booleans(self):
		for entry in ([1.9], [True], [False], ["1.9"]):
			with self.subTest(entry=entry):
				self.assertIsNone(chain._amount(entry))

	def test_it_never_raises_on_anything(self):
		for entry in (None, [], {}, "", 0, [None], [[]], [{}], object()):
			with self.subTest(entry=entry):
				chain._amount(entry)


class VaultWalk(unittest.TestCase):
	"""`_walk_for_resource` — finding the vault beside the resource.

	An account's state is a nested tree and the vault is identified by being
	the SIBLING of the resource id, not by any key naming it. That is fragile
	enough to deserve its edges pinned.
	"""

	def test_it_finds_the_sibling_of_a_matching_resource(self):
		node = [[resource_entry(POINTS_HEX), resource_entry(VAULT_HEX)]]
		self.assertEqual(chain._walk_for_resource(node, POINTS_HEX), "vault_" + VAULT_HEX)

	def test_it_finds_a_pair_nested_inside_dictionaries(self):
		node = {"state": {"vaults": [[resource_entry(POINTS_HEX), resource_entry(VAULT_HEX)]]}}
		self.assertEqual(chain._walk_for_resource(node, POINTS_HEX), "vault_" + VAULT_HEX)

	def test_the_resource_entry_itself_is_not_its_vault(self):
		# A bare match is the resource id, not the vault holding it. Returning
		# it would send the next request to the resource and read a balance
		# off the wrong substate.
		self.assertIsNone(chain._walk_for_resource(resource_entry(POINTS_HEX), POINTS_HEX))

	def test_a_resource_that_is_absent_is_none(self):
		node = {"state": {"vaults": [[resource_entry("aa" * 16), resource_entry("bb" * 16)]]}}
		self.assertIsNone(chain._walk_for_resource(node, POINTS_HEX))

	def test_it_never_guesses_a_sibling_from_a_longer_positional_list(self):
		node = [
			[
				resource_entry(POINTS_HEX),
				resource_entry(VAULT_HEX),
				resource_entry("ab" * 16),
			]
		]
		self.assertIsNone(chain._walk_for_resource(node, POINTS_HEX))

	def test_it_stops_rather_than_recursing_forever(self):
		# Depth is bounded so a deep or cyclic-looking body cannot turn a
		# policy read into a stack overflow inside a request handler.
		deep = resource_entry(POINTS_HEX)
		for _ in range(20):
			deep = {"wrapper": deep}
		self.assertIsNone(chain._walk_for_resource(deep, POINTS_HEX))

	def test_a_match_below_the_depth_limit_is_still_found(self):
		node = [[resource_entry(POINTS_HEX), resource_entry(VAULT_HEX)]]
		for _ in range(3):
			node = {"wrapper": node}
		self.assertEqual(chain._walk_for_resource(node, POINTS_HEX), "vault_" + VAULT_HEX)


class SlotLayout(unittest.TestCase):
	"""The positional map of the component's CBOR state, as literals.

	This is the one place the slot numbers are stated independently of the
	code that uses them. Everything else in this file builds its fixture from
	literal indices, so if a constant here moved by one the reads would land
	on the wrong slot and those tests would fail -- but only because this
	layout is pinned rather than inferred.

	The layout was confirmed on chain against component `component_73f1d0bf…`
	(K1, version 36), and all four resource slots match
	`ootle-testnet/ADDRESSES.md`. That correspondence is what fixes the
	mapping; without it the numbers are a guess that happens to parse.
	"""

	def test_the_four_resource_slots(self):
		self.assertEqual(chain.SLOT_POINTS_RESOURCE, 5)
		self.assertEqual(chain.SLOT_ENTITLEMENT_RESOURCE, 6)
		self.assertEqual(chain.SLOT_ENROLMENT_RESOURCE, 7)
		self.assertEqual(chain.SLOT_VAULT_CLAIM_RESOURCE, 8)

	def test_the_policy_slots(self):
		self.assertEqual(chain.SLOT_REDEMPTION_RATE, 9)
		self.assertEqual(chain.SLOT_PER_ISSUE_CEILING, 10)
		self.assertEqual(chain.SLOT_PER_EPOCH_CEILING, 11)

	def test_the_epoch_window_slots(self):
		self.assertEqual(chain.SLOT_WINDOW_EPOCH, 12)
		self.assertEqual(chain.SLOT_COMMITTED_THIS_EPOCH, 13)

	def test_every_slot_is_distinct(self):
		# Two constants pointing at one slot would read the same value twice
		# and report it under two names.
		slots = [
			chain.SLOT_POINTS_RESOURCE,
			chain.SLOT_ENTITLEMENT_RESOURCE,
			chain.SLOT_ENROLMENT_RESOURCE,
			chain.SLOT_VAULT_CLAIM_RESOURCE,
			chain.SLOT_REDEMPTION_RATE,
			chain.SLOT_PER_ISSUE_CEILING,
			chain.SLOT_PER_EPOCH_CEILING,
			chain.SLOT_WINDOW_EPOCH,
			chain.SLOT_COMMITTED_THIS_EPOCH,
		]
		self.assertEqual(len(set(slots)), len(slots))

	def test_the_minimum_length_covers_every_slot_read(self):
		self.assertEqual(chain.EXPECTED_MIN_SLOTS, 15)
		self.assertGreater(chain.EXPECTED_MIN_SLOTS, chain.SLOT_COMMITTED_THIS_EPOCH)


class ShapesThatAreNotTheContract(unittest.TestCase):
	"""Bodies that parse as JSON and are not what they claim to be.

	A proxy, a captive portal or an error page answers 200 with valid JSON of
	entirely the wrong shape. Each one below reaches a different guard, and
	every one of them must come back as a reason rather than an exception.
	"""

	def test_a_network_body_that_is_not_an_object(self):
		# `available()` reaches for .get(); a list or a string does not have one.
		# `'"ok"'` rather than `"ok"`: the transport passes strings through as
		# the raw body, and a bare `ok` is not JSON at all -- a different
		# refusal, reached one step earlier, which would leave this guard
		# untested while the test passed.
		for body in ([1, 2, 3], '"ok"', 7):
			with self.subTest(body=body):
				with reading(FakeTransport({"network": body})):
					reachable, reason = OotleReader().available()
				self.assertFalse(reachable, f"{body!r} is not an indexer answering")
				self.assertIn("shape this build does not recognise", reason)

	def test_a_state_that_is_not_a_list_is_refused(self):
		# A dict with enough keys passes a length check but not an index one:
		# `state[9]` on a dict raises KeyError, and promise() may not raise.
		state = {str(index): index for index in range(20)}
		with reading(FakeTransport({COMPONENT: component_body(slots=state)})):
			facts, reason = OotleReader(loyalty_component=COMPONENT).promise()
		self.assertIsNone(facts)
		self.assertIn("shape this build does not recognise", reason)

	def test_a_state_shorter_than_the_layout_is_refused(self):
		# Indexing slot 13 of a three-element list raises IndexError.
		with reading(FakeTransport({COMPONENT: component_body(slots=[None, None, None])})):
			facts, reason = OotleReader(loyalty_component=COMPONENT).promise()
		self.assertIsNone(facts)
		self.assertIn("shape this build does not recognise", reason)

	def test_a_state_exactly_the_minimum_length_is_accepted(self):
		# The boundary from the other side: EXPECTED_MIN_SLOTS is a minimum,
		# not a forbidden value, and a `<=` typo would refuse the real thing.
		with reading(FakeTransport({COMPONENT: component_body()})):
			facts, reason = OotleReader(loyalty_component=COMPONENT).promise()
		self.assertIsNone(reason)
		self.assertEqual(len(component_body()["substate"]["Component"]["body"]["state"]), 15)
		self.assertIsNotNone(facts)

	def test_a_non_object_header_is_refused_without_raising(self):
		body = component_body()
		body["substate"]["Component"]["header"] = None
		with reading(FakeTransport({COMPONENT: body})):
			facts, reason = OotleReader(loyalty_component=COMPONENT).promise()
		self.assertIsNone(facts)
		self.assertIn("shape this build does not recognise", reason)

	def test_every_fact_slot_must_have_its_documented_shape(self):
		for slot, value in (
			(chain.SLOT_ENTITLEMENT_RESOURCE, None),
			(chain.SLOT_WINDOW_EPOCH, "not-an-epoch"),
			(chain.SLOT_COMMITTED_THIS_EPOCH, ["not-an-amount"]),
		):
			with self.subTest(slot=slot):
				state = component_body()["substate"]["Component"]["body"]["state"]
				state[slot] = value
				with reading(FakeTransport({COMPONENT: component_body(slots=state)})):
					facts, reason = OotleReader(loyalty_component=COMPONENT).promise()
					self.assertIsNone(facts)
					self.assertIn("shape this build does not recognise", reason)

	def test_policy_numbers_must_have_sensible_domains(self):
		for slot, value in (
			(chain.SLOT_REDEMPTION_RATE, True),
			(chain.SLOT_REDEMPTION_RATE, 0),
			(chain.SLOT_PER_ISSUE_CEILING, [-1]),
			(chain.SLOT_PER_EPOCH_CEILING, [-1]),
			(chain.SLOT_COMMITTED_THIS_EPOCH, [-1]),
			(chain.SLOT_WINDOW_EPOCH, True),
			(chain.SLOT_WINDOW_EPOCH, -1),
		):
			with self.subTest(slot=slot, value=value):
				state = component_body()["substate"]["Component"]["body"]["state"]
				state[slot] = value
				with reading(FakeTransport({COMPONENT: component_body(slots=state)})):
					facts, reason = OotleReader(loyalty_component=COMPONENT).promise()
				self.assertIsNone(facts)
				self.assertIn("shape this build does not recognise", reason)


class WalkDepth(unittest.TestCase):
	"""The recursion limit, pinned from both sides.

	An account body is attacker-adjacent data: it comes off a public indexer
	and is walked recursively. The bound is what keeps a deep or cyclic-looking
	body from turning a policy read into a stack overflow inside a request
	handler -- and a bound that is never tested at its edge is a bound that
	can drift to "no bound at all" without a failure.
	"""

	def nested(self, wrappers):
		node = [[resource_entry(POINTS_HEX), resource_entry(VAULT_HEX)]]
		for _ in range(wrappers):
			node = {"wrapper": node}
		return node

	def test_the_deepest_reachable_pair_is_found(self):
		self.assertEqual(chain._walk_for_resource(self.nested(5), POINTS_HEX), "vault_" + VAULT_HEX)

	def test_one_level_deeper_is_not(self):
		self.assertIsNone(chain._walk_for_resource(self.nested(6), POINTS_HEX))

	def test_the_walk_starts_at_the_top(self):
		# Starting the count at 1 rather than 0 would silently cost a level,
		# and only the boundary pair above can tell.
		self.assertEqual(chain._walk_for_resource(self.nested(0), POINTS_HEX), "vault_" + VAULT_HEX)

	def test_a_resource_id_beside_the_pair_does_not_stop_the_walk(self):
		# The early `return None` is for a node that IS the wanted resource,
		# not for any node that merely carries some resource id. Stopping on
		# the latter would abandon the search inside every wrapper that names
		# a resource of its own.
		node = {
			"value": {"hex": "aa" * 16},
			"vaults": [[resource_entry(POINTS_HEX), resource_entry(VAULT_HEX)]],
		}
		self.assertEqual(chain._walk_for_resource(node, POINTS_HEX), "vault_" + VAULT_HEX)


class ZeroIsABalance(unittest.TestCase):
	"""A vault holding nothing holds zero, and zero is a number."""

	def test_an_empty_vault_reads_as_zero(self):
		transport = FakeTransport(
			{
				ACCOUNT: account_body([(resource_entry(POINTS_HEX), resource_entry(VAULT_HEX))]),
				"vault_" + VAULT_HEX: vault_body(0),
			}
		)
		with reading(transport):
			points, reason = OotleReader().points_balance(ACCOUNT, "resource_" + POINTS_HEX)
		self.assertIsNone(reason)
		# Not 1, and not None. A customer with an empty vault is told they
		# hold nothing, which is true; telling them they hold one point is a
		# promise the chain does not back.
		self.assertEqual(points, 0)


class PromiseNumericBoundaries(unittest.TestCase):
	def promise_for(self, body):
		with reading(FakeTransport({COMPONENT: body})):
			return OotleReader(loyalty_component=COMPONENT).promise()

	def test_rate_one_is_valid_and_zero_is_not(self):
		facts, reason = self.promise_for(component_body(rate=1))
		self.assertIsNone(reason)
		self.assertEqual(facts["redemption_rate"], 1)
		facts, reason = self.promise_for(component_body(rate=0))
		self.assertIsNone(facts)
		self.assertIn("shape", reason)

	def test_zero_ceilings_commitment_and_epoch_are_valid_numbers(self):
		body = component_body(per_issue=0, per_epoch=0, epoch=0)
		state = body["substate"]["Component"]["body"]["state"]
		state[13] = [0, 0]
		facts, reason = self.promise_for(body)
		self.assertIsNone(reason)
		self.assertEqual(facts["per_issue_ceiling"], 0)
		self.assertEqual(facts["per_epoch_ceiling"], 0)
		self.assertEqual(facts["committed_this_epoch"], 0)
		self.assertEqual(facts["window_epoch"], 0)

	def test_negative_ceilings_commitment_and_epoch_are_each_refused(self):
		for slot, value in ((10, [-1, 0]), (11, [-1, 0]), (13, [-1, 0]), (12, -1)):
			body = component_body()
			body["substate"]["Component"]["body"]["state"][slot] = value
			with self.subTest(slot=slot):
				facts, reason = self.promise_for(body)
			self.assertIsNone(facts)
			self.assertIn("shape", reason)
