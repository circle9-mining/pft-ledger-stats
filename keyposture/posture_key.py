"""Signing-key posture model shared by the keyposture tools (Python 3 stdlib only).

An account's posture is the tuple (master enabled?, RegularKey, SignerList) read from its
AccountRoot (lsfDisableMaster flag, RegularKey field) and its SignerList ledger object.
SignerList objects carry no Account field, so they are matched to their owner by keylet:
index = SHA-512Half(0x0053 || AccountID || uint32 0). The same code computes AccountRoot
keylets (0x0061 || AccountID), which `reconcile_key.py` checks against every snapshot index
as a self-test of the address decoding.
"""

import hashlib

B58 = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
LSF_DISABLE_MASTER = 0x00100000
# well-known addresses nobody holds a key for (AccountID 0, 1 and the "black hole" names)
BLACKHOLES = {
    "rrrrrrrrrrrrrrrrrrrrrhoLvTp",  # ACCOUNT_ZERO
    "rrrrrrrrrrrrrrrrrrrrBZbvji",  # ACCOUNT_ONE
    "rrrrrrrrrrrrrrrrrNAMEtxvNvQ",
    "rrrrrrrrrrrrrrrrrrrn5RM1rHd",  # NaN address
}

CLASSES = [
    "master_only",
    "master+regular",
    "master+multisig",
    "master+regular+multisig",
    "regular_only",
    "multisig_only",
    "regular+multisig",
    "blackholed",
    "no_usable_key",
]


def decode_address(addr):
    """20-byte AccountID of a classic address; ValueError on bad alphabet/length/checksum."""
    n = 0
    for ch in addr:
        i = B58.find(ch)
        if i < 0:
            raise ValueError(f"bad base58 character in {addr!r}")
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\x00" * (len(addr) - len(addr.lstrip(B58[0]))) + raw
    if len(raw) != 25 or raw[0] != 0:
        raise ValueError(f"not a classic account address: {addr!r}")
    if hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4] != raw[21:]:
        raise ValueError(f"bad checksum: {addr!r}")
    return raw[1:21]


def _half(data):
    return hashlib.sha512(data).digest()[:32].hex().upper()


def account_keylet(addr):
    return _half(b"\x00\x61" + decode_address(addr))


def signerlist_keylet(addr):
    return _half(b"\x00\x53" + decode_address(addr) + b"\x00\x00\x00\x00")


def signer_summary(sl):
    """Normalised SignerList: quorum, sorted (account, weight) entries."""
    if not sl:
        return None
    entries = sorted(
        (e["SignerEntry"]["Account"], int(e["SignerEntry"]["SignerWeight"]))
        for e in sl.get("SignerEntries", [])
    )
    return {"quorum": int(sl["SignerQuorum"]), "entries": entries}


def posture(acct, signer_list=None):
    """Posture tuple of an AccountRoot (dict) and its SignerList object (dict or None)."""
    return {
        "master": not (int(acct.get("Flags", 0)) & LSF_DISABLE_MASTER),
        "regular_key": acct.get("RegularKey"),
        "signers": signer_summary(signer_list),
    }


def classify(p):
    """Class name of a posture tuple (see CLASSES)."""
    rk, ms = p["regular_key"], p["signers"]
    if p["master"]:
        if rk and ms:
            return "master+regular+multisig"
        if rk:
            return "master+regular"
        if ms:
            return "master+multisig"
        return "master_only"
    usable_rk = bool(rk) and rk not in BLACKHOLES
    if usable_rk and ms:
        return "regular+multisig"
    if usable_rk:
        return "regular_only"
    if ms:
        return "multisig_only"
    if rk:
        return "blackholed"
    return "no_usable_key"


def weaknesses(account, p):
    """Weak-configuration flags for one account (empty list = none)."""
    w = []
    rk, ms = p["regular_key"], p["signers"]
    if p["master"] and (rk or ms):
        w.append("master_still_enabled")  # an extra key path that can sign alone
    if ms:
        weights = [x for _, x in ms["entries"]]
        if weights and max(weights) >= ms["quorum"]:
            w.append("single_signer_meets_quorum")
        if sum(weights) < ms["quorum"]:
            w.append("quorum_unreachable")
        if len(ms["entries"]) == 1:
            w.append("one_signer_entry")
        if any(a == account for a, _ in ms["entries"]):
            w.append("self_in_signer_list")
        if any(a == rk for a, _ in ms["entries"]) and rk:
            w.append("regular_key_in_signer_list")
    if rk and rk == account:
        w.append("regular_key_is_self")
    if not p["master"] and not ms and (not rk or rk in BLACKHOLES):
        w.append("no_usable_key" if not rk else "blackholed")
    return w
