#!/usr/bin/env python3
"""
gta6_money.py - set the bank balance in a decrypted GTA VI (PS5) story save.

Works on the *decrypted* save data (the "JSAV" file GarlicSaves produces,
e.g. SARC0001 or memory.dat), or on a decrypted workspace .zip as returned by
GarlicSaves' decrypt. The output is ready to be re-encrypted / resigned.

No third-party packages. Python 3.8+.

How GTA VI stores money (as observed on PS5 story saves, Nov 2026):
  * Each character has an inventory  ($0x789bdd41 = joaat("Inventories")).
  * An inventory holds 5 item tables. Money lives in table 1 ($0xfc8f2620) as
    parallel arrays: id1, type, id2, id3, value, flag.
  * Bank money = one "bank" item (id3 6837455, capped at the account tier,
    e.g. 15,000) PLUS any number of "bank stack" items (id3 2177639769).
    The in-game balance AND the displayed limit are the sum of all of these.
  * Each stack is capped at 85,000. Larger values are clamped on load.
  * The inventory also has an Items index ($0xabb1e011 = joaat("Items")):
    one entry per item, encoded (position << 4) | table_number (table 1..5).
    Items that are not in the index are DROPPED by the game on load, so every
    added stack must also be added to the index.
  * The index appears to be limited to 4,095 entries per inventory.
  * Cash on hand is a single item, id3 4163145158.
"""

import argparse
import io
import json
import os
import struct
import sys
import zipfile

__version__ = "1.5.1"

# --- field hashes / identifiers -------------------------------------------
K_ROOT = "$0x7a47cb05"
K_PARENT = "$0xd3f59a5e"
K_INVENTORIES = "$0x789bdd41"          # "Inventories"
K_ITEMS_INDEX = "$0xabb1e011"          # "Items"
K_REGISTER = "$0xd558e4da"             # parent register (items point into it via F_TYPE)
TABLES = ["$0xfc8f2620", "$0x6ebbccb1", "$0xc500ddf4", "$0x9098d12e", "$0x1085013f"]
MONEY_TABLE = TABLES[0]                # table number 1 in the Items index
F_ID1, F_TYPE, F_ID2, F_ID3, F_VALUE, F_FLAG = (
    "$0xb4bfeab1", "$0xc5730137", "$0xbe8e255c", "$0x12ae4287", "$0xe3fead7a", "$0x63b44f39")

ID3_BANK = 6837455
ID3_BANK_STACK = 2177639769
ID3_CASH = 4163145158
ID2_ECONOMY = 2084597891
BANK_ACCOUNT_ID2 = 869874572          # the bank account item every bank slot hangs under (all saves seen)

STACK_MAX = 85_000                     # game clamps any bank stack above this
LEDGER_MAX = 2_147_483_647             # spendable ledger is a signed 32-bit value
BANK_CEILING = 999_999_999             # highest balance this editor will set: the ledger would
                                       # overflow past ~2.1 billion, and a known-good modded save
                                       # sits just under 1 billion, so stay inside what's proven
ID3_BIG_STACK = 867169743              # the bank's large-denomination item
BIG_MAX = 1_500_000                    # game clamps one of those at 1.5M (seen 666 of them at exactly this)
ITEM_LIMIT = 4095                      # Items index size limit per inventory
NEW_ID1_BASE = 8489902515008300000     # id1s for new stacks start here (must be unique)

# --- safety limits (enforced; these are the values proven in-game) ---------
SAFE_RESERVED_ITEMS = 100              # item slots always left free for items the game adds later
SAFE_ITEM_LIMIT = ITEM_LIMIT - SAFE_RESERVED_ITEMS
CASH_MAX = 20_000                      # highest cash value confirmed in-game
EDITABLE_CHARACTERS = {0: "Shared bank"}

CHARACTER_NAMES = {0: "Shared bank", 1: "Jason (inventory only)"}


class SaveError(Exception):
    pass


def _arr(field):
    """Return the Python list behind a JSAV array field ({"$content":..,"$data":[..]} or a bare list)."""
    if isinstance(field, dict) and isinstance(field.get("$data"), list):
        return field["$data"]
    if isinstance(field, list):
        return field
    return None


