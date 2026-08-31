# cryptopos-rail-ootle

A [Tari Ootle](https://ootle.tari.com) (esmeralda) XTR payment rail for [CryptoPoS](https://github.com/dowoop/cryptopos-core), read over the indexer's REST and server-sent-event API.

> ### ⚠ A payment is bound to a sale only by "first open sale the money covers"
>
> Deposits land in **one shared merchant account**, so this rail cannot tell you
> who a payment was for. Settlement credits every unclaimed, timely deposit into
> that account and settles when the running total reaches the invoice. It does
> **not** match the amount, and it fails without an attacker:
>
> 1. Sale A (invoice 100,000 µXTR) and sale B (invoice 5,000,000 µXTR) are both open.
> 2. B's customer pays 5,000,000 µXTR.
> 3. A polls first, sees an unclaimed timely deposit, and its total covers its invoice.
> 4. **A settles — credited 5,000,000 µXTR against a 100,000 µXTR invoice.**
> 5. B, whose customer actually paid, ends `needs-review` credited nothing.
>
> Reproduced against this adapter, not theorised. Two deposits also **sum**:
> 3,000,000 + 2,000,000 settles a 5,000,000 invoice. Because the test is a
> running total rather than equality, **making each sale's amount unique does
> not fix this.**
>
> A deposit carries a transaction id, so a host keeping a claimed-transaction
> set can stop one transaction being credited twice. Nothing here can tell two
> concurrent sales apart. Do not accept real money on this rail without a
> per-sale binding the host owns.

> **Proven through this published wheel.** On 2026-08-31 a real esmeralda
> payment of 3,141,592 µXTR was charged, observed and settled through this
> package installed as a wheel and resolved through the `cryptopos.rails`
> entry point — transaction
> `d661a4399f3afe5bc77e0f8e03e8245a2a653eaded5d17475c93953f1090d720`.

**Not audited.** No external security audit; never used with mainnet funds.

Install it beside `cryptopos-core` and it registers itself through the
`cryptopos.rails` entry-point group — a host that discovers rails finds it with
no code change:

```bash
pip install cryptopos-rail-ootle
```

```python
from importlib import metadata

for point in metadata.entry_points(group="cryptopos.rails"):
    rail = point.load()
    print(point.name, rail.key, sorted(rail.capabilities))
```

## What it is

A `PaymentRail` implementation: it validates a recipient, builds a payment
request, observes the chain for arriving money, and returns a settlement
decision. It holds **no keys and never spends** — every rail here is a watcher,
and the customer's own wallet is the payer.

Zero runtime dependencies beyond `cryptopos-core`.

## Rails

| entry point | rail key | finality |
|---|---|---|
| `ootle-esmeralda-xtr` | `ootle:esmeralda/native:xtr` | BFT — a transaction is committed or it is not |

## Its observation model is the best of the rails in this project

Every other rail polls: it rescans a block range or re-reads an address, and a
payment that lands between two reads is found only because the next scan
happens to cover it. Ootle does not need that.

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

## Two limits, stated rather than buried

**There is no payment URI.** Ootle publishes no registered deeplink scheme —
searched for, not found — so `create_request` returns the recipient's account
address with a notice saying exactly that. This package will not invent a URI
scheme to make the request look tidier.

**The binding is a static account.** Deposits land in one merchant account, and
a payment is credited by **running total, not by amount match** — see the
warning at the top of this file for the exact sequence and why unique amounts
are not a remedy. Ootle *can* do better: a
payment component taking a sale reference as an argument would bind exactly,
which is the one chain here where the sale id could be an argument rather than
an inference. That is a smart contract, not an adapter change, and it is not in
this package.

## Also included

`cryptopos_rail_ootle.chain.OotleReader` — a read-only client for the indexer.
Every read is total: nothing raises, and a failed read returns a sentinel and a
reason, because a sale must never fail because a policy layer is down.

## What this package does not decide

Pricing, which rails a deployment offers, whether a rail is switched on, and
what an endpoint URL should be are **host** questions. They change per
deployment and are edited by someone with a login. This package answers only
what is true about the chain.

## Testing

```bash
PYTHONPATH=src python -m unittest discover -s tests -t .
```

No test in this package opens a socket.

## Licence

MIT.
