"""Reading the Ootle policy tier — free, keyless, feeless.

Every read here is TOTAL. Nothing raises. A read that fails returns a
sentinel and a reason, because the rule above this module is absolute:

    a sale must never fail because the policy layer is down.

Reading needs no account and costs nothing, and that is the property the
whole verification story rests on — a merchant's promise is checkable by the
customer who holds the card, from any machine, at no cost. A limit that costs
money to check is a limit most people will not check.

The component's state is a positional CBOR array. The mapping below was
verified against the deployed K1 component by matching all four resource
slots to ootle-testnet/ADDRESSES.md; a shape that does not match is a
REFUSAL, not a guess. A wrong rate on a screen is worse than no rate at all,
because it will be believed.

Configuration arrives through the constructor rather than being fetched from
a host's settings store, so this reads the same chain from a POS terminal, a
web backend, or a script with no framework under it at all.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

# Short on purpose. A screen may be building in front of a waiting customer,
# so a slow indexer must degrade to "unavailable" rather than hang a render.
READ_TIMEOUT_SECONDS = 4.0

# Account substates can be substantial, but they are still bounded protocol
# messages. Refuse an unexpectedly huge body before decoding it rather than
# letting a broken or hostile indexer choose this process's memory use.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# **THIS IS A TESTNET INDEXER, AND THE DEFAULT.** The deployed Loyalty
# component this package knows how to read lives on the Ootle test network;
# no mainnet policy tier is published, so there is no mainnet indexer to
# point at and none is invented here.
#
# The hazard is quiet: a mainnet deployment that never sets `indexer` reads
# testnet policy and shows a customer testnet ceilings as though they bounded
# real money. Nothing about the answer would look wrong. So `promise()`
# returns the indexer that answered as part of its facts, and a surface that
# displays a ceiling without displaying where it came from is a surface that
# has dropped the provenance rather than one this module failed to give it.
TESTNET_INDEXER = "https://ootle-indexer-a.tari.com"

DEFAULT_INDEXER = TESTNET_INDEXER

# Positional layout of Loyalty's component state, confirmed on-chain.
SLOT_POINTS_RESOURCE = 5
SLOT_ENTITLEMENT_RESOURCE = 6
SLOT_ENROLMENT_RESOURCE = 7
SLOT_VAULT_CLAIM_RESOURCE = 8
SLOT_REDEMPTION_RATE = 9
SLOT_PER_ISSUE_CEILING = 10
SLOT_PER_EPOCH_CEILING = 11
SLOT_WINDOW_EPOCH = 12
SLOT_COMMITTED_THIS_EPOCH = 13
EXPECTED_MIN_SLOTS = 15


class OotleReader:
	"""A configured, read-only view of the policy tier.

	Construct one per read from whatever holds your configuration -- it is
	cheap, holds no connection, and taking config at construction keeps the
	"latest settings win" behaviour a long-lived instance would lose.
	"""

	def __init__(
		self,
		indexer=None,
		loyalty_component="",
		loyalty_points_resource="",
		timeout=READ_TIMEOUT_SECONDS,
		user_agent=None,
		allow_insecure=False,
	):
		self._indexer_error = None
		if indexer is None or indexer == "":
			self.indexer = DEFAULT_INDEXER
		elif isinstance(indexer, str):
			self.indexer = indexer.rstrip("/")
		else:
			self.indexer = ""
			self._indexer_error = "the indexer address must be text"
		self._component_error = None
		if loyalty_component is None or isinstance(loyalty_component, str):
			self.loyalty_component = (loyalty_component or "").strip()
		else:
			self.loyalty_component = ""
			self._component_error = "the loyalty component must be text"
		if loyalty_points_resource is None or isinstance(loyalty_points_resource, str):
			self.loyalty_points_resource = (loyalty_points_resource or "").strip()
		else:
			self.loyalty_points_resource = ""
		self.timeout = timeout
		self.user_agent = user_agent if isinstance(user_agent, str) and user_agent else _default_user_agent()
		# Plain HTTP is refused unless a caller says otherwise, and the only
		# good reason to say otherwise is a local indexer on a development
		# machine. A policy read is the evidence behind what a customer is
		# told; over http, anyone on the path chooses what they are told.
		self.allow_insecure = bool(allow_insecure)

	def _get(self, path):
		"""GET {indexer}/{path}. Returns (body, None) or (None, reason)."""
		if self._indexer_error:
			return None, self._indexer_error
		url = f"{self.indexer}/{path}"

		# Checked here rather than in __init__ so construction remains total.
		# `allow_insecure` means a local HTTP indexer, not arbitrary URL schemes;
		# embedded credentials are also refused because `promise()` deliberately
		# returns this URL as provenance and must never expose a secret in it.
		try:
			parsed = urllib.parse.urlsplit(self.indexer)
			host = parsed.hostname
		except (TypeError, ValueError):
			return None, "the indexer address is not a valid URL"
		secure = parsed.scheme.lower() == "https"
		insecure_http = self.allow_insecure and parsed.scheme.lower() == "http"
		if not host or not (secure or insecure_http):
			return None, (
				"the indexer must be an https:// address (or http:// when explicitly "
				"allowed for local development)"
			)
		if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
			return None, "the indexer URL must not contain credentials, a query, or a fragment"

		try:
			request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
			with _urlopen(request, timeout=self.timeout) as response:
				payload = response.read(MAX_RESPONSE_BYTES + 1)
			if len(payload) > MAX_RESPONSE_BYTES:
				return None, f"the indexer response exceeded {MAX_RESPONSE_BYTES} bytes"
			return json.loads(payload.decode("utf-8")), None
		except urllib.error.HTTPError as exception:
			return None, f"the indexer answered {exception.code} for {path}"
		except (urllib.error.URLError, OSError) as exception:
			return None, f"the indexer did not answer: {exception}"
		except (ValueError, TypeError, RecursionError) as exception:
			return None, f"the indexer answered something that is not JSON: {exception}"

	def _get_sse(self, path):
		"""GET a bounded SSE replay. Returns (UTF-8 bytes, None) or (None, reason).

		The transaction event endpoint stays open and sends ``:`` while idle.
		That comment is the replay boundary: stop there instead of asking a
		request handler to wait forever for a response body that never closes.
		"""
		if self._indexer_error:
			return None, self._indexer_error
		url = f"{self.indexer}/{path}"
		try:
			parsed = urllib.parse.urlsplit(self.indexer)
			host = parsed.hostname
		except (TypeError, ValueError):
			return None, "the indexer address is not a valid URL"
		secure = parsed.scheme.lower() == "https"
		insecure_http = self.allow_insecure and parsed.scheme.lower() == "http"
		if not host or not (secure or insecure_http):
			return None, (
				"the indexer must be an https:// address (or http:// when explicitly "
				"allowed for local development)"
			)
		if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
			return None, "the indexer URL must not contain credentials, a query, or a fragment"

		try:
			request = urllib.request.Request(
				url,
				headers={"Accept": "text/event-stream", "User-Agent": self.user_agent},
			)
			payload = bytearray()
			try:
				with _urlopen(request, timeout=self.timeout) as response:
					while True:
						line = response.readline(MAX_RESPONSE_BYTES + 1 - len(payload))
						payload.extend(line)
						if len(payload) > MAX_RESPONSE_BYTES:
							return None, f"the indexer response exceeded {MAX_RESPONSE_BYTES} bytes"
						if not line or line.startswith(b":"):
							break
			except OSError:
				# A TIMEOUT WITH FRAMES ALREADY IN HAND IS THE END OF THE
				# REPLAY, NOT A FAILURE. The endpoint replays history and then
				# holds the connection open, so the `:` comment above is the
				# ordinary boundary -- but it only arrives when the server
				# decides to send it, and the app hands this reader a 4 second
				# budget. Measured 2026-08-31: charging on `xtr` inside the
				# host's containers failed with "the indexer did not answer:
				# The read operation timed out" while the frames for a real
				# settled payment were already in `payload` and were thrown
				# away with them.
				#
				# Discarding them would be worse than slow: the events are
				# cursor-addressed, so a partial read is not a partial answer.
				# It is a SHORTER answer, and the next poll resumes from the
				# last id it did see. Nothing is skipped and nothing is
				# double-counted. Only an empty payload is a real silence.
				#
				# `OSError` rather than `TimeoutError`: on 3.9 a read timeout
				# arrives as `socket.timeout`, which is an OSError and NOT a
				# TimeoutError. Catching the base covers both without importing
				# `socket`, and any other read interruption after valid frames
				# deserves the same treatment for the same reason.
				if not payload:
					raise
			return bytes(payload), None
		except urllib.error.HTTPError as exception:
			return None, f"the indexer answered {exception.code} for {path}"
		except (urllib.error.URLError, OSError) as exception:
			return None, f"the indexer did not answer: {exception}"
		except (TypeError, ValueError) as exception:
			return None, f"the indexer answered an invalid event stream: {exception}"

	def available(self):
		"""Is the policy layer reachable at all? Never raises."""
		body, reason = self._get("network")
		if body is None:
			return False, reason
		# Answering with valid JSON is not the same as answering as an
		# indexer. A proxy or an error page can return a list or a bare
		# string, and reaching for .get() on one of those is how a method
		# documented never to raise raises.
		if not isinstance(body, dict):
			return False, "the indexer answered in a shape this build does not recognise"
		return True, body.get("network", "")

	def promise(self):
		"""Read the deployed contract's own account of itself.

		Returns (facts, None) or (None, reason). `facts` carries the rate and
		both ceilings — the numbers that must appear on any surface offering
		the feature.
		"""
		if self._component_error:
			return None, self._component_error
		component = self.loyalty_component
		if not component:
			return None, "no loyalty component is configured"

		body, reason = self._get(f"substates/{component}")
		if body is None:
			return None, reason

		try:
			state = body["substate"]["Component"]["body"]["state"]
			header = body["substate"]["Component"]["header"]
		except (KeyError, TypeError):
			return None, "the component answered in a shape this build does not recognise"

		if not isinstance(body, dict) or not isinstance(header, dict):
			return None, "the component answered in a shape this build does not recognise"
		if not isinstance(state, list) or len(state) < EXPECTED_MIN_SLOTS:
			return None, "the component answered in a shape this build does not recognise"

		rate = state[SLOT_REDEMPTION_RATE]
		per_issue = _amount(state[SLOT_PER_ISSUE_CEILING])
		per_epoch = _amount(state[SLOT_PER_EPOCH_CEILING])
		points_resource = _hex_of(state[SLOT_POINTS_RESOURCE])
		entitlement_resource = _hex_of(state[SLOT_ENTITLEMENT_RESOURCE])
		committed_this_epoch = _amount(state[SLOT_COMMITTED_THIS_EPOCH])
		window_epoch = state[SLOT_WINDOW_EPOCH]

		invalid_shape = (
			not isinstance(rate, int)
			or isinstance(rate, bool)
			or rate <= 0
			or per_issue is None
			or per_issue < 0
			or per_epoch is None
			or per_epoch < 0
			or committed_this_epoch is None
			or committed_this_epoch < 0
			or not isinstance(window_epoch, int)
			or isinstance(window_epoch, bool)
			or window_epoch < 0
			or not isinstance(points_resource, str)
			or not points_resource
			or not isinstance(entitlement_resource, str)
			or not entitlement_resource
		)
		if invalid_shape:
			return None, "the component answered in a shape this build does not recognise"

		# The configured resource must be the one the component actually names.
		# A mismatch means the settings point at a superseded component -- the
		# old addresses still resolve and still answer, which is exactly how a
		# stale address gets believed.
		configured = self.loyalty_points_resource
		named = f"resource_{points_resource}"
		if configured and configured != named:
			return None, (
				f"the configured points resource is not the one this component names "
				f"({configured} vs {named}); refusing rather than reading the wrong ledger"
			)

		return (
			{
				"component": component,
				# Which indexer said so. A ceiling read from a test network
				# and one read from a real one are different claims, and the
				# facts carry that rather than leaving a surface to assume.
				"indexer": self.indexer,
				"version": body.get("version"),
				"owner_rule": header.get("owner_rule"),
				"redemption_rate": rate,
				"per_issue_ceiling": per_issue,
				"per_epoch_ceiling": per_epoch,
				"window_epoch": window_epoch,
				"committed_this_epoch": committed_this_epoch,
				"points_resource": named,
				"entitlement_resource": f"resource_{entitlement_resource}",
			},
			None,
		)

	def points_balance(self, account, points_resource):
		"""Read a customer's points balance. Returns (points, None) or (None, reason).

		A balance of 0 and an unreadable balance are different answers and are
		returned differently. NEVER call this on the path of a sale.
		"""
		return self.resource_balance(account, points_resource)

	def resource_balance(self, account, resource):
		"""Read one public fungible or stealth resource balance from an account.

		This is the transport primitive shared by loyalty points and the Ootle
		payment rail. A confidential container is deliberately unreadable: its
		revealed amount is only a floor, not the account's balance.
		"""
		found, reason = self.resource_vault(account, resource)
		if reason is not None:
			return None, reason
		if found is None:
			return 0, None

		vault_body, reason = self._get(f"substates/{found}")
		if vault_body is None:
			return None, reason
		return _balance_of(vault_body)

	def resource_vault(self, account, resource):
		"""Resolve the vault holding ``resource`` in an account's public state."""
		if not account:
			return None, "no account given"
		if not isinstance(resource, str) or not resource:
			return None, "the resource must be non-empty text"
		body, reason = self._get(f"substates/{account}")
		if body is None:
			return None, reason
		try:
			vaults = body["substate"]["Component"]["body"]["state"]
		except (KeyError, TypeError):
			return None, "the account answered in a shape this build does not recognise"
		return _walk_for_resource(vaults, resource.removeprefix("resource_")), None

	def check_it_yourself(self, facts, account=""):
		"""The literal URLs a customer can open to check the promise themselves."""
		lines = [
			("The contract itself", f"{self.indexer}/substates/{facts['component']}"),
			("The points resource", f"{self.indexer}/substates/{facts['points_resource']}"),
		]
		if account:
			lines.append(("Your account", f"{self.indexer}/substates/{account}"))
		return lines