def _dumps(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class Inventory:
    def __init__(self, raw, number):
        self.raw = raw
        self.number = number
        t = raw[MONEY_TABLE]
        self.id1, self.type, self.id2 = _arr(t[F_ID1]), _arr(t[F_TYPE]), _arr(t[F_ID2])
        self.id3, self.value, self.flag = _arr(t[F_ID3]), _arr(t[F_VALUE]), _arr(t[F_FLAG])
        self.index = _arr(raw[K_ITEMS_INDEX])
        self._bank_account_computed = False
        self._bank_account_cache = None
        cols = [self.id1, self.type, self.id2, self.id3, self.value, self.flag]
        if any(c is None for c in cols) or len({len(c) for c in cols}) != 1:
            raise SaveError(f"inventory {number}: money table columns are missing or uneven")

    @property
    def name(self):
        return CHARACTER_NAMES.get(self.number, f"inventory {self.number}")

    def _positions(self, id3):
        return [i for i, x in enumerate(self.id3) if x == id3]

    def table_sizes(self):
        sizes = []
        for t in TABLES:
            col = _arr(self.raw[t][F_VALUE]) if t in self.raw and isinstance(self.raw[t], dict) else None
            sizes.append(len(col) if isinstance(col, list) else 0)
        return sizes

    # ---- parent links: every item's F_TYPE is an index into the parent register ----
    def _register(self):
        r = _arr(self.raw.get(K_REGISTER)) if isinstance(self.raw, dict) else None
        return r if isinstance(r, list) else []

    def _parent_pos(self, i):
        """Position (in the money table) of item i's parent, or None."""
        reg = self._register(); c = self.type[i]
        if not isinstance(c, int) or c < 0 or c >= len(reg):
            return None
        if not getattr(self, '_pos_cache', None) or len(self._pos_cache) != len(self.id1):
            self._pos_cache = {x: k for k, x in enumerate(self.id1)}
        return self._pos_cache.get(reg[c])

    def _account_of(self, i):
        """id2 of the account an item's slot belongs to (item -> slot -> account)."""
        p = self._parent_pos(i)
        a = self._parent_pos(p) if p is not None else None
        return (self.id2[a] & 0xffffffff) if a is not None else None

    @property
    def bank_account(self):
        """The bank account's id2: from the bank item if present, else the account most stacks sit under."""
        if self._bank_account_computed:
            return self._bank_account_cache
        account = None
        for i in self._positions(ID3_BANK):
            account = self._account_of(i)
            if account is not None:
                break
        if account is None:
            accs = [self._account_of(i) for i in self._positions(ID3_BANK_STACK) + self._positions(ID3_BIG_STACK)]
            accs = [a for a in accs if a == BANK_ACCOUNT_ID2] or [a for a in accs if a is not None]
            account = max(set(accs), key=accs.count) if accs else None
        self._bank_account_cache = account
        self._bank_account_computed = True
        return account

    def _in_bank(self, i):
        acc = self.bank_account
        return acc is not None and self._account_of(i) == acc

    @property
    def bank_field(self):
        return sum(self.value[i] for i in self._positions(ID3_BANK))

    @property
    def stacks(self):
        """Stacks that really count: those under the same bank account as the bank item."""
        return [self.value[i] for i in self._positions(ID3_BANK_STACK) if self._in_bank(i)]

    @property
    def orphan_stacks(self):
        """Stacks the game ignores (wrong parent): left behind by older editors."""
        return [i for i in self._positions(ID3_BANK_STACK) if not self._in_bank(i)]

    @property
    def bank(self):
        """Everything held in the bank account (bank item, stacks and other bank item types)."""
        if self.bank_account is None:
            return 0
        return sum(self.value[i] for i in range(len(self.value))
                   if self.id3[i] != ID3_CASH and self._in_bank(i) and isinstance(self.value[i], int))

    @property
    def cash(self):
        p = self._positions(ID3_CASH)
        return self.value[p[0]] if p else 0

    @property
    def items_used(self):
        return len(self.index)

    def max_bank(self):
        """Highest bank balance allowed: existing stacks full, plus new stacks up to
        SAFE_ITEM_LIMIT items (always leaving SAFE_RESERVED_ITEMS slots free)."""
        if self._denomination()[0] is None:
            return self.bank
        free = max(SAFE_ITEM_LIMIT - (self.items_used - len(self.orphan_stacks)), 0)
        _, cap, _ = self._denomination()
        bigs = [self.value[i] for i in self._positions(ID3_BIG_STACK) if self._in_bank(i)]
        headroom = (sum(STACK_MAX - v for v in self.stacks if v < STACK_MAX)
                    + sum(BIG_MAX - v for v in bigs if v < BIG_MAX))
        # the spendable ledger is a signed 32-bit value, so money above that can't be spent
        return min(max(self.bank, self.bank + headroom + cap * free), BANK_CEILING, LEDGER_MAX)

    def _stack_parent(self):
        """Register index that new stacks must use: the parent of an existing, correctly placed stack."""
        good = [i for i in self._positions(ID3_BANK_STACK) if self._in_bank(i)]
        return self.type[good[0]] if good else None

    def _big_parent(self):
        """Register index for large-denomination items, if this save already holds one.

        These sit in their own slot under the bank account and hold up to BIG_MAX each -
        about 17x an 85k stack - so a save that has one can reach a far higher balance
        before running out of item slots. We never invent the slot, only reuse it."""
        good = [i for i in self._positions(ID3_BIG_STACK) if self._in_bank(i)]
        return self.type[good[0]] if good else None

    def _denomination(self):
        """(register index, per-item cap, id3) to use when appending new money items."""
        big = self._big_parent()
        if big is not None:
            return big, BIG_MAX, ID3_BIG_STACK
        return self._stack_parent(), STACK_MAX, ID3_BANK_STACK

    def _lower_bank(self, target):
        """Lower the bank to `target`: zero/trim bank items and remove spare stacks.
        One stack is always kept (possibly at 0) so every bank slot keeps an item and no parent link breaks."""
        items = [i for i in range(len(self.value)) if self.id3[i] != ID3_CASH and self._in_bank(i)]
        stacks = [i for i in items if self.id3[i] == ID3_BANK_STACK]
        others = [i for i in items if self.id3[i] != ID3_BANK_STACK]
        left = target
        for i in others:                      # bank item etc.: keep up to their current value
            v = min(self.value[i], left); self.value[i] = v; left -= v
        keep = []
        for i in stacks:
            if left > 0 or not keep:
                v = min(STACK_MAX, left); self.value[i] = v; left -= v; keep.append(i)
        if left > 0:
            raise SaveError(f"{self.name}: could not lower to {target:,}")
        drop = set(stacks) - set(keep)
        if drop:
            self._remove_positions(drop)
        if self.bank != target:
            raise SaveError(f"{self.name}: lowering check failed ({self.bank:,} != {target:,})")

    def compact_bank(self):
        """Swap 85k stacks for large-denomination items, keeping the balance identical.

        A save that already holds a large-denomination item can store the same money in
        about 1/17th of the item slots, which matters because the Items index is capped.
        Returns the number of item slots freed (0 if this save can't do it)."""
        big_parent = self._big_parent()
        if big_parent is None:
            return 0
        stacks = [i for i in self._positions(ID3_BANK_STACK) if self._in_bank(i)]
        if not stacks:
            return 0
        before_bank, before_items = self.bank, len(self.value)
        moved = sum(self.value[i] for i in stacks)
        # top up existing big items first, then append as few new ones as possible
        need = moved
        for i in self._positions(ID3_BIG_STACK):
            if need <= 0:
                break
            if not self._in_bank(i):
                continue
            room = BIG_MAX - self.value[i]
            if room > 0:
                add = min(room, need)
                self.value[i] += add
                need -= add
        used = set(self.id1)
        next_id = NEW_ID1_BASE
        while need > 0:
            amount = min(BIG_MAX, need)
            while next_id in used:
                next_id += 1
            pos = len(self.value)
            self.id1.append(next_id); used.add(next_id)
            self.type.append(big_parent)
            self.id2.append(ID2_ECONOMY)
            self.id3.append(ID3_BIG_STACK)
            self.value.append(amount)
            self.flag.append(False)
            self.index.append((pos << 4) | 1)
            need -= amount
        # the stacks are gone only once the money is safely held elsewhere
        self._remove_positions(set(stacks))
        ok, why = self.check_index()
        if not ok:
            raise SaveError(f"{self.name}: index check failed after compacting: {why}")
        if self.bank != before_bank:
            raise SaveError(f"{self.name}: compacting changed the balance ({before_bank:,} -> {self.bank:,})")
        return before_items - len(self.value)

    def remove_orphan_stacks(self):
        """Delete stacks with a wrong parent (the game never counts them) and re-number the Items index."""
        bad = set(self.orphan_stacks)
        if not bad:
            return 0
        self._remove_positions(bad)
        return len(bad)

    def _remove_positions(self, bad):
        """Delete money-table rows (never parents of other items) and re-number the Items index."""
        reg = set(self._register())
        if any(self.id1[i] in reg for i in bad):
            raise SaveError(f"{self.name}: an item to remove is a parent of other items; not removing")
        keep = [i for i in range(len(self.value)) if i not in bad]
        newpos = {old: new for new, old in enumerate(keep)}
        t = self.raw[MONEY_TABLE]; n = len(self.id1)
        cols = [_arr(t[col]) for col in t]
        if any(not isinstance(a, list) or len(a) != n for a in cols):
            raise SaveError(f"{self.name}: money table columns are uneven; not removing")
        for a in cols:
            a[:] = [a[i] for i in keep]
        out = []
        for e in self.index:
            tn, p = e & 15, e >> 4
            if tn == 1:
                if p in newpos:
                    out.append((newpos[p] << 4) | 1)
            else:
                out.append(e)
        self.index[:] = out
        self._pos_cache = None
        self._bank_account_computed = False
        self._bank_account_cache = None
        ok, why = self.check_index()
        if not ok:
            raise SaveError(f"{self.name}: index check failed after cleanup: {why}")

    def limits(self):
        return {"character": self.number, "name": self.name, "bank": self.bank, "cash": self.cash,
                "max_bank": self.max_bank(), "max_cash": CASH_MAX if self._positions(ID3_CASH) else 0,
                "items_used": self.items_used, "item_limit": SAFE_ITEM_LIMIT,
                "orphan_stacks": len(self.orphan_stacks), "has_bank_stack_slot": self._denomination()[0] is not None,
                "big_denomination": self._big_parent() is not None}

    def check_index(self):
        """The Items index must list every position of every table exactly once."""
        seen = {}
        for e in self.index:
            seen.setdefault(e & 15, []).append(e >> 4)
        for n, size in enumerate(self.table_sizes(), start=1):
            pos = sorted(seen.get(n, []))
            if pos != list(range(size)):
                return False, f"table {n}: index lists {len(pos)} positions, table has {size}"
        if len(self.index) > ITEM_LIMIT:
            return False, f"{len(self.index)} items exceeds the {ITEM_LIMIT} limit"
        return True, "ok"

    def set_bank(self, target):
        """Raise the bank balance to `target` using 85k stacks (and the Items index)."""
        current = self.bank
        if not isinstance(target, int) or isinstance(target, bool):
            raise SaveError("bank amount must be a whole number")
        if target < 0:
            raise SaveError("bank amount can't be negative")
        if target < current:
            return self._lower_bank(target)
        if target > current and self._denomination()[0] is None:
            raise SaveError(f"{self.name} has no bank stack yet, so there is nowhere safe to add money in this save. "
                            "The game creates one later (once the bank goes over its tier limit).")
        if target > self.max_bank():
            raise SaveError(f"{self.name}: {target:,} is over the safe limit for this save; "
                            f"the most allowed is {self.max_bank():,} "
                            f"({SAFE_ITEM_LIMIT} items, {SAFE_RESERVED_ITEMS} kept free)")
        need = target - current
        # 1) top up existing money items that are below their cap (big ones first)
        for id3, cap in ((ID3_BIG_STACK, BIG_MAX), (ID3_BANK_STACK, STACK_MAX)):
            for i in self._positions(id3):
                if need <= 0:
                    break
                if not self._in_bank(i):
                    continue
                room = cap - self.value[i]
                if room > 0:
                    add = min(room, need)
                    self.value[i] += add
                    need -= add
        # 2) append new money items at the end of the money table + index them
        new_type, new_cap, new_id3 = self._denomination()
        if new_type is None and need > 0:
            raise SaveError(f"{self.name}: this save has no bank stack yet, so there is nowhere safe to add money. "
                            "Play until the bank shows more than its tier limit once, or add a smaller amount.")
        used = set(self.id1)
        next_id = NEW_ID1_BASE
        added = 0
        while need > 0:
            amount = min(new_cap, need)
            while next_id in used:
                next_id += 1
            pos = len(self.value)
            self.id1.append(next_id); used.add(next_id)
            self.type.append(new_type)
            self.id2.append(ID2_ECONOMY)
            self.id3.append(new_id3)
            self.value.append(amount)
            self.flag.append(False)
            self.index.append((pos << 4) | 1)
            need -= amount
            added += 1
        ok, why = self.check_index()
        if not ok:
            raise SaveError(f"{self.name}: index check failed after edit: {why}")
        if self.bank != target:
            raise SaveError(f"{self.name}: internal error, balance is {self.bank:,} not {target:,}")
        return added

    def set_cash(self, amount):
        p = self._positions(ID3_CASH)
        if not p:
            raise SaveError(f"{self.name} has no cash item in this save (cash is 0). "
                            "Adding a new cash item is not supported by this tool.")
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise SaveError("cash amount must be a whole number")
        if amount < 0:
            raise SaveError("cash cannot be negative")
        if amount > CASH_MAX:
            raise SaveError(f"{self.name}: cash {amount:,} is over the limit of {CASH_MAX:,}")
        self.value[p[0]] = amount


class Save:
    """A decrypted GTA VI JSAV save (SARCxxxx or memory.dat)."""

    def __init__(self, data: bytes):
        if data[:4] == b"ASAV":
            raise SaveError("this file is in the game's packed 'ASAV' format, not decrypted JSAV. "
                            "Decrypt it again with GarlicSaves, or load and re-save it in-game first.")
        if data[:4] != b"JSAV":
            raise SaveError("not a GTA VI JSAV save (missing 'JSAV' header)")
        self.data = bytearray(data)
        _, self.version, n_code, n_script = struct.unpack_from("<4sIII", data, 0)
        self.records = [list(struct.unpack_from("<IIIII", data, 16 + 20 * i))
                        for i in range(n_code + n_script)]
        self.block_no, self.block = self._find_inventory_block()
        inv = self.block[K_ROOT][K_PARENT][K_INVENTORIES]
        self.inventories = {}
        for n, raw in enumerate(inv):
            if (isinstance(raw, dict) and MONEY_TABLE in raw and K_ITEMS_INDEX in raw
                    and isinstance(_arr(raw[K_ITEMS_INDEX]), list)
                    and isinstance(raw[MONEY_TABLE], dict) and _arr(raw[MONEY_TABLE].get(F_VALUE)) is not None):
                self.inventories[n] = Inventory(raw, n)
        if not self.inventories:
            raise SaveError("no character inventories found")

    def _find_inventory_block(self):
        for n, (_, _, _, off, size) in enumerate(self.records):
            raw = bytes(self.data[off:off + size])
            if b'"' + K_INVENTORIES.encode() + b'"' not in raw:
                continue
            try:
                obj = json.loads(raw)
                obj[K_ROOT][K_PARENT][K_INVENTORIES]
            except (ValueError, KeyError, TypeError):
                continue
            if _dumps(obj) != raw:
                raise SaveError(f"block {n} does not re-serialize identically; refusing to edit "
                                "(unknown save variant)")
            return n, obj
        raise SaveError("could not find the inventory block")

    def inventory(self, n):
        if n not in EDITABLE_CHARACTERS:
            raise SaveError(f"only {', '.join(f'{k} = {v}' for k, v in EDITABLE_CHARACTERS.items())} can be edited")
        if n not in self.inventories:
            raise SaveError(f"inventory {n} not present (have {sorted(self.inventories)})")
        return self.inventories[n]

    def to_bytes(self) -> bytes:
        off, old_size = self.records[self.block_no][3], self.records[self.block_no][4]
        new = _dumps(self.block)
        delta = len(new) - old_size
        out = bytearray(self.data[:off]) + new + self.data[off + old_size:]
        for i, rec in enumerate(self.records):
            r_off, r_size = rec[3], rec[4]
            if i == self.block_no:
                r_size += delta
            elif r_off > off:
                r_off += delta
            struct.pack_into("<IIIII", out, 16 + 20 * i, rec[0], rec[1], rec[2], r_off, r_size)
        # self-check: every block must still parse, everything else unchanged
        check = Save.__new__(Save)
        check.data = out
        n_code, n_script = struct.unpack_from("<II", out, 8)
        for i in range(n_code + n_script):
            _, _, _, o, s = struct.unpack_from("<IIIII", out, 16 + 20 * i)
            try:
                json.loads(bytes(out[o:o + s]))
            except ValueError:
                raise SaveError(f"self-check failed: block {i} no longer parses")
        return bytes(out)

    def limits(self):
        """Per-character current values and allowed maximums (for UIs)."""
        return [inv.limits() for n, inv in self.inventories.items() if n in EDITABLE_CHARACTERS]

    def summary(self):
        lines = []
        for n, inv in self.inventories.items():
            if n not in EDITABLE_CHARACTERS:
                continue
            ok, why = inv.check_index()
            lines.append(f"  {inv.name:12} bank {inv.bank:>14,}  (field {inv.bank_field:,} + "
                         f"{len(inv.stacks)} stacks)  cash {inv.cash:>9,}  items {inv.items_used}/{ITEM_LIMIT}"
                         f"  max bank {inv.max_bank():,}  index {'ok' if ok else 'BAD: ' + why}")
        return "\n".join(lines)


def _as_int(v, what):
    """Accept 66966966, "66,966,966", "$66,966,966" or "66_966_966"."""
    try:
        s = str(v).replace(",", "").replace("_", "").replace("$", "").strip()
        if not s.lstrip("-").isdigit():
            raise ValueError
        return int(s)
    except ValueError:
        raise SaveError(f"{what} must be a whole number, got {v!r}")


def limits(data: bytes):
    """Current values and allowed maximums per character, for a decrypted save."""
    _check_header(data)
    original = Save(data).limits()
    try:
        probe = Save(_prepare_shared_bank(data)).limits()[0]
        for key in ("max_bank", "has_bank_stack_slot", "big_denomination"):
            original[0][key] = probe[key]
    except SaveError as exc:
        original[0]["bank_unavailable_reason"] = str(exc)
    original[0]["shared_bank"] = True
    original[0]["cash_label"] = "Cash (Lucia)"
    return original



# ---------- bank ledger (block with $0x1e1e8a08): the game approves spending against its last balance ----------
K_LEDGER = '"$0x1e1e8a08":['
LEDGER_MAX_ENTRIES = 100


def _ledger_info(data):
    """(block index, list of (start,end) entry spans, block text) or None if the save has no ledger."""
    n_code, n_script = struct.unpack_from("<II", data, 8)
    for i in range(n_code + n_script):
        _, _, _, o, sz = struct.unpack_from("<IIIII", data, 16 + 20 * i)
        raw = bytes(data[o:o + sz])
        if K_LEDGER.encode() not in raw:
            continue
        text = raw.decode("utf-8"); k = text.index(K_LEDGER) + len(K_LEDGER)
        spans, depth, start, j, instr = [], 0, None, k, False
        while True:
            ch = text[j]
            if instr:
                if ch == "\\": j += 2; continue
                if ch == '"': instr = False
            elif ch == '"': instr = True
            elif ch == "{":
                if depth == 0: start = j
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0: spans.append((start, j + 1))
            elif ch == "]" and depth == 0:
                return i, spans, text, j
            j += 1
    return None


def ledger_balance(data):
    info = _ledger_info(data)
    if not info or not info[1]:
        return None
    i, spans, text, _ = info
    return json.loads(text[spans[-1][0]:spans[-1][1]])["$0x818f6f6d"]


def sync_ledger(data: bytes, new_balance: int) -> bytes:
    """Append one deposit/withdrawal so the ledger's last balance equals `new_balance` (Lucia's bank).
    Copies the game's own entry layout, next sequence number, keeps the newest 100 entries."""
    info = _ledger_info(data)
    if not info or not info[1]:
        raise SaveError("this save has no bank ledger; can't keep spending in step")
    bi, spans, text, end = info
    last = json.loads(text[spans[-1][0]:spans[-1][1]])
    old_bal = last["$0x818f6f6d"]
    if old_bal == new_balance:
        return bytes(data)
    if not (0 <= new_balance < 2 ** 31):
        raise SaveError("ledger balance must be between 0 and 2,147,483,647")
    seq = max(json.loads(text[a:b])["$0x9d8e7980"] for a, b in spans)
    entry = ('{"$0x9d8e7980":%d,"$0x6d0bd92f":3221225474,"$0xf0e40798":{"$data":"0x00000000"},'
             '"$0x37f7ebed":{"$data":"0x00000000"},"$0xd876c338":{"$data":"0x00000000"},'
             '"$0xcfcdc72c":0,"$0x706a1287":0,"$0xa662d8be":%d,"$0x818f6f6d":%d}') % (seq + 1, new_balance - old_bal, new_balance)
    if list(json.loads(entry)) != list(last):
        raise SaveError("ledger entries in this save have an unknown layout; not editing")
    first = 1 if len(spans) >= LEDGER_MAX_ENTRIES else 0
    body = text[spans[first][0]:spans[-1][1]] + "," + entry
    new_text = text[:spans[0][0]] + body + text[end:]
    json.loads(new_text)
    nb = new_text.encode("utf-8")
    _, _, _, off, size = struct.unpack_from("<IIIII", data, 16 + 20 * bi)
    delta = len(nb) - size
    out = bytearray(bytes(data[:off]) + nb + bytes(data[off + size:]))
    n_code, n_script = struct.unpack_from("<II", data, 8)
    for i in range(n_code + n_script):
        a, b_, c, o, sz = struct.unpack_from("<IIIII", data, 16 + 20 * i)
        if i == bi: sz += delta
        elif o > off: o += delta
        struct.pack_into("<IIIII", out, 16 + 20 * i, a, b_, c, o, sz)
    _check_header(bytes(out))
    if ledger_balance(bytes(out)) != new_balance:
        raise SaveError("ledger check failed")
    return bytes(out)


def _check_header(data: bytes):
    """Refuse saves whose header no longer matches their contents (damaged by another tool)."""
    n_code, n_script = struct.unpack_from("<II", data, 8)
    bad = []
    for i in range(n_code + n_script):
        _, _, _, o, sz = struct.unpack_from("<IIIII", data, 16 + 20 * i)
        try:
            json.loads(bytes(data[o:o + sz]))
        except ValueError:
            bad.append(i)
    if bad:
        raise SaveError(f"this save is already damaged: {len(bad)} block(s) don't match the save's header "
                        f"(first: block {bad[0]}). Another tool probably edited it. Start from an untouched save.")


def _prepare_shared_bank(data):
    """Activate only observed, existing empty bank slots; never invent an account."""
    s = Save(data)
    inv = s.inventory(0)
    if inv._denomination()[0] is not None:
        return bytes(data)
    fields = {F_ID1, F_TYPE, F_ID2, F_ID3, F_VALUE, F_FLAG}
    if set(inv.raw[MONEY_TABLE]) != fields:
        raise SaveError("Unsupported shared bank table layout")
    accounts = [i for i, x in enumerate(inv.id2)
                if x == BANK_ACCOUNT_ID2 and inv.id3[i] == 3359303526]
    if len(accounts) != 1:
        raise SaveError("This save needs one existing shared bank account; no account was created")
    account = accounts[0]
    def slot(spec):
        found = [i for i, x in enumerate(inv.id2) if x == spec
                 and inv.id3[i] == 1490883802 and inv._parent_pos(i) == account]
        if len(found) != 1:
            raise SaveError("Missing or ambiguous native shared bank slot; no slot was created")
        return found[0]
    overflow = slot(864635288)
    base = slot(642088078)
    used = set()
    for raw in s.block[K_ROOT][K_PARENT][K_INVENTORIES]:
        if isinstance(raw, dict):
            for t in TABLES:
                if isinstance(raw.get(t), dict):
                    used.update(_arr(raw[t].get(F_ID1)) or [])
    next_id = max([x for x in used if x >= 0] + [8_600_000_000_000_000_000]) + 1
    reg = inv._register()
    def add(parent, category):
        nonlocal next_id
        if len(inv.index) >= SAFE_ITEM_LIMIT or next_id >= 2**63:
            raise SaveError("Not enough safe inventory capacity to activate the shared bank")
        item = inv.id1[parent]
        if item not in reg:
            reg.append(item)
        values = {F_ID1: next_id, F_TYPE: reg.index(item), F_ID2: ID2_ECONOMY,
                  F_ID3: category, F_VALUE: 0, F_FLAG: False}
        pos = len(inv.id1)
        for key, col in inv.raw[MONEY_TABLE].items():
            _arr(col).append(values[key])
        inv.index.append((pos << 4) | 1)
        next_id += 1
    if not inv._positions(ID3_BANK):
        add(base, ID3_BANK)
    add(overflow, ID3_BANK_STACK)
    ok, why = inv.check_index()
    if not ok:
        raise SaveError("Bank activation index check failed: " + why)
    return s.to_bytes()


MONEY_PRESETS = (("10 Million", 10_000_000), ("20 Million", 20_000_000),
                 ("50 Million", 50_000_000), ("Max Money", "max"))


def money_presets(data):
    """Button descriptors; disabled amounts never silently clamp to another amount."""
    info = limits(data)[0]
    return [{"label": label, "value": value,
             "enabled": info["has_bank_stack_slot"] and
                 (value == "max" or value <= info["max_bank"])}
            for label, value in MONEY_PRESETS]


def edit_preset_bytes(data, preset):
    """Apply a supported shared-bank button; leave both characters' cash alone."""
    values = {str(value): value for _, value in MONEY_PRESETS}
    if str(preset) not in values:
        raise SaveError("Choose 10 Million, 20 Million, 50 Million or Max Money")
    return edit_bytes(data, bank=values[str(preset)])


def edit_bytes(data: bytes, bank=None, cash=None, character=0) -> bytes:
    """One-call API for integrations: returns the EDITED save bytes, already verified.

    Raises SaveError if nothing was requested, if the edit can't be applied, or if
    re-reading the output doesn't show the requested values. Encrypt the bytes this
    returns, not the original input.
    """
    if bank is None and cash is None:
        raise SaveError("no edit requested (pass bank and/or cash)")
    character = _as_int(character, "character")
    if character != 0:
        raise SaveError("The bank is shared. Use character=0; there is no separate Jason bank")
    _check_header(data)
    original_data = bytes(data)
    if bank is not None:
        # This path reuses native empty slots found in both controlled input saves.
        data = _prepare_shared_bank(data)
    # bank="max": use this save's own safe maximum (what a "Max money" button should call).
    if isinstance(bank, str) and bank.strip().lower() == "max":
        _check_header(data)
        info = limits(data)[character]
        if not info["has_bank_stack_slot"]:
            raise SaveError(f"{info['name']} has no bank stack yet, so there is nowhere safe to add money in this save.")
        if info.get("big_denomination"):
            probe = Save(data).inventory(character)
            probe.compact_bank()
            info = dict(info, max_bank=probe.max_bank())
        bank = info["max_bank"] // 1_000_000 * 1_000_000   # round down to a whole million
        if bank <= info["bank"]:
            bank = info["bank"]  # Max never lowers money or changes on a second click
    if isinstance(cash, str) and cash.strip().lower() == "max":
        cash = CASH_MAX
    bank = None if bank is None else _as_int(bank, "bank")
    cash = None if cash is None else _as_int(cash, "cash")
    character = _as_int(character, "character")
    _check_header(data)
    s = Save(data)
    inv = s.inventory(character)
    for c in EDITABLE_CHARACTERS:       # dead stacks from older editors: never counted, just take up room
        try:
            s.inventory(c).remove_orphan_stacks()
        except SaveError:
            raise
        except Exception:
            pass
    if bank is not None and bank != inv.bank:
        # Large-denomination items hold ~17x an 85k stack. If the target needs more item
        # slots than are left, swap existing stacks for them first - same balance, far fewer items.
        if bank > inv.bank and inv._big_parent() is not None and bank > inv.max_bank():
            inv.compact_bank()
        inv.set_bank(bank)
    if cash is not None:
        inv.set_cash(cash)
    out = s.to_bytes()
    # keep the bank ledger in step (Lucia's bank): the game only lets you spend what the ledger says
    if bank is not None and character == 0 and ledger_balance(out) not in (None, bank):
        out = sync_ledger(out, bank)
    check = Save(out).inventory(character)
    if bank is not None and check.bank != int(bank):
        raise SaveError(f"verification failed: bank is {check.bank:,}, expected {int(bank):,}")
    if cash is not None and check.cash != int(cash):
        raise SaveError(f"verification failed: cash is {check.cash:,}, expected {int(cash):,}")
    ok, why = check.check_index()
    if not ok:
        raise SaveError(f"verification failed: {why}")
    if out == bytes(data):
        # The requested balance already exists. A verified no-op is valid.
        return bytes(data)
    return out


def edit_zip(zip_bytes: bytes, bank=None, cash=None, character=0) -> bytes:
    """Same as edit_bytes() for a GarlicSaves decrypt zip; returns the edited zip bytes.
    Every decrypted save inside (e.g. SARCxxxx and memory.dat) is edited."""
    zin = zipfile.ZipFile(io.BytesIO(zip_bytes))
    buf = io.BytesIO()
    edited = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if _is_save_member(info.filename, data) and data[:4] == b"JSAV":
                data = edit_bytes(data, bank, cash, character)
                edited += 1
            zout.writestr(info, data)
    if not edited:
        raise SaveError("no decrypted JSAV save found inside the zip")
    return buf.getvalue()


# --- file / zip helpers -----------------------------------------------------
def _is_save_member(name, data):
    base = name.rsplit("/", 1)[-1]
    return "/sce_sys/" not in f"/{name}" and not base.startswith("chives.") and data[:4] in (b"JSAV", b"ASAV")


def load(path):
    """Return list of (member_name or None, Save) for a file or decrypted workspace zip."""
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:2] == b"PK":
        out = []
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                data = z.read(name)
                if _is_save_member(name, data):
                    out.append((name, Save(data)))
        if not out:
            raise SaveError("no decrypted save file found inside the zip")
        return out
    with open(path, "rb") as f:
        return [(None, Save(f.read()))]


