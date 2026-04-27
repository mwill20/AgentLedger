"""Merkle tree helpers for Layer 3 audit batch anchoring."""

from __future__ import annotations

from hashlib import sha3_256


ZERO_HASH = "0x" + ("0" * 64)


def _strip_0x(value: str) -> str:
    return value[2:] if value.startswith("0x") else value


def _hash_pair(left: str, right: str) -> str:
    """Hash one ordered pair of hex digests into a new digest."""
    payload = bytes.fromhex(_strip_0x(left)) + bytes.fromhex(_strip_0x(right))
    return "0x" + sha3_256(payload).hexdigest()


def build_tree(leaves: list[str]) -> dict[str, object]:
    """Build a Merkle root and proofs for a sequence of leaf digests."""
    if not leaves:
        return {"root": ZERO_HASH, "proofs": []}

    levels: list[list[str]] = [list(leaves)]
    while len(levels[-1]) > 1:
        current = levels[-1]
        parent_level: list[str] = []
        for index in range(0, len(current), 2):
            left = current[index]
            right = current[index + 1] if index + 1 < len(current) else current[index]
            parent_level.append(_hash_pair(left, right))
        levels.append(parent_level)

    proofs: list[list[dict[str, str]]] = []
    for leaf_index in range(len(leaves)):
        proof: list[dict[str, str]] = []
        index = leaf_index
        for level in levels[:-1]:
            sibling_index = index + 1 if index % 2 == 0 else index - 1
            if sibling_index >= len(level):
                sibling_index = index
            position = "right" if sibling_index >= index else "left"
            proof.append({"position": position, "hash": level[sibling_index]})
            index //= 2
        proofs.append(proof)

    return {"root": levels[-1][0], "proofs": proofs}


def verify_proof(leaf_hash: str, proof: list[dict[str, str]], root_hash: str) -> bool:
    """Verify one Merkle inclusion proof."""
    current = leaf_hash
    for step in proof:
        sibling_hash = step["hash"]
        if step["position"] == "left":
            current = _hash_pair(sibling_hash, current)
        else:
            current = _hash_pair(current, sibling_hash)
    return current == root_hash
