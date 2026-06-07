#!/usr/bin/env python3
"""Bouw de Cognac-crus kaartlaag voor Obsidian Bases (Maps-plugin).

VERSIE 3.7 — splitsing van split-gemeenten, in volgorde van VOORRANG:
  1. AANGELEVERDE LIJN (cru-splitlines.geojson/.kml/.gpx) die de gemeente
     doorsnijdt — bv. de Charente. Jouw getekende lijn wint dus.
  2. OFFICIËLE DEELGEMEENTE-GEOMETRIE (communes déléguées via geo.api) — voor
     fusiegemeenten mét déléguées (zie DELEGUEE_SPLITS), als er geen lijn ligt.
  3. ANDERS: de hele gemeente naar de hoofd-cru (hoogste prioriteit). GEEN
     rasterbenadering -> geen zaagtandranden.

Eerder: actuele grenzen via geo.api (v2), interne gaten vullen (v3.1),
kustlijn-clip standaard uit (v3.2).

Produceert: cognac-crus.geojson + cognac-crus-style-light/dark.json.
AFHANKELIJKHEDEN: Python 3.9+, `requests`, `shapely`.   pip install requests shapely

GEBRUIK:
    python build_cognac_map.py                 # alles
    python build_cognac_map.py --no-split      # split-gemeenten op hoofd-cru laten
    python build_cognac_map.py --verify        # check tegen producers_points.json
    python build_cognac_map.py --no-fill / --clip / --skip-styles / --inline-geojson
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

import requests
from shapely.geometry import (
    LineString, MultiLineString, MultiPolygon, Point, Polygon, box, mapping, shape,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union

# --------------------------------------------------------------------------- #
# Configuratie                                                                 #
# --------------------------------------------------------------------------- #

HERE: Path = Path(__file__).resolve().parent

MAPPING_PATH: Path = HERE / "cognac_cru_communes.json"
PRODUCERS_PATH: Path = HERE / "producers_points.json"  # alleen voor --verify

SPLITLINE_CANDIDATES: list[str] = [
    "cru-splitlines.geojson", "cru-splitlines.json",
    "cru-splitlines.kml", "cru-splitlines.gpx",
]

OUTPUT_GEOJSON: Path = HERE / "cognac-crus.geojson"
OUTPUT_STYLE_LIGHT: Path = HERE / "cognac-crus-style-light.json"
OUTPUT_STYLE_DARK: Path = HERE / "cognac-crus-style-dark.json"

GEOJSON_PUBLIC_URL: str = "https://raw.githubusercontent.com/maanrijp/cognac-map/main/cognac-crus.geojson"

FILL_OPACITY: float = 0.45
SIMPLIFY_TOLERANCE_DEG: float = 0.0006
COORD_PRECISION: int = 5
HTTP_TIMEOUT: int = 120

DEPARTEMENTEN: list[str] = ["16", "17", "24", "79"]

GEO_API_COMMUNES: str = (
    "https://geo.api.gouv.fr/communes"
    "?codeDepartement={dep}&fields=code,nom&format=geojson&geometry=contour"
)
GEO_API_DELEGUEE: str = (
    "https://geo.api.gouv.fr/communes_associees_deleguees/{code}"
    "?fields=nom,code&format=geojson&geometry=contour"
)

# Datagedreven split via officiële deelgemeente-geometrie (gebruikt als er geen
# lijn ligt):  commune nouvelle (INSEE) -> { déléguée-INSEE: cru }.
DELEGUEE_SPLITS: dict[str, dict[str, str]] = {
    "16224": {"16224": "Petite Champagne", "16179": "Fins Bois"},  # Montmérac: Montchaude / Lamérac
    "16233": {"16233": "Petite Champagne", "16351": "Fins Bois"},  # Mosnac-Saint-Simeux: Mosnac / Saint-Simeux
}

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
    "Grande Champagne", "Petite Champagne", "Borderies",
    "Fins Bois", "Bons Bois", "Bois Ordinaires",
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("cognac-map")


# --------------------------------------------------------------------------- #
# Inlezen & lidmaatschap                                                       #
# --------------------------------------------------------------------------- #

def load_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"Bestand niet gevonden: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Ongeldige JSON in {path}: {exc}") from exc


def membership_from_mapping(mapping: dict[str, Any]) -> dict[str, set[str]]:
    crus: dict[str, list[dict[str, Any]]] = mapping["crus"]
    onbekend = [c for c in crus if c not in CRU_PRIORITY]
    if onbekend:
        raise ValueError(f"Onbekende cru-namen (niet in CRU_PRIORITY): {onbekend}")
    membership: dict[str, set[str]] = {}
    for cru, communes in crus.items():
        for item in communes:
            membership.setdefault(str(item["insee"]).strip(), set()).add(cru)
    return membership


# --------------------------------------------------------------------------- #
# Splitlijnen (GeoJSON / KML / GPX)                                            #
# --------------------------------------------------------------------------- #

def _lines_from_geojson(data: dict[str, Any]) -> list[LineString]:
    out: list[LineString] = []
    feats = data.get("features", []) if data.get("type") == "FeatureCollection" else [data]
    for f in feats:
        geom = f.get("geometry", f)
        t = geom.get("type")
        if t == "LineString":
            out.append(LineString(geom["coordinates"]))
        elif t == "MultiLineString":
            out.extend(LineString(c) for c in geom["coordinates"])
    return out


def _lines_from_kml(text: str) -> list[LineString]:
    out: list[LineString] = []
    for el in ET.fromstring(text).iter():
        if el.tag.rsplit("}", 1)[-1] == "coordinates" and el.text:
            pts = []
            for token in el.text.split():
                lon, lat, *_ = token.split(",")
                pts.append((float(lon), float(lat)))
            if len(pts) >= 2:
                out.append(LineString(pts))
    return out


def _lines_from_gpx(text: str) -> list[LineString]:
    out: list[LineString] = []
    for seg in ET.fromstring(text).iter():
        if seg.tag.rsplit("}", 1)[-1] in ("trkseg", "rte"):
            pts = [(float(pt.get("lon")), float(pt.get("lat")))
                   for pt in seg if pt.tag.rsplit("}", 1)[-1] in ("trkpt", "rtept")]
            if len(pts) >= 2:
                out.append(LineString(pts))
    return out


def load_split_lines() -> list[LineString]:
    path = next((HERE / n for n in SPLITLINE_CANDIDATES if (HERE / n).is_file()), None)
    if path is None:
        log.info("Geen splitlijn-bestand gevonden.")
        return []
    suffix = path.suffix.lower()
    try:
        if suffix in (".geojson", ".json"):
            lines = _lines_from_geojson(json.loads(path.read_text(encoding="utf-8")))
        elif suffix == ".kml":
            lines = _lines_from_kml(path.read_text(encoding="utf-8"))
        elif suffix == ".gpx":
            lines = _lines_from_gpx(path.read_text(encoding="utf-8"))
        else:
            lines = []
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Kon splitlijnen niet lezen uit {path.name}: {exc}") from exc
    log.info("Splitlijnen geladen uit %s: %d lijn(en).", path.name, len(lines))
    return lines


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


def _clean(geom: BaseGeometry) -> BaseGeometry:
    return geom if geom.is_valid else geom.buffer(0)


def download_communes(departementen: list[str]) -> dict[str, dict[str, Any]]:
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


_DELEGUEE_CACHE: dict[str, BaseGeometry] = {}


def fetch_deleguee_contour(code: str) -> BaseGeometry:
    """Haal de contour van een commune déléguée op (met cache)."""
    if code not in _DELEGUEE_CACHE:
        feat = _get_json(GEO_API_DELEGUEE.format(code=code))
        _DELEGUEE_CACHE[code] = _clean(shape(feat["geometry"]))
    return _DELEGUEE_CACHE[code]


def download_land_mask(bbox: tuple[float, float, float, float]) -> BaseGeometry | None:
    try:
        fc = _get_json(LAND_GEOJSON_URL)
    except RuntimeError as exc:
        log.warning("Kustlijn-bron niet bereikbaar, sla clipping over: %s", exc)
        return None
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
# Split-gemeenten verdelen                                                     #
# --------------------------------------------------------------------------- #

def _nearest_cru(piece: BaseGeometry, masses: dict[str, BaseGeometry]) -> str:
    rang = {c: i for i, c in enumerate(CRU_PRIORITY)}
    pt = piece.representative_point()
    return min(masses, key=lambda c: (pt.distance(masses[c]), rang[c]))


def _split_by_deleguees(parent_geom: BaseGeometry, spec: dict[str, str]) -> dict[str, list[BaseGeometry]]:
    """Splits een fusiegemeente via officiële deelgemeente-geometrie (geo.api)."""
    parts: dict[str, list[BaseGeometry]] = {}
    gebruikt: list[BaseGeometry] = []
    for dcode, cru in spec.items():
        dg = fetch_deleguee_contour(dcode)
        clipped = _clean(dg.intersection(parent_geom))
        if not clipped.is_empty:
            parts.setdefault(cru, []).append(clipped)
            gebruikt.append(clipped)
    rest = _clean(parent_geom.difference(unary_union(gebruikt))) if gebruikt else parent_geom
    if not rest.is_empty and rest.area > 1e-8 and parts:
        massas = {cru: _clean(unary_union(gs)) for cru, gs in parts.items()}
        for stuk in (rest.geoms if rest.geom_type == "MultiPolygon" else [rest]):
            parts[_nearest_cru(stuk, massas)].append(stuk)
    return parts


def _split_by_lines(geom: BaseGeometry, lines: list[LineString]) -> list[BaseGeometry] | None:
    relevant = [ln for ln in lines if ln.intersects(geom)]
    if not relevant:
        return None
    merged = unary_union([geom.boundary, *relevant])
    faces = [f for f in polygonize(merged) if geom.contains(f.representative_point())]
    if len(faces) <= 1:
        return None
    return [_clean(f) for f in faces]


def build_cru_geometries(
    membership: dict[str, set[str]],
    communes_by_insee: dict[str, dict[str, Any]],
    split_lines: list[LineString],
    split: bool = True,
) -> dict[str, BaseGeometry]:
    """Bouw per cru de geometrie; split-gemeenten via lijn / déléguée / hoofd-cru."""
    enkel: dict[str, list[BaseGeometry]] = {cru: [] for cru in CRU_PRIORITY}
    split_communes: list[tuple[str, BaseGeometry, set[str]]] = []
    ontbrekend: list[str] = []
    rang = {c: i for i, c in enumerate(CRU_PRIORITY)}

    for code, crus in membership.items():
        feat = communes_by_insee.get(code)
        if feat is None:
            ontbrekend.append(code)
            continue
        geom = _clean(shape(feat["geometry"]))
        if len(crus) == 1 or not split:
            enkel[min(crus, key=lambda c: rang[c])].append(geom)
        else:
            split_communes.append((code, geom, crus))

    if ontbrekend:
        log.warning("Geen geometrie voor %d INSEE-code(s) (verouderd/opgeheven): %s",
                    len(ontbrekend), ", ".join(sorted(ontbrekend)))

    massa = {cru: (_clean(unary_union(g)) if g else Polygon()) for cru, g in enkel.items()}
    massa_snel = {c: m.simplify(0.003) for c, m in massa.items()}
    extra: dict[str, list[BaseGeometry]] = {cru: [] for cru in CRU_PRIORITY}

    # Per split-gemeente, in volgorde van VOORRANG:
    #   1) aangeleverde lijn (rivier) die de gemeente doorsnijdt;
    #   2) officiële deelgemeente-geometrie (déléguées);
    #   3) anders de hele gemeente naar de hoofd-cru (geen zaagtandranden).
    via_lijn = via_deleguee = via_heel = 0
    for code, geom, crus in split_communes:
        relevante = {c: massa_snel[c] for c in crus}
        faces = _split_by_lines(geom, split_lines) if split_lines else None
        if faces:
            via_lijn += 1
            for f in faces:
                extra[_nearest_cru(f, relevante)].append(f)
        elif code in DELEGUEE_SPLITS:
            try:
                for cru, gs in _split_by_deleguees(geom, DELEGUEE_SPLITS[code]).items():
                    extra[cru].extend(gs)
                via_deleguee += 1
            except RuntimeError as exc:
                doel = sorted(set(DELEGUEE_SPLITS[code].values()), key=lambda c: rang[c])[0]
                log.warning("Déléguée-split mislukt voor %s (%s); hele gemeente -> %s.", code, exc, doel)
                extra[doel].append(geom)
                via_heel += 1
        else:
            extra[min(crus, key=lambda c: rang[c])].append(geom)
            via_heel += 1

    if split_communes:
        log.info("Split-gemeenten: %d via aangeleverde lijn, %d via déléguée-data, "
                 "%d heel naar hoofd-cru.", via_lijn, via_deleguee, via_heel)

    cru_geoms: dict[str, BaseGeometry] = {}
    for cru in CRU_PRIORITY:
        stukken = enkel[cru] + extra[cru]
        if stukken:
            cru_geoms[cru] = _clean(unary_union(stukken))
            log.info("Cru '%s' opgebouwd.", cru)
    return cru_geoms


# --------------------------------------------------------------------------- #
# Nabewerking                                                                  #
# --------------------------------------------------------------------------- #

def _drop_interior_rings(geom: BaseGeometry) -> BaseGeometry:
    if geom.geom_type == "Polygon":
        return Polygon(geom.exterior)
    if geom.geom_type == "MultiPolygon":
        return _clean(unary_union([Polygon(p.exterior) for p in geom.geoms]))
    return geom


def fill_internal_gaps(cru_geoms: dict[str, BaseGeometry],
                       adjacency_eps: float = 0.0006) -> dict[str, BaseGeometry]:
    crus = dict(cru_geoms)
    union_all = _clean(unary_union(list(crus.values())))
    gaps = _clean(_drop_interior_rings(union_all).difference(union_all))
    if gaps.is_empty:
        log.info("Geen interne gaten gevonden.")
        return crus
    gap_polys = list(gaps.geoms) if gaps.geom_type == "MultiPolygon" else [gaps]
    out = dict(crus)
    n = 0
    for gap in gap_polys:
        if gap.area <= 0:
            continue
        scores = {c: gap.buffer(adjacency_eps).intersection(g).area for c, g in crus.items()}
        beste = max(scores, key=scores.get)
        if scores[beste] <= 0:
            continue
        out[beste] = _clean(unary_union([out[beste], gap]))
        n += 1
    if n:
        log.info("Interne gaten opgevuld: %d stuk(s).", n)
    return out


def clip_to_coast(cru_geoms: dict[str, BaseGeometry], land: BaseGeometry) -> dict[str, BaseGeometry]:
    return {cru: _clean(geom.intersection(land)) for cru, geom in cru_geoms.items()}


def simplify_all(cru_geoms: dict[str, BaseGeometry]) -> dict[str, BaseGeometry]:
    return {cru: _clean(geom.simplify(SIMPLIFY_TOLERANCE_DEG, preserve_topology=True))
            for cru, geom in cru_geoms.items()}


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


def inject_cru_layer(style: dict[str, Any], geojson_url: str | None,
                     geojson_inline: dict[str, Any] | None) -> dict[str, Any]:
    if (geojson_url is None) == (geojson_inline is None):
        raise ValueError("Geef precies één van geojson_url / geojson_inline op.")
    style = json.loads(json.dumps(style))
    style.setdefault("sources", {})["cognac-crus"] = {
        "type": "geojson",
        "data": geojson_inline if geojson_inline is not None else geojson_url,
    }
    fill_layer = {
        "id": "cognac-cru-vlakken", "type": "fill", "source": "cognac-crus",
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
        merged = inject_cru_layer(base,
                                  geojson_url=None if geojson_inline else GEOJSON_PUBLIC_URL,
                                  geojson_inline=geojson_inline)
        write_json(out_path, merged)


# --------------------------------------------------------------------------- #
# Verificatie                                                                  #
# --------------------------------------------------------------------------- #

def verify_against_producers(cru_geoms: dict[str, BaseGeometry]) -> None:
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
        (anders if elders else buiten).append(
            f"{p['naam']} ({p['gemeente']}): notitie={p['cru']}"
            + (f" -> ligt in {elders}" if elders else " -> ligt buiten elk vlak"))
    log.info("VERIFICATIE: %d/%d producenten in eigen cru-vlak.", juist, len(producers))
    for kop, lijst in (("In een ANDER cru-vlak", anders), ("BUITEN elk vlak", buiten)):
        if lijst:
            log.warning("%s (%d):", kop, len(lijst))
            for r in lijst:
                log.warning("  %s", r)
    if not anders and not buiten:
        log.info("Geen afwijkingen bij de producent-locaties. 🎉")


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bouw de Cognac-crus kaartlaag (v3.7).")
    parser.add_argument("--skip-styles", action="store_true")
    parser.add_argument("--clip", action="store_true", help="WEL op de kustlijn knippen")
    parser.add_argument("--no-fill", action="store_true", help="interne gaten NIET opvullen")
    parser.add_argument("--no-split", action="store_true", help="split-gemeenten NIET splitsen")
    parser.add_argument("--inline-geojson", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        mapping_data = load_json(MAPPING_PATH)
        membership = membership_from_mapping(mapping_data)
        split_lines = [] if args.no_split else load_split_lines()
        communes = download_communes(DEPARTEMENTEN)
        cru_geoms = build_cru_geometries(membership, communes, split_lines, split=not args.no_split)

        if not args.no_fill:
            cru_geoms = fill_internal_gaps(cru_geoms)
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
