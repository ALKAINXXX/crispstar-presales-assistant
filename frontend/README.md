# 澄星售前助手前端

This is the React/TypeScript display layer for the existing FastAPI project.
Its fixed source/runtime baseline, copied component paths, changes, and license
are documented in [NOTICE.md](NOTICE.md). The dependency graph is independently
installable from public npm with the committed lockfile.

From the repository:

```powershell
cd frontend
npm ci
npm run build
npm run test
```

`npm run build` type-checks the source and writes `app/ui/index.html` and hashed
`app/ui/assets/index-*.js/css`. Existing FastAPI continues to serve `/` and
`/assets`. `generated-assets.json` records only assets owned by this build, so
subsequent builds can remove those exact obsolete bundles without deleting
unrelated assets. Set `UI_BUILD_OUT_DIR` to write a separate build directory;
leave it unset to build into `app/ui`.

Development (optional): start existing FastAPI on `127.0.0.1:8000`, then run
`npm run dev` here. Vite on `127.0.0.1:5173` proxies `/chat`, `/sessions`,
`/health`, `/proposal`, `/quotes`, and `/data-management` to that server. The
production entry uses relative URLs and is served
from the same origin as FastAPI; there is no frontend model endpoint.

The runtime uses `useExternalStoreRuntime` with one ordinary JSON `/chat`
request per turn and server-owned sessions. It has no streaming transport,
client tools, cloud credentials, local business-state persistence, or additional
database. Browser storage keeps only application identity, the last active
**session ID**, and original save/management operation references. These keys
are scoped by the service-reported dataset identity, actual location identity,
and clearing epoch. Snapshot contents, requirements, and amounts are not stored
in browser storage. The first connection migrates legacy references once,
for read-only verification; it does not restore a save permit.

`src/business/adapter.ts` manages request identity, per-session submission
guards, view generations, and session reloads. An old response never writes into
another conversation's messages. Revisiting its original conversation while a
request completes causes a fresh server read. No onEdit/onReload/onCancel,
branch, archive, attachments, or message editing adapters are enabled. Saving
and data management use project-owned ordinary JSON endpoints and verified
receipts; they do not add model calls.

`business.presentation.answer_markdown` is the server-produced page text.
`ChatResponse.message` is the independent, stored history projection from those
same facts. The browser does not parse or remove sentences from a model answer.
`show_comparison` and `show_current_result` decide whether a turn owns a business
display. An ordinary question does not redraw the prior task summary.

Current selection identity comes from `current_task.result_turn_id` and
`result_message_id`, with the actual server `status` controlling validity.
The top “查看当前结果” button locates the live result when present. After a
refresh or session switch it opens a read-only card returned by `/sessions/{id}`
and can locate the original stored reply if the server message ID is available.
Missing process state is not reconstructed from history text.

DataTable keeps business order and renders the four server support states.
The same compound cell renderer is used by its table and narrow cards.
Sources uses only explicitly adopted server source identities for answer
evidence; the other real recall fragments remain in technical records.
Raw source text is rendered in full as ordinary text, never injected HTML.
Money is formatted from safe integer fen using BigInt; the browser does not
multiply, round, or replace the server total/formula.

Saving, original-action verification and direct quote lookup remain in the
ordinary chat shell. It also includes a complete quote directory read from actual local
archives, independent chat/quote deletion, one persistent “最近删除” area,
restoration, permanent deletion and one “清除当前应用记录” entry under the fixed
sidebar “数据管理” entry. Confirmation displays the actual storage path, complete
normal/trash counts, processing scope and retained external copies. Trash has no
expiry. Restoring an object leaves the current area selected and provides a
separate “查看” action; it never restores old process work or saving permission.

The controller verifies the service scope before the production page allows a
message or save. Mutations carry `X-Data-Epoch` and `X-Data-Location`; an existing
chat also carries `X-Session-Version`. Focus/storage events and a lightweight
three-second metadata check synchronize other open pages. A global archive
revision change does not by itself discard a valid current Proposal. A changed
active chat version or last persisted message ID triggers a historical read,
and actual quote deletion closes the affected direct detail only. HTTP receipt
loss and finite timeouts retain the original operation reference for read-only
verification, without automatic resubmission. Unknown full clearing results
pause messages and saves until verification.

`npm test` covers the production controller and presentation with controlled
transport timing. Scope/location isolation, independent deletion, history,
complete quote discovery, pending receipts and late-response behavior are in
`src/business/management.test.tsx`. These tests do not establish physical SQLite
deletion, service restart behavior or real browser layout; these require separate
verification.
