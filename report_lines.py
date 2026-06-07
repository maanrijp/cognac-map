#!/usr/bin/env python3
"""Diagnose: hoe wordt elke split-gemeente afgehandeld, en wat doen de lijnen?

Draai naast build_cognac_map.py:   python report_lines.py

Per split-gemeente (gemeente die in >=2 crus ligt) toont het:
  - welke aangeleverde lijn(en) de gemeente RAKEN (index in je splitlines-bestand);
  - de gekozen methode: déléguée-data / lijn-split / 'lijn raakt maar snijdt NIET
    door' / heel naar hoofd-cru.
Zo zie je meteen of een rivierlijn net niet door een gemeente heen loopt (dan even
doortrekken voorbij de gemeentegrens).
"""

from __future__ import annotations

from shapely.geometry import shape

import build_cognac_map as bcm


def main() -> None:
    mapping = bcm.load_json(bcm.MAPPING_PATH)
    membership = bcm.membership_from_mapping(mapping)
    lines = bcm.load_split_lines()
    communes = bcm.download_communes(bcm.DEPARTEMENTEN)

    print(f"\nAantal lijnen: {len(lines)}  (index 0 = eerste lijn, 1 = tweede, ...)\n" + "=" * 78)
    for code, crus in sorted(membership.items()):
        if len(crus) < 2:
            continue
        feat = communes.get(code)
        if not feat:
            continue
        geom = bcm._clean(shape(feat["geometry"]))
        nom = feat["properties"].get("nom", "?")
        raakt = [i for i, ln in enumerate(lines) if ln.intersects(geom)]
        faces = bcm._split_by_lines(geom, lines) if lines else None
        if code in bcm.DELEGUEE_SPLITS:
            methode = "déléguée-data (officiële deelgemeenten)"
        elif faces:
            methode = f"LIJN-SPLIT ✓ ({len(faces)} delen)"
        elif raakt:
            methode = "lijn raakt maar snijdt NIET door  → lijn doortrekken!"
        else:
            methode = "heel naar hoofd-cru"
        print(f"{nom:30s} {code}  crus={sorted(crus)}")
        print(f"    lijn-raakt={raakt}   methode: {methode}")
    print("=" * 78)


if __name__ == "__main__":
    main()
