#!/usr/bin/env python3
"""
Atlas API key generator.

Produces a word-seed key (default 5 words, hyphen-joined) like
    apple-pie-red-car-purple
prints it ONCE, and emits the argon2id hash to paste into atlas.yaml.

Usage:
    ./atlas-keygen.py --name perseus-main
    ./atlas-keygen.py --name perseus-main --words 4
    ./atlas-keygen.py --wordlist /path/to/eff_large_wordlist.txt

Entropy: with the EFF large list (7776 words), 5 words ≈ 64.6 bits.
With the small built-in list (256 words), 5 words = 40 bits — fine for a
LAN-only service, but use the EFF list for anything exposed further.

Requires: pip install argon2-cffi
"""
import argparse
import secrets
import sys
from pathlib import Path

try:
    from argon2 import PasswordHasher
    from argon2.low_level import Type
except ImportError:
    print("pip install argon2-cffi", file=sys.stderr)
    sys.exit(1)

# Small fallback list; replace with EFF large wordlist for real deployments.
BUILTIN = """
apple amber anchor arrow autumn badge bamboo basket beacon berry birch blade
blossom bolt bottle breeze brick bridge bronze brook butter cabin cactus candle
canyon carbon castle cedar chalk cherry cider cliff cloud clover cobalt comet
copper coral cotton crane crystal cypress daisy dawn delta desert dune eagle
ember engine falcon feather fern fiddle flame flint forest fossil frost galaxy
garden garnet geyser ginger glacier globe granite gravel harbor harvest hazel
helmet hollow honey horizon icicle indigo iron island ivory jade jasper jungle
kayak kernel kettle lagoon lantern lava lemon lilac linen lotus magnet maple
marble meadow meteor mint mirror moss mustard nectar nickel noodle north oak
olive onyx orbit orchid otter oyster paddle panda paper pearl pepper pine
planet plum pocket prism pumpkin quartz quill rabbit raven reef ribbon ridge
river robin rocket rose ruby saddle salmon sand sapphire satin scarf shadow
shell silver sketch slate smoke snow sparrow spice spiral spruce stone storm
summit sunset tangerine temple thistle thunder tiger timber topaz torch tulip
tundra turtle umber valley velvet violet walnut walrus water willow wind
winter wolf yarn zebra zephyr zinc red blue green purple orange yellow black
white gray gold car tree pie bird fish star moon sun sky sea hill road gate
""".split()


def load_wordlist(path: str | None) -> list[str]:
    if not path:
        return BUILTIN
    words = []
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        words.append(parts[-1])  # EFF format is "11111<TAB>word"; plain lists also work
    return words


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="key name as it appears in atlas.yaml")
    ap.add_argument("--words", type=int, default=5, choices=range(3, 6), help="word count (max 5)")
    ap.add_argument("--wordlist", help="path to a wordlist (EFF large recommended)")
    args = ap.parse_args()

    words = load_wordlist(args.wordlist)
    key = "-".join(secrets.choice(words) for _ in range(args.words))

    ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, type=Type.ID)
    digest = ph.hash(key)

    print("\n=== Atlas API key (shown once, store it in Perseus now) ===\n")
    print(f"  {key}\n")
    print("=== Paste into atlas.yaml under auth.keys ===\n")
    print(f"  - name: {args.name}")
    print(f'    hash: "{digest}"')
    print("    revoked: false")
    print("    rate_limit_per_min: 120\n")


if __name__ == "__main__":
    main()