class _NoDowngradeRedirects(urllib.request.HTTPRedirectHandler):
	"""Follow redirects, but never from https to a non-https scheme.

	Without this the scheme check above is decoration: an indexer is asked
	over https, answers 302 to an http URL, and urllib follows it without
	comment. The downgrade is the attack -- it is how a connection that
	looked verified stops being one, and nothing in the response says so.
	"""

	def redirect_request(self, request, fp, code, msg, headers, newurl):
		source_scheme = urllib.parse.urlsplit(request.full_url).scheme.lower()
		resolved_url = urllib.parse.urljoin(request.full_url, newurl)
		destination_scheme = urllib.parse.urlsplit(resolved_url).scheme.lower()
		if source_scheme == "https" and destination_scheme != "https":
			raise urllib.error.URLError(
				f"refusing a redirect from https to {destination_scheme} "
				f"({newurl}); a downgraded "
				f"policy read is not one this build will present as evidence"
			)
		return super().redirect_request(request, fp, code, msg, headers, resolved_url)


# Built once and reused. The no-downgrade handler is installed
# UNCONDITIONALLY, including for `allow_insecure` readers: the handler only
# fires when the request itself was https, so a deliberately-http local
# indexer never touches it, and an https one is protected no matter what the
# caller asked for. One opener, one behaviour, nothing to configure wrong.
_OPENER = None


