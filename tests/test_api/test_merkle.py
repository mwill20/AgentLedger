"""Unit tests for Layer 3 Merkle helpers."""

from api.services import merkle


def test_build_tree_returns_proofs_for_each_leaf():
    leaves = [
        "0x" + ("11" * 32),
        "0x" + ("22" * 32),
        "0x" + ("33" * 32),
    ]

    tree = merkle.build_tree(leaves)

    assert tree["root"].startswith("0x")
    assert len(tree["proofs"]) == len(leaves)


def test_verify_proof_accepts_valid_leaf():
    leaves = [
        "0x" + ("11" * 32),
        "0x" + ("22" * 32),
        "0x" + ("33" * 32),
        "0x" + ("44" * 32),
    ]

    tree = merkle.build_tree(leaves)

    assert merkle.verify_proof(leaves[2], tree["proofs"][2], tree["root"]) is True


def test_verify_proof_rejects_wrong_leaf():
    leaves = [
        "0x" + ("11" * 32),
        "0x" + ("22" * 32),
    ]

    tree = merkle.build_tree(leaves)

    assert merkle.verify_proof("0x" + ("ff" * 32), tree["proofs"][0], tree["root"]) is False
