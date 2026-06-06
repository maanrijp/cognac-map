#!/usr/bin/env python3
"""Bouw de Cognac-crus kaartlaag voor Obsidian Bases (Maps-plugin).

Dit script is REPRODUCEERBAAR en zelfstandig: draai het op je eigen machine of
n8n-server (waar IGN/GitHub bereikbaar zijn) en het produceert drie bestanden:

  1. cognac-crus.geojson            -> 6 features (één per cru), property `cru`
  2. cognac-crus-style-light.json   -> MapLibre-style: OpenFreeMap 'bright' + cru-fill
  3. cognac-crus-style-dark.json    -> MapLibre-style: OpenFreeMap 'dark'   + cru-fill

STRUCTUUR EN KEUZES
-------------------
* Databron grenzen : france-geojson (Etalab/IGN Admin Express, WGS84/EPSG:4326),
                     gefilterd op INSEE-code per departement (16, 17, 24, 79).
* Cru-toewijzing   : de gevalideerde mapping in `cognac_cru_communes.json`
                     (afgeleid uit het INAO Cahier des Charges Cognac 2024/2025).
* Overlap          : enkele gemeenten liggen in twee crus (rive gauche/droite).
                     We kennen zo'n gemeente toe aan de HOOGSTE prioriteit-cru
                     (CRU_PRIORITY), zodat de vlakken elkaar niet overlappen en
                     de kaart visueel schoon blijft. De gevallen worden gelogd.
* Basiskaart       : OpenFreeMap (de Obsidian-standaard). We halen die style op
                     en injecteren één data-driven fill-laag, net ONDER de
                     tekstlabels, zodat plaatsnamen leesbaar blijven.
* Kleuren          : exact de `rgb(...)`-waarden uit je producent-notities, zodat
                     de gekleurde gebieden matchen met je marker-kleuren.

AFHANKELIJKHEDEN: Python 3.9+, `requests`, `shapely`.
    pip install requests shapely

GEBRUIK:
    python build_cognac_map.py                # bouwt alles
    python build_cognac_map.py --skip-styles  # alleen de geojson
    python build_cognac_map.py --inline-geojson   # geojson in de styles embedden
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterable

import requests
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

# --------------------------------------------------------------------------- #
# Configuratie                                                                 #
# --------------------------------------------------------------------------- #

HERE: Path = Path(__file__).resolve().parent

# Invoer: de gevalideerde mapping (naast dit script plaatsen).
MAPPING_PATH: Path = HERE / "cognac_cru_communes.json"

# Uitvoerbestanden.
OUTPUT_GEOJSON: Path = HERE / "cognac-crus.geojson"
OUTPUT_STYLE_LIGHT: Path = HERE / "cognac-crus-style-light.json"
OUTPUT_STYLE_DARK: Path = HERE / "cognac-crus-style-dark.json"

# PAS DIT AAN naar de plek waar je cognac-crus.geojson host (bv. GitHub raw of
# GitHub Pages). De styles verwijzen hiernaar. Voorbeeld:
#   https://raw.githubusercontent.com/<gebruiker>/<repo>/main/cognac-crus.geojson
GEOJSON_PUBLIC_URL: str = "https://raw.githubusercontent.com/USER/REPO/main/cognac-crus.geojson"

# Transparantie van de gekleurde vlakken (advies uit de brief: 0.45).
FILL_OPACITY: float = 0.45

# Vereenvoudiging van de polygonen (graden). ~0.0008° ≈ 80 m: prima voor een
# regionale overzichtskaart en houdt de bestandsgrootte klein.
SIMPLIFY_TOLERANCE_DEG: float = 0.0008

# Coördinaten afronden op 5 decimalen (~1 m) -> kleinere bestanden.
COORD_PRECISION: int = 5

# Departementen waarin de Cognac-AOC ligt + hun france-geojson commune-bestanden.
FRANCE_GEOJSON_URLS: dict[str, str] = {
    "16": "https://raw.githubusercontent.com/gregoiredavid/france-geojson/master/departements/16-charente/communes-16-charente.geojson",
    "17": "https://raw.githubusercontent.com/gregoiredavid/france-geojson/master/departements/17-charente-maritime/communes-17-charente-maritime.geojson",
    "24": "https://raw.githubusercontent.com/gregoiredavid/france-geojson/master/departements/24-dordogne/communes-24-dordogne.geojson",
    "79": "https://raw.githubusercontent.com/gregoiredavid/france-geojson/master/departements/79-deux-sevres/communes-79-deux-sevres.geojson",
}

# OpenFreeMap-styles (de Obsidian-standaardkaart).
OFM_STYLE_URLS: dict[str, str] = {
    "light": "https://tiles.openfreemap.org/styles/bright",
    "dark": "https://tiles.openfreemap.org/styles/dark",
}

# Kleuren per cru — exact overgenomen uit de producent-notities (property `rgb`).
CRU_COLORS: dict[str, str] = {
    "Grande Champagne": "rgb(230, 73, 41)",
    "Petite Champagne": "rgb(237, 120, 55)",
    "Borderies": "rgb(249, 171, 22)",
    "Fins Bois": "rgb(253, 222, 64)",
    "Bons Bois": "rgb(167, 208, 99)",
    "Bois Ordinaires": "rgb(115, 175, 76)",
}

# Prioriteit bij gemeenten die in meerdere crus voorkomen: de eerste wint.
# Volgorde = van "edelste"/binnenste cru naar buiten, wat visueel logisch is.
CRU_PRIORITY: list[str] = [
    "Grande Champagne",
    "Petite Champagne",
    "Borderies",
    "Fins Bois",
    "Bons Bois",
    "Bois Ordinaires",
]

HTTP_TIMEOUT: int = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cognac-map")


# --------------------------------------------------------------------------- #
# Hulpfuncties                                                                 #
# --------------------------------------------------------------------------- #

def load_mapping(path: Path) -> dict[str, Any]:
    """Lees de gevalideerde cru->communes mapping in.

    Verwacht de structuur: {"crus": {"<cru>": [{"nom":..,"insee":..}, ...]}, ...}.
    Gooit een duidelijke fout als het bestand ontbreekt of corrupt is.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"Mapping niet gevonden: {path}. Plaats 'cognac_cru_communes.json' "
            f"naast dit script."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Mapping is geen geldige JSON: {exc}") from exc

    if "crus" not in data or not isinstance(data["crus"], dict):
        raise ValueError("Mapping mist de verwachte sleutel 'crus' (object).")
    return data