def _urlopen(request, timeout=None):
	"""The single seam through which this module reaches the network.

	A module-level function rather than a direct `urllib.request.urlopen`
	call, because `urlopen` uses urllib's own global opener and would not
	carry the redirect handler. It is also the one place the suite patches:
	`test_chain` replaces this, and the README's promise that no test in this
	package touches a network holds because there is exactly one door.
	"""
	global _OPENER
	if _OPENER is None:
		_OPENER = urllib.request.build_opener(_NoDowngradeRedirects())
	return _OPENER.open(request, timeout=timeout)


def _default_user_agent():
	from . import __version__

	return f"cryptopos-rail-ootle/{__version__}"


def _hex_of(entry):
	if isinstance(entry, dict) and isinstance(entry.get("value"), dict):
		return entry["value"].get("hex")
	return None


def _amount(entry):
	# Amounts arrive as a two-element array; the first element is the value.
	#
	# The conversion is guarded rather than trusted. int() on a first element
	# that is not a number raises, and a raise here escapes promise() -- which
	# is documented never to raise -- for the sake of a slot this module
	# already knows how to refuse. Returning None routes it into that refusal.
	if isinstance(entry, list) and entry:
		return _exact_integer(entry[0])
	if isinstance(entry, int) and not isinstance(entry, bool):
		return entry
	# A BARE DECIMAL STRING, which is what esmeralda's 0.39.3 indexer answers
	# with. Measured 2026-08-30: a funded account's vault returned
	# `{"Stealth": {"revealed_amount": "999997692", "locked_amount": "0"}}`,
	# and without this branch `_balance_of` refused its own live answer as "a
	# shape this build does not recognise". `_exact_integer` already decodes a
	# string without truncating it, so this routes rather than reimplements.
	if isinstance(entry, str):
		return _exact_integer(entry)
	return None


