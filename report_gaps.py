#!/usr/bin/env python3
"""Diagnose: welke interne gaten zijn opgevuld, met welke gemeente(n) en cru?

Draai dit NAAST build_cognac_map.py (zelfde map). Het hergebruikt dat script om
de actuele commune-geometrie op te halen, berekent de interne gaten net als de
echte bouw, en print per gat:
  - de toegewezen cru (= de kleur waarmee het gevuld wordt);
  - de gemeente(n) die in het gat liggen (naam + INSEE);
  - of die gemeente al in de mapping staat, en zo ja onder welke cru.

Zo zie je meteen of een gat de juiste kleur kreeg, of dat er een gemeente bij zit
die eigenlijk niet in de AOC hoort (dan zou je hem beter niet inkleuren).

    python report_gaps.py
"""

from __future__ import annotations

from shapely.geometry import shape
from shapely.ops import unary_union

import build_cognac_map as bcm


def main() -> None:
    mapping = bcm.load_json(bcm.MAPPING_PATH)

    # INSEE -> cru zoals in de mapping (incl. naam, voor de rapportage).
    code_to_naam_cru: dict[str, tuple[str, str]] = {}
    for cru, arr in mapping["crus"].items():
        for item in arr:
            code_to_naam_cru[str(item["insee"])] = (item.get("nom", "?"), cru)

    insee_to_cru = bcm.build_insee_to_cru(mapping)
    communes = bcm.download_communes(bcm.DEPARTEMENTEN)

    # Cru-vlakken (zonder gaten-vulling), om de gaten te kunnen vinden.
    cru_geoms = bcm.build_cru_geometries(insee_to_cru, communes)

    union_all = bcm._clean(unary_union(list(cru_geoms.values())))
    filled = bcm._drop_interior_rings(union_all)
    gaps = bcm._clean(filled.difference(union_all))
    gap_polys = list(gaps.geoms) if gaps.geom_type == "MultiPolygon" else ([gaps] if not gaps.is_empty else [])

    print(f"\nAantal interne gaten: {len(gap_polys)}\n" + "=" * 70)

    # Geometrie per commune (voor het bepalen welke gemeente in een gat ligt).
    geom_by_code = {code: bcm._clean(shape(f["geometry"])) for code, f in communes.items()}

    for i, gap in enumerate(sorted(gap_polys, key=lambda g: -g.area), start=1):
        # Toewijzing exact zoals fill_internal_gaps: cru met grootste overlap.
        opgeblazen = gap.buffer(0.0006)
        scores = {c: opgeblazen.intersection(g).area for c, g in cru_geoms.items()}
        toegewezen = max(scores, key=scores.get)

        # Welke gemeente(n) liggen in dit gat?
        inside = []
        for code, g in geom_by_code.items():
            if g.intersection(gap).area > 0.4 * g.area:
                naam = communes[code]["properties"].get("nom", "?")
                in_map = code_to_naam_cru.get(code)
                vlag = f"in mapping als {in_map[1]}" if in_map else "NIET in mapping"
                inside.append(f"{naam} ({code}, {vlag})")

        print(f"\nGat #{i}  ~{gap.area*12300:.1f} km²  -> gevuld als: {toegewezen}")
        if inside:
            for s in inside:
                print(f"    bevat: {s}")
        else:
            print("    (geen hele gemeente; waarschijnlijk een naad tussen gemeenten)")

    print("\n" + "=" * 70)
    print("Tip: staat er een gemeente 'NIET in mapping' die je herkent als wél/"
          "niet-cognac, geef die door — dan corrigeren we de toewijzing of "
          "sluiten we 'm uit.")


if __name__ == "__main__":
    main()
