# Simplified GTA VI money form

The `/gta6` template now shows money totals, $10M / $20M / $50M / Max Money / Max Cash, and the existing edit/encrypt/download actions. It does not enumerate internal inventories, character choices, item counts or capacity tables. A bank choice leaves cash alone; Max Cash sets cash to20,000 and can be toggled off. Buttons select values without submitting; the action buttons become available after a selection.

`routes/gta6.py` uses the bundled module’s shared-bank limits and accepts `max` for both bank and cash. Ownership checks, ZIP handling and crypto jobs remain unchanged. `services/gta6_money.py` matches Joshhooger/gta6-money-editor v1.5.1, retaining large-denomination support and bounded activation of existing empty bank slots. No bank account or slot is fabricated. Empty-slot activation has local structural tests but still awaits gameplay spending/save/reload evidence.

Deploy this repository’s template, route and bundled service together. Updating the separate money-module repository alone cannot replace this hard-coded host form. No live deployment or console transfer was performed by these changes.

Run `python tests/test_gta6_money_controls.py path/to/zero-bank.zip path/to/funded.zip` for local route-helper and real-save checks. Tests run locally without Quart, service requests or customer data in the repository. DOM interaction checks additionally covered all five choices and preservation of the host submit actions.