def build_insee_to_cru(mapping: dict[str, Any]) -> dict[str, str]:
    """Maak een eenduidige INSEE -> cru toewijzing op basis van CRU_PRIORITY.

    Een gemeente die in twee crus staat (bv. Cognac in GC én Borderies) wordt
    toegekend aan de cru met de hoogste prioriteit. Deze gevallen worden gelogd.
    """
    crus: dict[str, list[dict[str, str]]] = mapping["crus"]

    # Controleer dat elke cru bekend is in CRU_PRIORITY (anders: configuratiefout).
    onbekend = [c for c in crus if c not in CRU_PRIORITY]
    if onbekend:
        raise ValueError(f"Onbekende cru-namen in mapping (niet in CRU_PRIORITY): {onbekend}")

    rang: dict[str, int] = {cru: i for i, cru in enumerate(CRU_PRIORITY)}
    toewijzing: dict[str, str] = {}
    multi: dict[str, set[str]] = {}

    for cru, communes in crus.items():
        for item in communes:
            insee = str(item["insee"]).strip()
            multi.setdefault(insee, set()).add(cru)
            huidige = toewijzing.get(insee)
            if huidige is None or rang[cru] < rang[huidige]:
                toewijzing[insee] = cru

    gesplitst = {i: sorted(cs) for i, cs in multi.items() if len(cs) > 1}
    if gesplitst:
        log.info("Gemeenten in meerdere crus (toegekend via prioriteit): %d", len(gesplitst))
        for insee, cs in sorted(gesplitst.items()):
            log.info("  %s : %s  -> %s", insee, cs, toewijzing[insee])
    return toewijzing