def _exact_integer(value):
	"""Decode an integer-shaped indexer value without truncation or booleans."""
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


def _walk_for_resource(node, wanted, depth=0):
	"""Find a vault id keyed against `wanted` in an account's state tree."""
	if depth > 6:
		return None
	if isinstance(node, dict):
		hexed = _hex_of(node)
		if hexed and hexed == wanted:
			return None  # this is the resource itself, not its vault
		for value in node.values():
			found = _walk_for_resource(value, wanted, depth + 1)
			if found:
				return found
	if isinstance(node, list):
		# Accounts store vaults as (resource, vault) pairs in a map; when the
		# resource matches, the other half is the vault. Do not guess a
		# sibling in an arbitrary longer list: positional component state often
		# contains several unrelated addresses beside a resource.
		if len(node) == 2:
			hexes = [_hex_of(item) for item in node]
			if all(hexes) and hexes.count(wanted) == 1:
				return f"vault_{hexes[1 - hexes.index(wanted)]}"
		for item in node:
			found = _walk_for_resource(item, wanted, depth + 1)
			if found:
				return found
	return None


def _balance_of(vault_body):
	try:
		vault = vault_body["substate"]["Vault"]
		container = vault["resource_container"]
		if "Fungible" in container:
			amount = _amount(container["Fungible"]["amount"])
		elif "Stealth" in container:
			stealth = container["Stealth"]
			amount = _amount(stealth.get("revealed_amount", stealth.get("amount")))
		else:
			return None, (
				"the vault answered in a shape this build does not recognise; the balance may be confidential"
			)
		if amount is None or amount < 0:
			return None, "the vault answered in a shape this build does not recognise"
		return amount, None
	except (AttributeError, KeyError, TypeError, ValueError):
		return None, "the vault answered in a shape this build does not recognise"


