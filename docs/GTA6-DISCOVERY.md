# Passive weapon recipe discovery

Opening money saves and downloading money edits schedule a background scan. The existing car editor loads a small hook that scans on `GTA6Cars.open` and `toBytes`, covering opened saves and car additions without replacing its editor or contribution system. Scanning never changes save bytes. Candidates are not automatically trusted as usable guns.

Only 32-bit weapon recipe/category identifiers and an unreviewed status are collected. The server rejects extra report fields. No save archives, account IDs, paths, balances, mission progress, credentials or instance IDs are included. Known74 catalogue recipes are filtered and repeated reports deduplicated. Car and gun editors can share the same scanner later. Existing car contribution handling remains unchanged.

Reports queue in `workspace/gta6-discovery`. To synchronize them to GitHub, the host operator must set `GTA_DISCOVERY_GITHUB_TOKEN` to a server-side token with Contents write permission to `Joshhooger/gta6-money-editor`. Optional `GTA_DISCOVERY_REPO` overrides that destination. Aggregate file: `data/weapon-discoveries.json`. The connector credential is never extracted, bundled or placed in browser code. Without the server token, local collection still works but GitHub synchronization is not active.

Synchronization is background-only; GitHub failures retain receipts for retry on a subsequent scan. Expected SHA updates retry conflicts and merge concurrent candidates. No editor action waits for network synchronization. Deploy route, helper, known catalogue and static hook together. No live collection or deployment was verified in this change.