def download_communes(urls: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Download commune-GeoJSON per departement en indexeer op INSEE-code.

    Retourneert {insee: feature}. Gooit een fout bij netwerk-/parseproblemen.
    """
    by_insee: dict[str, dict[str, Any]] = {}
    for dep, url in urls.items():
        log.info("Download communes departement %s ...", dep)
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            fc = resp.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"Download mislukt voor departement {dep}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Ongeldige GeoJSON voor departement {dep}: {exc}") from exc

        features = fc.get("features", [])
        if not features:
            raise RuntimeError(f"Geen features in commune-bestand van departement {dep}.")
        for feat in features:
            code = str(feat.get("properties", {}).get("code", "")).strip()
            if code:
                by_insee[code] = feat
        log.info("  %d communes geïndexeerd (departement %s)", len(features), dep)

    log.info("Totaal %d unieke communes beschikbaar als geometrie.", len(by_insee))
    return by_insee


def build_cru_geometries(
    insee_to_cru: dict[str, str],
    communes_by_insee: dict[str, dict[str, Any]],
) -> dict[str, BaseGeometry]:
    """Voeg per cru de commune-polygonen samen (dissolve) tot één geometrie.

    Rapporteert INSEE-codes uit de mapping waarvoor géén geometrie gevonden is
    (bijvoorbeeld communes nouvelles die nog niet in de databron zitten; hun
    oude deelgemeenten dekken de oppervlakte doorgaans al af).
    """
    per_cru: dict[str, list[BaseGeometry]] = {cru: [] for cru in CRU_PRIORITY}
    ontbrekend: list[str] = []

    for insee, cru in insee_to_cru.items():
        feat = communes_by_insee.get(insee)
        if feat is None:
            ontbrekend.append(insee)
            continue
        geom = shape(feat["geometry"])
        if not geom.is_valid:
            geom = geom.buffer(0)  # repareer self-intersections
        per_cru[cru].append(geom)

    if ontbrekend:
        log.warning(
            "Geen geometrie gevonden voor %d INSEE-code(s) (waarschijnlijk recente "
            "communes nouvelles; oppervlakte meestal gedekt door deelgemeenten): %s",
            len(ontbrekend),
            ", ".join(sorted(ontbrekend)),
        )

    cru_geoms: dict[str, BaseGeometry] = {}
    for cru, geoms in per_cru.items():
        if not geoms:
            log.warning("Cru '%s' heeft geen enkele matchende commune-geometrie!", cru)
            continue
        merged = unary_union(geoms)            # dissolve tot één (Multi)Polygon
        merged = merged.buffer(0)              # ruim slivers/zelfsnijdingen op
        merged = merged.simplify(SIMPLIFY_TOLERANCE_DEG, preserve_topology=True)
        cru_geoms[cru] = merged
        log.info("Cru '%s': %d communes -> geometrie opgebouwd.", cru, len(geoms))
    return cru_geoms


def _round_coords(obj: Any, ndigits: int) -> Any:
    """Rond alle coördinaten recursief af op `ndigits` decimalen."""
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, list):
        return [_round_coords(x, ndigits) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_round_coords(x, ndigits) for x in obj)
    return obj


def to_feature_collection(cru_geoms: dict[str, BaseGeometry]) -> dict[str, Any]:
    """Zet de cru-geometrieën om in een GeoJSON FeatureCollection (WGS84)."""
    features: list[dict[str, Any]] = []
    # In prioriteitsvolgorde voor een nette, voorspelbare bestandsindeling.
    for cru in CRU_PRIORITY:
        geom = cru_geoms.get(cru)
        if geom is None:
            continue
        geometry = _round_coords(mapping(geom), COORD_PRECISION)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "cru": cru,
                    "kleur": CRU_COLORS[cru],
                },
                "geometry": geometry,
            }
        )
    return {
        "type": "FeatureCollection",
        "name": "cognac-crus",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }


def write_json(path: Path, obj: dict[str, Any]) -> None:
    """Schrijf JSON compact (geen spaties) weg in UTF-8."""
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log.info("Geschreven: %s (%d KB)", path.name, path.stat().st_size // 1024)


# --------------------------------------------------------------------------- #
# Style-generatie                                                             #
# --------------------------------------------------------------------------- #

def _fill_color_expression() -> list[Any]:
    """Bouw de data-driven MapLibre `fill-color` expressie (match op `cru`)."""
    expr: list[Any] = ["match", ["get", "cru"]]
    for cru, kleur in CRU_COLORS.items():
        expr.extend([cru, kleur])
    expr.append("#999999")  # fallback
    return expr


def inject_cru_layer(
    style: dict[str, Any],
    geojson_url: str | None,
    geojson_inline: dict[str, Any] | None,
) -> dict[str, Any]:
    """Voeg de crus-bron + één fill-laag toe aan een bestaande MapLibre-style.

    De fill-laag wordt vlak vóór de eerste tekstlaag (`type == "symbol"`)
    ingevoegd, zodat plaatsnamen leesbaar bovenop de gekleurde vlakken blijven.
    Geef óf een URL (geojson_url) óf inline-data (geojson_inline) op.
    """
    if (geojson_url is None) == (geojson_inline is None):
        raise ValueError("Geef precies één van geojson_url / geojson_inline op.")

    style = json.loads(json.dumps(style))  # diepe kopie, originele style ongemoeid
    style.setdefault("sources", {})
    style["sources"]["cognac-crus"] = {
        "type": "geojson",
        "data": geojson_inline if geojson_inline is not None else geojson_url,
    }

    fill_layer: dict[str, Any] = {
        "id": "cognac-cru-vlakken",
        "type": "fill",
        "source": "cognac-crus",
        "paint": {
            "fill-color": _fill_color_expression(),
            "fill-opacity": FILL_OPACITY,
            "fill-outline-color": "rgba(80,80,80,0.5)",
        },
    }

    layers: list[dict[str, Any]] = style.get("layers", [])
    insert_at = next(
        (i for i, lyr in enumerate(layers) if lyr.get("type") == "symbol"),
        len(layers),  # geen labels gevonden -> bovenop
    )
    layers.insert(insert_at, fill_layer)
    style["layers"] = layers
    return style


def fetch_style(url: str) -> dict[str, Any]:
    """Haal een MapLibre-style-JSON op (OpenFreeMap)."""
    log.info("Style ophalen: %s", url)
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Style-download mislukt ({url}): {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Style is geen geldige JSON ({url}): {exc}") from exc


def build_styles(geojson_inline: dict[str, Any] | None) -> None:
    """Genereer light- en dark-style met de cru-fill-laag erin geïnjecteerd."""
    targets = {
        "light": OUTPUT_STYLE_LIGHT,
        "dark": OUTPUT_STYLE_DARK,
    }
    for variant, out_path in targets.items():
        base = fetch_style(OFM_STYLE_URLS[variant])
        merged = inject_cru_layer(
            base,
            geojson_url=None if geojson_inline else GEOJSON_PUBLIC_URL,
            geojson_inline=geojson_inline,
        )
        write_json(out_path, merged)


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bouw de Cognac-crus kaartlaag.")
    parser.add_argument("--skip-styles", action="store_true", help="alleen de geojson bouwen")
    parser.add_argument(
        "--inline-geojson",
        action="store_true",
        help="geojson direct in de styles embedden i.p.v. via URL",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        mapping_data = load_mapping(MAPPING_PATH)
        insee_to_cru = build_insee_to_cru(mapping_data)
        communes = download_communes(FRANCE_GEOJSON_URLS)
        cru_geoms = build_cru_geometries(insee_to_cru, communes)
        if len(cru_geoms) != len(CRU_PRIORITY):
            log.warning(
                "Let op: %d van %d crus hebben geometrie.", len(cru_geoms), len(CRU_PRIORITY)
            )
        fc = to_feature_collection(cru_geoms)
        write_json(OUTPUT_GEOJSON, fc)

        if not args.skip_styles:
            inline = fc if args.inline_geojson else None
            build_styles(geojson_inline=inline)

        log.info("Klaar. Vergeet niet GEOJSON_PUBLIC_URL aan te passen vóór het hosten.")
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level: log en stop met foutcode
        log.error("Bouw afgebroken: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