# ---------------------------------------------------------------------------
# The ceilings, in words. Charter §2 rule 3: a ceiling ships on the surface
# that offers the feature, beside the promise it bounds -- not in
# documentation to be discovered later.
#
# These are free functions, not methods: they read no chain and need no
# configuration, so a surface can render the wording with the indexer down.
# ---------------------------------------------------------------------------
def ceilings_wording(facts):
	"""Every limit that must be displayed wherever loyalty is offered."""
	rate = facts["redemption_rate"]
	return [
		(
			"The rate is locked. Prices are not.",
			f"{rate} points buy one cent, and that rate can never change — no "
			f"method on this component writes it and none can be added. This "
			f"promises we will not change the rate, never that your points keep "
			f"their value: the merchant still sets prices.",
		),
		(
			"Points cannot be sold or transferred.",
			"They are earnable and redeemable here, and nowhere else. Nothing "
			"can move them off your account.",
		),
		(
			"Ceilings tighten only.",
			f"{facts['per_issue_ceiling']:,} points per award and "
			f"{facts['per_epoch_ceiling']:,} per epoch. Both can be lowered and "
			f"neither can ever be raised.",
		),
		(
			"A redemption must be whole cents' worth.",
			"The remainder stays yours rather than being rounded away.",
		),
		(
			"What earning publishes.",
			"Points land on a public network. The account is linkable and the "
			"amount is inferable, and one account used at one shop for a year is "
			"a purchase history.",
		),
		(
			"There is no owner and no upgrade path.",
			"Nobody can alter these rules after the fact — not the merchant, not "
			"the author. Fixing anything means a new component, and balances do "
			"not move to it automatically.",
		),
	]


def earning_only_notice():
	"""The single most important thing a surface must not get wrong."""
	return (
		"EARNING ONLY. Points accrue and cannot be devalued — that promise is "
		"real and a customer can check it themselves. SPENDING THEM DOES NOT "
		"WORK YET: a redemption needs the customer to co-sign, and no wallet in "
		"reach can do that. Do not tell a customer they can spend these."
	)
