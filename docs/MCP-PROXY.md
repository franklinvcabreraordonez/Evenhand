# The MCP witness proxy

An agent that spends money usually does it through tools, and more and more of those tools are MCP servers. The proxy sits between the agent's host and a tool server, passes every message through untouched, and has an Evenhand witness record each purchase the agent makes. Nothing in the agent or the tool server has to change.

```
agent host  <-->  evenhand-mcp  <-->  the real MCP server (the shop, the payments tool)
                       |
                    a witness: records, signs and chains a receipt for every purchase
```

Why a proxy: the witness has to sit outside the agent's control. A proxy is on the tool path, so the agent cannot skip it, and the record is made by a separate process the agent does not run.

It can also hold back a purchase before it happens (`--enforce`), and it gives the agent a plain explanation when it does, so the agent can choose something else.

> Status: a prototype, tested against a stand-in shop server and the written MCP specification, not yet against third-party servers in the field. Run `evenhand-mcp selftest` on your machine first.

## Try it in two minutes

```
python -m pip install -e .
evenhand-mcp selftest
evenhand-mcp demo
```

If `evenhand-mcp` is not found, use `python -m evenhand.mcp` in its place (on Windows, `py -m evenhand.mcp`). `selftest` takes a few seconds and checks the whole path on your machine. `demo` plays the full story: an agent buys, one purchase is outside what the user allowed, the evidence is checked, and a second session shows the purchase being blocked.

## Wrapping a real tool server

You need three things: the user's signed mandate, a description of which tool calls are purchases, and the real server's command.

**1. The user's key and mandate.** In real use a wallet signs the mandate. Until then:

```
evenhand-mcp keygen --out user.key
evenhand-mcp mandate create --key user.key --intent "buy running shoes under $120" --merchants stridemart --max-per-txn 120 --categories sports --out mandate.json
```

Keep `user.key` secret. The second command prints the user's public key. Pin it with `--principal-key` so the witness accepts mandates only from that key.

**2. Run the proxy in front of the server.** Everything after `--` is the real server's command:

```
evenhand-mcp run --data-dir C:\evenhand\shop --mandate mandate.json --principal-key <USER_PUBLIC_KEY> --tool "buy_item:merchant=merchant,amount=price,description=item,category=category" --enforce hard -- npx -y some-shop-mcp-server
```

**3. Point the agent host at the proxy** instead of the server. Hosts that start MCP servers from a command (Claude Desktop, Cursor and others) take a command and arguments, so put the proxy's there. A settings file keeps it short:

```json
{
  "mcpServers": {
    "shop": {
      "command": "evenhand-mcp",
      "args": ["run", "--config", "C:\\evenhand\\shop.json"]
    }
  }
}
```

A host may not find `evenhand-mcp` on its path. Then give the full path to Python: `"command": "C:\\Python\\python.exe", "args": ["-m", "evenhand.mcp", "run", ...]`. Find yours with `py -c "import sys; print(sys.executable)"`.

## Telling the proxy what a purchase is

The proxy needs to know, for each tool, where the merchant and the amount are in the call. Write it on the command line with `--tool`, or in a settings file.

```
--tool "NAME:merchant=PATH,amount=PATH,description=PATH,category=PATH,amount_scale=0.01"
```

A path is a name in the call's arguments, such as `price`, or a dotted path such as `store.id` or `items[0].name`. A few more things you can do:

- **Cents instead of dollars:** `amount_scale=0.01`. The proxy converts with exact decimal arithmetic, so 1999 becomes 19.99.
- **A fixed value:** `category=const:sports`.
- **A value from the result:** start the path with `result.`, such as `amount=result.structuredContent.total`. This cannot be combined with `--enforce`, which needs the amount before the call runs.
- **Several tools:** repeat `--tool`. A name may use `*`, as in `pay_*`.
- **Let the proxy guess:** `--watch "buy_*"` witnesses matching tools and looks for common names (`merchant`, `store`, `price`, `total`, `amount_cents` and so on).

Map a `description`. It is what the adjudicator reads to judge whether the purchase matches what the user asked for. Without one the receipt says only the tool's name.

The same settings in a file:

```json
{
  "data_dir": "C:\\evenhand\\shop",
  "mandate": { "file": "C:\\evenhand\\mandate.json" },
  "principal_key": "<USER_PUBLIC_KEY>",
  "tools": {
    "buy_item": { "merchant": "merchant", "amount": "price", "description": "item", "category": "category" },
    "checkout": { "merchant": "store.id", "amount": "total_cents", "amount_scale": 0.01, "description": "items" }
  },
  "watch": ["pay_*"],
  "enforce": "hard",
  "on_witness_down": "block",
  "upstream": { "command": ["npx", "-y", "some-shop-mcp-server"], "env": { "SHOP_API_KEY": "..." } }
}
```

Flags override the file.

## What gets recorded, and when

The proxy records a receipt only when a watched call comes back as a finished, successful result. It does this before the agent sees the result, so the agent never acts on a purchase that has no receipt.

| What the server answers | Receipt? |
|---|---|
| A finished result | Yes |
| A tool error (`isError: true`), such as a declined card | No. Nothing was bought |
| A protocol error | No |
| "Input required", the server asking for more before it can finish | No. The retry, if it finishes, gets one |
| A handle for a long-running task | No |

Only the fields you map are kept: the merchant, amount, description and category, and the currency and sku if you map them. The proxy never writes the call's other arguments to its log.

