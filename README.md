# cryptopos-rail-ootle

A [Tari Ootle](https://ootle.tari.com) (esmeralda) XTR payment rail for [cryptopos-core](https://github.com/dowoop/cryptopos-core), read over the indexer's REST and server-sent-event API.

It holds **no keys and never spends**. It is a watcher: the customer's own
wallet is the payer, and this package only tells you what the chain says.

```bash
pip install cryptopos-rail-ootle
```

Installing it *is* the integration — it registers itself through the
`cryptopos.rails` entry-point group, and a host that calls `discover()` finds it
with no code change.

> ### ⚠ Two bindings, and the HOST chooses which one it gets
>
> **With a payment component (`payment_component` in the rail's configuration)
> the money names the sale.** The payer calls the component's `pay` method with
> the sale reference; the deposit event carries that reference, and this rail
> credits only deposits naming the sale it is settling. Nothing is inferred from
> amount, timing or polling order. That is the mode to use, and it is what
> `cryptopos-rail-ootle` 0.2.0 added.
>
> **Without one, deposits land in one shared merchant account and this rail
> cannot tell you who a payment was for.** Settlement credits every unclaimed,
> timely deposit into that account and settles when the running total reaches the
> invoice. It does **not** match the amount, and it fails without an attacker:
>
> 1. Sale A (invoice 100,000 µXTR) and sale B (invoice 5,000,000 µXTR) are both open.
> 2. B's customer pays 5,000,000 µXTR.
> 3. A polls first, sees an unclaimed timely deposit, and its total covers its invoice.
> 4. **A settles — credited 5,000,000 µXTR against a 100,000 µXTR invoice.**
> 5. B, whose customer actually paid, ends `needs-review` credited nothing.
>
> Reproduced against this adapter, not theorised. Two deposits also **sum**:
> 3,000,000 + 2,000,000 settles a 5,000,000 invoice. Because the test is a
> running total rather than equality, **making each sale's amount unique does not
> fix this.**
>
> A deposit carries a transaction id, so a host keeping a claimed-transaction set
> can stop one transaction being credited twice. Nothing in the shared mode can
> tell two concurrent sales apart. **Do not accept real money on the shared
> account.** [Configure a payment component](#2-choose-your-binding-out-loud), or
> supply a per-sale binding the host owns.
>
> The rail does not decide which mode it is in and cannot: a host that reports a
> binding to its operator must compute it from its own configuration, because the
> same adapter is per-sale with a component and shared without one.

> **Proven through this published wheel.** On 2026-08-31 a real esmeralda payment
> of 3,141,592 µXTR was charged, observed and settled through this package
> installed as a wheel and resolved through the `cryptopos.rails` entry point —
> transaction `d661a4399f3afe5bc77e0f8e03e8245a2a653eaded5d17475c93953f1090d720`.
>
> The per-sale binding added in 0.2.0 was proven on the same network the same
> day, through the host this package was extracted from: a sale charged, paid by
> a wallet holding a key the merchant has never seen, settled and booked. A
> payment naming the **wrong** sale reference, for the exactly correct amount,
> into the correct component, was refused — which is the stronger evidence,
> because it is the case the shared account gets wrong.

**Not audited.** No external security audit; never used with mainnet funds.

## Rails

| entry point | rail key | finality |
|---|---|---|
| `ootle-esmeralda-xtr` | `ootle:esmeralda/native:xtr` | BFT — a transaction is committed or it is not |

---

# Cookbook

The five-call sequence, the settlement states, and the four host obligations are
in [cryptopos-core's cookbook](https://github.com/dowoop/cryptopos-core#the-five-calls).
This file covers only what is specific to Ootle.

## 1. Configure it

```python
configuration = {
    "endpoint": "https://ootle-indexer-a.tari.com",
    "payment_component": "component_pay_abc123",   # OMIT THIS AND YOU GET THE SHARED MODE
    "timeout_seconds": 10,
}
```

`payment_component` is the single most consequential line of configuration in
this package. With it, money names the sale. Without it, this rail cannot tell
two concurrent sales apart — see the warning above.

## 2. Choose your binding, out loud

The rail reports the choice back to you in the payer's own instructions, so the
difference is visible rather than buried in a config file.

<!-- readme: new -->
```python
from cryptopos_core.plugin import PaymentIntent, RecipientBaseline
from cryptopos_rail_ootle import ootle_esmeralda as rail

rail.key                                 # -> 'ootle:esmeralda/native:xtr'
ACCOUNT = "component_6f1d2c4b8a9e0f3d5c7b1a2e4f6d8c0b"

def request_with(component):
    baseline = RecipientBaseline(rail.key, ACCOUNT, "indexer", tip=5,
                                 payment_component=component)
    intent = PaymentIntent("sale-1042", rail.key, ACCOUNT, 3_141_592,
                           1_787_100_000, 1_787_101_800,
                           payment_reference="sale-1042", baseline=baseline)
    return rail.create_request(intent)
```

**Shared mode** — the payer is told to send an amount to an address, and nothing
in that instruction names the sale:

```python
request_with("").payer_notice
#   -> 'Ootle has no registered payment URI; this is an account address, not a deeplink. Send exactly 3141592 microTari.'
```

**Per-sale mode** — the instruction names the sale, and says what happens if it
is ignored:

```python
request_with("component_pay_abc123").payer_notice
#   -> "Pay by calling this component's `pay` method with the sale reference 'sale-1042'; a plain transfer to this address names no sale and will not be credited. Send exactly 3141592 microTari."
```

Note the URI field also changes — it becomes the component the payer must go
through, not the merchant account:

```python
request_with("component_pay_abc123").uri            # -> 'component_pay_abc123'
```

**The component travels on the baseline, not on a method argument.** That is
deliberate: `create_request` takes only an intent, and adding a required
argument to a published protocol method made every installed 0.1.0 wheel
undriveable while the source suite stayed green. The adapter that needs the fact
is the one that captured the baseline, so the fact travels with the baseline.

**`binding_category` stays `not-unconditional` in both modes**, because the rail
genuinely does not know which mode it is in:

```python
rail.binding_category                    # -> 'not-unconditional'
```

A host that reports its binding to an operator must compute it from its own
configuration. Reading it off the rail would report the shared mode's weakness
for a deployment that had correctly configured a component, and — far worse —
could be made to report safety it does not have.

## 3. There is no payment URI, and that is not an omission

Ootle publishes no registered deeplink scheme — searched for, not found — so
`create_request` returns an **account address** and a notice saying exactly
that. This package will not invent a URI scheme to make the request look tidier.

```python
rail.validate_recipient(ACCOUNT)[0]      # -> 'unchecked'
```

`unchecked` is not a soft `ok`. Ootle account identifiers carry no local
checksum, so claiming the address was verified would make the verdict
meaningless everywhere else it is used.

Practically: render the address as text the customer can copy, or as a QR of the
plain address, and show `payer_notice` beside it. Do not build a `ootle:` URI.

## 4. Observation resumes; it does not rescan

This is the best observation model of any rail in this project. Every other rail
polls — it rescans a block range or re-reads an address, and a payment landing
between two reads is found only because the next scan happens to cover it. Ootle
does not need that:

```
GET /transactions/events/stream?substate_id=<vault>&topic=std.vault.deposit&after_id=<n>
```

filters to one vault, carries a **transaction id and an exact amount** on every
deposit, and replays from a cursor. The rail resumes from the last event id it
saw, so it cannot miss a payment between polls.

There is **no confirmation depth**, and that is a property of the consensus
rather than a gap: Ootle commits are final, there is no reorg to outlive, and
waiting "three more of something" would be waiting for a thing that does not
happen.

Against a live indexer the loop is the standard one:

<!-- readme: skip -->
```python
baseline = rail.capture_baseline(ACCOUNT, configuration)   # drains the history
batch = rail.observe(intent, configuration)
while not batch.complete:
    batch = rail.observe(intent, configuration, batch)
decision = rail.settle(intent, batch, claimed_transaction_ids=already_credited)
```

## 5. Budget for the timeouts

**Every read of the event stream costs its full timeout.** The endpoint documents
a `:` comment while idle and, measured against `ootle-indexer-a.tari.com` on
2026-08-31, never sends one: a replay from `after_id=0` returned five events in
4.56 s and the next read returned no bytes in 4.34 s. So the end of a history
looks like silence, and a drained baseline costs two reads.

That matters most at `capture_baseline`, which is on the customer-facing path.
Budget ~10 s for starting a sale, and do not put it inside a request handler
that has a shorter timeout than that.

## Three more limits, stated rather than buried

**A baseline is drained, and a read that stops short is not a baseline.**
`capture_baseline` replays the event stream until a page adds nothing, rather
than accepting the first read. A single bounded read over a long history hands
back a cursor in the middle of the past, and every deposit after that cursor —
money that arrived before the sale existed — then looks like a payment for it.
Settlement bounds the window at **both** ends as well, with an hour of clock
skew allowed, so a deposit predating its sale goes to review rather than
settling. A history too long to drain is a refusal: no sale is charged against a
starting point this rail knows it cannot trust.

**A transaction the indexer does not call committed cannot settle a sale.** Ootle
finality is a property of a *committed* transaction, so the outcome is read and
checked rather than assumed. An `Abort`, a `Reject`, or a summary this build
cannot read is reported as an unconfirmed transfer with the outcome named —
never silently dropped, because an indexer wrong about an abort would otherwise
rob a customer who really paid.

**The component is a smart contract, not an adapter setting.** This package
drives it and does not contain it. Deploying and funding it is a separate step
that happens on the network, not in `pip`.

## Also included

`cryptopos_rail_ootle.chain.OotleReader` — a read-only client for the indexer,
used for the optional loyalty/policy tier. **Every read is total: nothing in
that module raises**, and a failed read returns a sentinel and a reason, because
a sale must never fail because a policy layer is down.

<!-- readme: skip -->
```python
from cryptopos_rail_ootle.chain import OotleReader, ceilings_wording

reader = OotleReader(loyalty_component="component_abc...")
facts, reason = reader.promise()
if facts is None:
    print(f"policy layer unavailable: {reason}")   # check the sentinel; there is nothing to catch
else:
    for heading, body in ceilings_wording(facts):
        print(heading, "--", body)
```

It refuses a non-https indexer and refuses redirects that leave HTTPS. Its
`promise()` facts carry the indexer that answered — the default is a **testnet**
indexer, because no mainnet policy tier is published.

## What this package does not decide

Pricing, which rails a deployment offers, whether a rail is switched on, and
what an endpoint URL should be are **host** questions. They change per
deployment and are edited by someone with a login. This package answers only
what is true about the chain.

## Testing

```bash
PYTHONPATH=src python -m unittest discover -s tests -t .
python3 tools/readme.py --wheel   # every example above, against the wheel
```

No test in this package opens a socket.

## Licence

MIT — the full text is in [`LICENSE`](LICENSE), which ships in the sdist and the
wheel.