def write(path_in, path_out, edited):
    """edited: dict member_name(or None) -> new bytes."""
    if None in edited:
        with open(path_out, "wb") as f:
            f.write(edited[None])
        return
    with zipfile.ZipFile(path_in) as zin, zipfile.ZipFile(path_out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = edited.get(info.filename, zin.read(info.filename))
            zout.writestr(info, data)


def _parse_amount(s):
    """CLI amounts: a number (commas/$ allowed) or the word "max"."""
    if str(s).strip().lower() == "max":
        return "max"
    return int(str(s).replace(",", "").replace("_", "").replace("$", ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Set the bank balance in a decrypted GTA VI PS5 story save.")
    ap.add_argument("input", help="decrypted save file (SARCxxxx / memory.dat) or GarlicSaves decrypt .zip")
    ap.add_argument("--bank", type=_parse_amount, help="new bank balance, e.g. 66966966, 66,966,966, or max (this save's safe maximum)")
    ap.add_argument("--cash", type=_parse_amount, help="new cash on hand (only if the character already has cash)")
    ap.add_argument("--character", type=int, default=0, help="inventory to edit: 0 = Lucia (default), 1 = Jason")
    ap.add_argument("-o", "--output", help="output path (default: <input>.edited[.zip])")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)
    try:
        saves = load(a.input)
        for name, s in saves:
            print(f"{name or os.path.basename(a.input)}:\n{s.summary()}")
        if a.bank is None and a.cash is None:
            return 0
        edited = {}
        for name, s in saves:
            # Same path as integrations: dead-stack cleanup, raise/lower, ledger kept in step, verified.
            edited[name] = edit_bytes(bytes(s.data), bank=a.bank, cash=a.cash, character=a.character)
            inv = Save(edited[name]).inventory(a.character)
            print(f"-> {inv.name}: bank {inv.bank:,}, cash {inv.cash:,}, ledger {ledger_balance(edited[name])}")
        root, ext = os.path.splitext(a.input)
        out = a.output or (root + ".edited" + (ext if ext.lower() == ".zip" else ""))
        write(a.input, out, edited)
        for name, data in edited.items():
            print(f"verify {name or 'file'}:\n{Save(data).summary()}")
        print(f"written: {out}")
        return 0
    except (SaveError, OSError, zipfile.BadZipFile) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())


def clean_bytes(data: bytes) -> bytes:
    """Remove dead bank stacks (wrong parent, never counted by the game) from every character."""
    s = Save(data); n = 0
    for c in EDITABLE_CHARACTERS:
        try:
            n += s.inventory(c).remove_orphan_stacks()
        except SaveError:
            raise
        except Exception:
            continue
    return s.to_bytes() if n else bytes(data)
