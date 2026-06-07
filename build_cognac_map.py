#!/usr/bin/env python3
"""Bouw de Cognac-crus kaartlaag voor Obsidian Bases (Maps-plugin).

VERSIE 3.2 — wijzigingen:
* v2: actuele grenzen via `geo.api.gouv.fr` (communes nouvelles).
* v3.1: interne gaten (witte vlekken) op UNIE-niveau opvullen, incl. gaten op de
  grens tussen twee crus.
* v3.2: kustlijn-clipping STANDAARD UIT (zoals de eerste versie). De officiële
  commune-grenzen blijven volledig behouden; coastline-clip met de grove
  Natural Earth 10m-bron sneed kustgemeenten verkeerd af. Optioneel weer aan met
  `--clip` (alleen zinvol met een fijne kustbron; zie LAND_GEOJSON_URL).

Produceert:
  1. cognac-crus.geojson            -> 6 features (één per cru), property `cru`
  2. cognac-crus-style-light.json   -> OpenFreeMap 'bright' + cru-fill
  3. cognac-crus-style-dark.json    -> OpenFreeMap 'dark'   + cru-fill

AFHANKELIJKHEDEN: Python 3.9+, `requests`, `shapely`.
    pip install requests shapely

GEBRUIK:
    python build_cognac_map.py                 # alles (gaten vullen, GEEN kust-clip)
    python build_cognac_map.py --no-fill       # interne gaten NIET vullen
    python build_cognac_map.py --clip          # WEL op de kustlijn knippen
    python build_cognac_map.py --verify        # check tegen producers_points.json
    python build_cognac_map.py --skip-styles   # alleen de geojson
    python build_cognac_map.py --inline-geojson  # geojson in de styles embedden
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterable

import requests
from shapely.geometry import MultiPolygon, Point, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

# --------------------------------------------------------------------------- #
# Configuratie                                                                 #
# --------------------------------------------------------------------------- #

HERE: Path = Path(__file__).resolve().parent

MAPPING_PATH: Path = HERE / "cognac_cru_communes.json"
PRODUCERS_PATH: Path = HERE / "producers_points.json"  # alleen voor --verify

OUTPUT_GEOJSON: Path = HERE / "cognac-crus.geojson"
OUTPUT_STYLE_LIGHT: Path = HERE / "cognac-crus-style-light.json"
OUTPUT_STYLE_DARK: Path = HERE / "cognac-crus-style-dark.json"

GEOJSON_PUBLIC_URL: str = "https://raw.githubusercontent.com/maanrijp/cognac-map/main/cognac-crus.geojson"

FILL_OPACITY: float = 0.45
SIMPLIFY_TOLERANCE_DEG: float = 0.0006   # ~60 m
COORD_PRECISION: int = 5
HTTP_TIMEOUT: int = 120

DEPARTEMENTEN: list[str] = ["16", "17", "24", "79"]

GEO_API_COMMUNES: str = (
    "https://geo.api.gouv.fr/communes"
    "?codeDepartement={dep}&fields=code,nom&format=geojson&geometry=contour"
)

# Alleen gebruikt met --clip. Natural Earth 10m is grof; vervang door een fijne
# kustbron (bv. OSM water/land-polygons) als je netjes op de waterrand wilt knippen.
LAND_GEOJSON_URL: str = (
    "https://raw.githubusercontent.com/martynafford/natural-earth-geojson/"
    "master/10m/physical/ne_10m_land.json"
)

OFM_STYLE_URLS: dict[str, str] = {
    "light": "https://tiles.openfreemap.org/styles/bright",
    "dark": "https://tiles.openfreemap.org/styles/dark",
}

CRU_COLORS: dict[str, str] = {
    "Grande Champagne": "rgb(230, 73, 41)",
    "Petite Champagne": "rgb(237, 120, 55)",
    "Borderies": "rgb(249, 171, 22)",
    "Fins Bois": "rgb(253, 222, 64)",
    "Bons Bois": "rgb(167, 208, 99)",
    "Bois Ordinaires": "rgb(115, 175, 76)",
}

CRU_PRIORITY: list[str] = [
    "Grande Champagne",
    "Petite Champagne",
    "Borderies",
    "Fins Bois",
    "Bons Bois",
    "Bois Ordinaires",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cognac-map")


# --------------------------------------------------------------------------- #
# Inlezen & toewijzing                                                         #
# --------------------------------------------------------------------------- #

def load_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"Bestand niet gevonden: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Ongeldige JSON in {path}: {exc}") from exc


def build_insee_to_cru(mapping: dict[str, Any]) -> dict[str, str]:
    """INSEE -> cru (eenduidig via CRU_PRIORITY); split-gemeenten worden gelogd."""
    crus: dict[str, list[dict[str, Any]]] = mapping["crus"]
    onbekend = [c for c in crus if c not in CRU_PRIORITY]
    if onbekend:
        raise ValueError(f"Onbekende cru-namen (niet in CRU_PRIORITY): {onbekend}")
    rang = {cru: i for i, cru in enumerate(CRU_PRIORITY)}
    toewijzing: dict[str, str] = {}
    multi: dict[str, set[str]] = {}
    for cru, communes in crus.items():
        for item in communes:
            insee = str(item["insee"]).strip()
            multi.setdefault(insee, set()).add(cru)
            if insee not in toewijzing or rang[cru] < rang[toewijzing[insee]]:
                toewijzing[insee] = cru
    gesplitst = {i: cs for i, cs in multi.items() if len(cs) > 1}
    if gesplitst:
        log.info("Gemeenten in meerdere crus (toegekend via prioriteit): %d", len(gesplitst))
    return toewijzing


# --------------------------------------------------------------------------- #
# Geometrie ophalen                                                            #
# --------------------------------------------------------------------------- #

def _get_json(url: str) -> Any:
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Download mislukt ({url}): {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Geen geldige JSON ({url}): {exc}") from exc


def download_communes(departementen: list[str]) -> dict[str, dict[str, Any]]:
    """Actuele commune-contouren via geo.api.gouv.fr, geïndexeerd op INSEE."""
    by_insee: dict[str, dict[str, Any]] = {}
    for dep in departementen:
        log.info("Download actuele communes departement %s (geo.api.gouv.fr) ...", dep)
        fc = _get_json(GEO_API_COMMUNES.format(dep=dep))
        feats = fc.get("features", []) if isinstance(fc, dict) else fc
        if not feats:
            raise RuntimeError(f"Geen features voor departement {dep}.")
        for feat in feats:
            code = str(feat.get("properties", {}).get("code", "")).strip()
            if code and feat.get("geometry"):
                by_insee[code] = feat
        log.info("  %d communes (departement %s)", len(feats), dep)
    log.info("Totaal %d unieke communes als geometrie beschikbaar.", len(by_insee))
    return by_insee


def _clean(geom: BaseGeometry) -> BaseGeometry:
    return geom if geom.is_valid else geom.buffer(0)


def download_land_mask(bbox: tuple[float, float, float, float]) -> BaseGeometry | None:
    """Land-/kustlijn-polygoon, beperkt tot de regio-bbox (of None bij falen)."""
    try:
        fc = _get_json(LAND_GEOJSON_URL)
    except RuntimeError as exc:
        log.warning("Kustlijn-bron niet bereikbaar, sla clipping over: %s", exc)
        return None
    from shapely.geometry import box
    region = box(*bbox).buffer(0.2)
    delen = [
        _clean(shape(f["geometry"])).intersection(region)
        for f in fc.get("features", [])
        if _clean(shape(f["geometry"])).intersects(region)
    ]
    if not delen:
        log.warning("Geen landpolygoon in de regio; sla clipping over.")
        return None
    log.info("Kustlijn-masker opgebouwd (Natural Earth 10m land).")
    return _clean(unary_union(delen))


# --------------------------------------------------------------------------- #
# Cru-polygonen bouwen                                                         #
# --------------------------------------------------------------------------- #

def build_cru_geometries(
    insee_to_cru: dict[str, str],
    communes_by_insee: dict[str, dict[str, Any]],
) -> dict[str, BaseGeometry]:
    """Dissolve per cru; rapporteer INSEE-codes zonder geometrie."""
    per_cru: dict[str, list[BaseGeometry]] = {cru: [] for cru in CRU_PRIORITY}
    ontbrekend: list[str] = []
    for insee, cru in insee_to_cru.items():
        feat = communes_by_insee.get(insee)
        if feat is None:
            ontbrekend.append(insee)
            continue
        per_cru[cru].append(_clean(shape(feat["geometry"])))
    if ontbrekend:
        log.warning(
            "Geen geometrie voor %d INSEE-code(s) (verouderd/opgeheven): %s",
            len(ontbrekend), ", ".join(sorted(ontbrekend)),
        )
    cru_geoms: dict[str, BaseGeometry] = {}
    for cru, geoms in per_cru.items():
        if not geoms:
            log.warning("Cru '%s' zonder geometrie!", cru)
            continue
        cru_geoms[cru] = _clean(unary_union(geoms))
        log.info("Cru '%s': %d communes samengevoegd.", cru, len(geoms))
    return cru_geoms


def _drop_interior_rings(geom: BaseGeometry) -> BaseGeometry:
    """Geef de geometrie terug zonder interne gaten (alleen buitenranden)."""
    if geom.geom_type == "Polygon":
        return Polygon(geom.exterior)
    if geom.geom_type == "MultiPolygon":
        return _clean(unary_union([Polygon(p.exterior) for p in geom.geoms]))
    return geom


def fill_internal_gaps(
    cru_geoms: dict[str, BaseGeometry],
    adjacency_eps: float = 0.0006,
) -> dict[str, BaseGeometry]:
    """Vul ALLE interne gaten (witte vlekken) van het totale cru-gebied op.

    Op UNIE-niveau, zodat ook gaten op de GRENS tussen twee crus worden gevuld
    (die zijn voor één cru geen intern gat):
      1. Bepaal de unie van alle cru-vlakken en haal er de interne gaten uit.
      2. Het verschil = de losse gat-polygonen (ontbrekende gemeenten e.d.).
      3. Ken elk gat toe aan de cru die er het meest omheen ligt (grootste
         overlap van een licht opgeblazen gat met die cru) en voeg het toe.
    Geen witte enclaves, geen overlap (elk gat gaat naar precies één cru).
    """
    crus = dict(cru_geoms)
    union_all = _clean(unary_union(list(crus.values())))
    filled = _drop_interior_rings(union_all)
    gaps = _clean(filled.difference(union_all))
    if gaps.is_empty:
        log.info("Geen interne gaten gevonden.")
        return crus

    gap_polys = list(gaps.geoms) if gaps.geom_type == "MultiPolygon" else [gaps]
    out = dict(crus)
    bijgevuld = 0.0
    n = 0
    for gap in gap_polys:
        if gap.area <= 0:
            continue
        opgeblazen = gap.buffer(adjacency_eps)
        scores = {c: opgeblazen.intersection(g).area for c, g in crus.items()}
        beste = max(scores, key=scores.get)
        if scores[beste] <= 0:
            continue  # grenst aan geen enkele cru -> laat staan
        out[beste] = _clean(unary_union([out[beste], gap]))
        bijgevuld += gap.area
        n += 1
    if n:
        log.info("Interne gaten opgevuld: %d stuk(s) (≈%.4f deg²).", n, bijgevuld)
    return out


def clip_to_coast(cru_geoms: dict[str, BaseGeometry], land: BaseGeometry) -> dict[str, BaseGeometry]:
    """Knip de cru-vlakken op de kustlijn (alleen met --clip)."""
    return {cru: _clean(geom.intersection(land)) for cru, geom in cru_geoms.items()}


def simplify_all(cru_geoms: dict[str, BaseGeometry]) -> dict[str, BaseGeometry]:
    """Vereenvoudig elk cru-vlak licht (kleinere bestanden)."""
    return {
        cru: _clean(geom.simplify(SIMPLIFY_TOLERANCE_DEG, preserve_topology=True))
        for cru, geom in cru_geoms.items()
    }


def _round_coords(obj: Any, ndigits: int) -> Any:
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, (list, tuple)):
        return [_round_coords(x, ndigits) for x in obj]
    return obj


def to_feature_collection(cru_geoms: dict[str, BaseGeometry]) -> dict[str, Any]:
    features = []
    for cru in CRU_PRIORITY:
        geom = cru_geoms.get(cru)
        if geom is None or geom.is_empty:
            continue
        features.append({
            "type": "Feature",
            "properties": {"cru": cru, "kleur": CRU_COLORS[cru]},
            "geometry": _round_coords(mapping(geom), COORD_PRECISION),
        })
    return {
        "type": "FeatureCollection",
        "name": "cognac-crus",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }


def write_json(path: Path, obj: dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log.info("Geschreven: %s (%d KB)", path.name, path.stat().st_size // 1024)


# --------------------------------------------------------------------------- #
# Style-generatie                                                             #
# --------------------------------------------------------------------------- #

def _fill_color_expression() -> list[Any]:
    expr: list[Any] = ["match", ["get", "cru"]]
    for cru, kleur in CRU_COLORS.items():
        expr.extend([cru, kleur])
    expr.append("#999999")
    return expr


def inject_cru_layer(
    style: dict[str, Any],
    geojson_url: str | None,
    geojson_inline: dict[str, Any] | None,
) -> dict[str, Any]:
    if (geojson_url is None) == (geojson_inline is None):
        raise ValueError("Geef precies één van geojson_url / geojson_inline op.")
    style = json.loads(json.dumps(style))
    style.setdefault("sources", {})["cognac-crus"] = {
        "type": "geojson",
        "data": geojson_inline if geojson_inline is not None else geojson_url,
    }
    fill_layer = {
        "id": "cognac-cru-vlakken",
        "type": "fill",
        "source": "cognac-crus",
        "paint": {
            "fill-color": _fill_color_expression(),
            "fill-opacity": FILL_OPACITY,
            "fill-outline-color": "rgba(80,80,80,0.5)",
        },
    }
    layers = style.get("layers", [])
    idx = next((i for i, l in enumerate(layers) if l.get("type") == "symbol"), len(layers))
    layers.insert(idx, fill_layer)
    style["layers"] = layers
    return style


def build_styles(geojson_inline: dict[str, Any] | None) -> None:
    for variant, out_path in (("light", OUTPUT_STYLE_LIGHT), ("dark", OUTPUT_STYLE_DARK)):
        base = _get_json(OFM_STYLE_URLS[variant])
        merged = inject_cru_layer(
            base,
            geojson_url=None if geojson_inline else GEOJSON_PUBLIC_URL,
            geojson_inline=geojson_inline,
        )
        write_json(out_path, merged)


# --------------------------------------------------------------------------- #
# Verificatie                                                                  #
# --------------------------------------------------------------------------- #

def verify_against_producers(cru_geoms: dict[str, BaseGeometry]) -> None:
    """Controleer dat elke producent binnen het vlak van zijn eigen cru valt."""
    data = load_json(PRODUCERS_PATH)
    producers = data.get("producers", [])
    tol = 0.003
    buffered = {cru: g.buffer(tol) for cru, g in cru_geoms.items()}
    juist = 0
    anders: list[str] = []
    buiten: list[str] = []
    for p in producers:
        pt = Point(p["lon"], p["lat"])
        eigen = buffered.get(p["cru"])
        if eigen is not None and eigen.contains(pt):
            juist += 1
            continue
        elders = [cru for cru, g in buffered.items() if g.contains(pt)]
        if elders:
            anders.append(f"{p['naam']} ({p['gemeente']}): notitie={p['cru']} -> ligt in {elders}")
        else:
            buiten.append(f"{p['naam']} ({p['gemeente']}, {p['cru']})")
    log.info("VERIFICATIE: %d/%d producenten in eigen cru-vlak.", juist, len(producers))
    if anders:
        log.warning("In een ANDER cru-vlak (%d):", len(anders))
        for r in anders:
            log.warning("  %s", r)
    if buiten:
        log.warning("BUITEN elk vlak — mogelijk gat (%d):", len(buiten))
        for r in buiten:
            log.warning("  %s", r)
    if not anders and not buiten:
        log.info("Geen gaten gevonden bij de producent-locaties. 🎉")


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bouw de Cognac-crus kaartlaag (v3.2).")
    parser.add_argument("--skip-styles", action="store_true")
    parser.add_argument("--clip", action="store_true",
                        help="WEL op de kustlijn knippen (alleen zinvol met een fijne kustbron)")
    parser.add_argument("--no-fill", action="store_true", help="interne gaten NIET opvullen")
    parser.add_argument("--inline-geojson", action="store_true")
    parser.add_argument("--verify", action="store_true", help="check tegen producers_points.json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        mapping_data = load_json(MAPPING_PATH)
        insee_to_cru = build_insee_to_cru(mapping_data)
        communes = download_communes(DEPARTEMENTEN)
        cru_geoms = build_cru_geometries(insee_to_cru, communes)

        if not args.no_fill:
            cru_geoms = fill_internal_gaps(cru_geoms)

        # Kustlijn-clipping is standaard UIT (volledige commune-grenzen behouden).
        if args.clip:
            land = download_land_mask(unary_union(list(cru_geoms.values())).bounds)
            if land is not None:
                cru_geoms = clip_to_coast(cru_geoms, land)

        cru_geoms = simplify_all(cru_geoms)

        fc = to_feature_collection(cru_geoms)
        write_json(OUTPUT_GEOJSON, fc)

        if args.verify:
            verify_against_producers(cru_geoms)

        if not args.skip_styles:
            build_styles(geojson_inline=fc if args.inline_geojson else None)

        log.info("Klaar.")
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("Bouw afgebroken: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