## Enforcement

| `--enforce` | What happens |
|---|---|
| `off` (default) | Every purchase goes through and is witnessed. Verdicts come later, from the evidence |
| `hard` | A purchase that breaks the mandate's limits is stopped before it reaches the server: merchant list, per-purchase and total caps, categories, validity window |
| `strict` | As `hard`, and the item must also clearly match what the user asked for |

A blocked call is answered with a tool error that says why and that nothing was bought, so the agent can pick something else. A purchase above the mandate's confirmation threshold is blocked too: the proxy has no way to ask the user, and an agent's own claim that the user confirmed would prove nothing. Two purchases at the same moment cannot both slip under a total cap, because each holds its amount until it is answered.

The checks are the same hard constraints the adjudicator applies later, and the tests keep the two in step.

## When things go wrong

- **The witness is unreachable.** By default (`--on-witness-down block`) the purchase is refused: no receipt, no purchase. With `queue` the purchase goes ahead, the receipt is kept in the proxy's log, and it is recorded as soon as the witness is back, also after a restart.
- **A call cannot be read** (no amount where the mapping says). With enforcement it is blocked. Without it the call goes through, is logged as unrecorded, and a warning is printed.
- **The proxy is stopped or killed.** Receipts already recorded are safe. Calls that were in flight are marked as interrupted in the log the next time the proxy starts, because whether they went through is unknown.
- **Two proxies on one data folder.** The second refuses to start. Give each proxy its own `--data-dir`.
- **One thing to know.** If the proxy dies after the witness acknowledged a receipt but before it noted that in its own log, a retry could record the same purchase twice. Receipts have no idempotency key yet.

The proxy's own log is `audit.jsonl` in the data folder, one JSON line per event. It is not signed. The witness's record is.

## Looking at the evidence

```
evenhand-mcp report --data-dir C:\evenhand\shop --user-key <USER_PUBLIC_KEY> --witness-key <WITNESS_PUBLIC_KEY> --disputes all --packets packets
```

It lists the receipts, verifies the chain, and for each receipt you ask about it runs the adjudicator and writes an evidence packet (JSON, and PDF if `reportlab` is installed).

- Take the witness key from a source other than the witness, and the user key from the user. Without both, the check proves only that the record is consistent with itself, and the report says so.
- `--save-checkpoint FILE` asks the witness for a signed checkpoint. Keep it where the witness cannot reach it. Later, `--checkpoint-file FILE` checks that no receipts were cut off the end. See [TRUST-MODEL.md](TRUST-MODEL.md).
- The report opens the data folder itself, so stop the proxy first. To report on a running proxy, run it with `--witness-url` against a witness server instead (see [HTTP-API.md](HTTP-API.md)).

## Remote servers: the HTTP gateway

For an MCP server reached over the network, run a local gateway and point the client at it:

```
evenhand-mcp serve-http --upstream-url https://shop.example/mcp --listen 127.0.0.1:8788 --data-dir C:\evenhand\shop --mandate mandate.json --tool "buy_item:merchant=merchant,amount=price,description=item" --enforce hard
```

The gateway passes each request and its headers on unchanged, including `Authorization`, the session header of the older protocol revisions, and the `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` headers the newest revision checks against the body. It streams event streams through, recording a purchase's receipt before the event carrying its result is released. Each HTTP exchange is tracked on its own, so two clients using the same request id are never confused.

It answers only to local names (a defence against DNS rebinding) and refuses browser pages from other sites. `--allow-host` and `--allow-origin` add names. It has no authentication of its own, so keep it on a machine and network you trust.

## Protocol notes

- The proxy works with both eras of the protocol. It forwards everything, including `initialize`, `server/discover`, notifications and requests from the server, and looks only at `tools/call` and its answer.
- It writes nothing to standard output except the messages it passes on. Its own messages go to standard error.
- A server reached over HTTP cannot be wrapped by `run`. Use `serve-http`, or give `run` a bridge such as `mcp-remote` as the command.
- A watched call that arrives inside a JSON-RPC batch is handled, and if any of it is blocked, the whole batch is.

## Settings

| Flag | Meaning |
|---|---|
| `--config FILE` | A JSON settings file. Flags override it |
| `--data-dir DIR` | The witness key, its records and the proxy's log. Default `~/.evenhand-mcp`. Use a full path |
| `--mandate FILE`, `--mandate-id ID`, `--dev-mandate INTENT` | The mandate: a signed file, one the witness already has, or a throwaway one for trying things (testing only) |
| `--principal-key HEX` | Accept mandates only from this user key |
| `--witness-url URL`, `--api-key KEY`, `--witness-key HEX` | Use a witness server that is already running, and pin its key |
| `--tool`, `--watch` | What to witness, as described above |
| `--enforce off\|hard\|strict` | Block purchases outside the mandate |
| `--on-witness-down block\|queue` | What to do when the witness cannot be reached |

## What this does and does not protect

The proxy makes the record independent of the agent: it is made by a different process, signed with a key the agent never holds, and chained so that edits and deletions show. It does not stop whoever configures the agent host from removing the proxy from the host's settings, and a tool that bypasses MCP is not seen at all. The witness key lives in the data folder, so protect that folder. A cap on total spending is only exact if one proxy serves a mandate. And a purchase that goes through a tool the proxy was never told to watch leaves no receipt.