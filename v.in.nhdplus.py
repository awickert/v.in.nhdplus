#!/usr/bin/env python3
############################################################################
#
# MODULE:       v.in.nhdplus
#
# AUTHOR(S):    Andrew Wickert
#
# PURPOSE:      Import NHDPlus stream flowlines, catchments, and WBD
#               watershed boundaries into GRASS GIS
#
# COPYRIGHT:    (c) 2026 Andrew Wickert
#
#               This program is free software under the GNU General Public
#               License (>=v2). Read the file COPYING that comes with GRASS
#               for details.
#
#############################################################################

#%module
#% description: Download NHDPlus flowlines, catchments, and WBD watershed units within the current region or specified HUCs
#% keyword: vector
#% keyword: import
#% keyword: hydrology
#% keyword: NHD
#% keyword: stream network
#% keyword: watershed
#% keyword: HUC
#%end

#%option G_OPT_V_OUTPUT
#%  key: output
#%  label: Output map basename ({output}_flowlines, {output}_catchments, {output}_hucN)
#%  required: yes
#%end

#%option
#%  key: type
#%  type: string
#%  label: Feature type(s) to import
#%  options: flowlines,catchments,both
#%  answer: both
#%  required: yes
#%end

#%option
#%  key: min_order
#%  type: integer
#%  label: Minimum Strahler stream order to import (1 = all streams)
#%  answer: 1
#%  required: no
#%end

#%option
#%  key: source
#%  type: string
#%  label: NHDPlus version to query
#%  options: v2,hr
#%  answer: v2
#%  required: no
#%  description: v2=NHDPlus v2 (1:100k, richest attributes); hr=NHDPlus HR (1:24k)
#%end

#%option
#%  key: hucs
#%  type: string
#%  label: Comma-separated HUC codes to use as spatial filter (level inferred from code length)
#%  description: e.g. 10190005,10190006 (HUC8) or 1019 (HUC4). All codes must be the same level.
#%  required: no
#%end

#%option
#%  key: huc_level
#%  type: integer
#%  label: Download Watershed Boundary Dataset (WBD) boundaries at this HUC level
#%  options: 2,4,6,8,10,12
#%  required: no
#%  description: Imports {output}_hucN map. May be combined with hucs= or used alone.
#%end

import os
import sys
import tempfile
import atexit

import grass.script as gs

_TMPFILES = []

_FLOWLINE_COLS = [
    'comid', 'gnis_name', 'streamorde', 'streamcalc',
    'areasqkm', 'totdasqkm', 'lengthkm',
    'ftype', 'reachcode', 'geometry',
]
_CATCHMENT_COLS = ['featureid', 'areasqkm', 'geometry']


def cleanup():
    for f in _TMPFILES:
        try:
            os.unlink(f)
        except OSError:
            pass


def _tmpfile(suffix=''):
    p = tempfile.mktemp(suffix=suffix)
    _TMPFILES.append(p)
    return p


def require_package(pkg):
    try:
        __import__(pkg)
    except ImportError:
        gs.fatal(
            "Python package '{}' is required. "
            "Install with: pip3 install --break-system-packages {}".format(pkg, pkg)
        )


def get_geographic_bbox():
    """Return (xmin, ymin, xmax, ymax) in WGS84."""
    region = gs.region()
    proj = gs.parse_command('g.proj', flags='g')
    proj_name = proj.get('proj', '')

    if proj_name in ('ll', 'longlat'):
        return region['w'], region['s'], region['e'], region['n']

    corners = [
        (region['w'], region['s']), (region['w'], region['n']),
        (region['e'], region['s']), (region['e'], region['n']),
    ]
    lons, lats = [], []
    for x, y in corners:
        proc = gs.Popen(
            ['m.proj', '-i', 'coordinates={},{}'.format(x, y), 'separator=space'],
            stdout=gs.PIPE, stderr=gs.PIPE
        )
        out, _ = proc.communicate()
        parts = out.decode().strip().split()
        if len(parts) >= 2:
            lons.append(float(parts[0]))
            lats.append(float(parts[1]))
    if not lons:
        gs.fatal("Could not determine geographic bounding box.")
    return min(lons), min(lats), max(lons), max(lats)


def geodataframe_to_grass(gdf, output):
    """Write a GeoDataFrame to a GRASS vector map via GeoPackage + v.import."""
    if (os.path.exists('/usr/share/proj/proj.db')
            and not os.environ.get('PROJ_DATA')
            and not os.environ.get('PROJ_LIB')):
        os.environ['PROJ_DATA'] = '/usr/share/proj'

    for col in gdf.columns:
        if col != 'geometry':
            gdf[col] = gdf[col].astype(object)

    tmp_gpkg = _tmpfile('.gpkg')
    if os.path.exists(tmp_gpkg):
        os.unlink(tmp_gpkg)
    gdf.to_file(tmp_gpkg, driver='GPKG')
    gs.run_command('v.import', input=tmp_gpkg, output=output, overwrite=True)


def huc_level_from_codes(huc_list):
    """Infer HUC level from code lengths; all codes must be the same length."""
    lengths = set(len(h) for h in huc_list)
    if len(lengths) > 1:
        gs.fatal(
            "HUC codes have mixed lengths ({}). "
            "All codes must be the same HUC level.".format(
                ', '.join(str(l) for l in sorted(lengths)))
        )
    length = lengths.pop()
    valid = {2, 4, 6, 8, 10, 12}
    if length not in valid:
        gs.fatal(
            "HUC code length {} does not correspond to a valid HUC level "
            "(expected 2, 4, 6, 8, 10, or 12 digits).".format(length)
        )
    return length


def fetch_wbd(level, bbox=None, huc_list=None):
    """Fetch WBD watershed boundaries at a given HUC level.

    If huc_list is given, fetches those specific HUCs by ID.
    Otherwise fetches all HUCs intersecting bbox.
    """
    from pynhd import WBD
    layer = 'huc{}'.format(level)
    gs.message("Querying WBD {} boundaries...".format(layer.upper()))
    wbd = WBD(layer)
    if huc_list:
        gdf = wbd.byids(layer, huc_list)
    else:
        gdf = wbd.bybox(bbox)
    if gdf is None or gdf.empty:
        return None
    gs.message("  {:,} {} unit(s) returned.".format(len(gdf), layer.upper()))
    return gdf


def get_query_geometry(huc_list, huc_list_level, bbox):
    """Return a shapely geometry to use as the spatial filter for NHDPlus queries.

    If HUC codes are given, dissolve their boundaries. Otherwise use bbox polygon.
    """
    from shapely.geometry import box as shapely_box
    if huc_list:
        huc_gdf = fetch_wbd(huc_list_level, huc_list=huc_list)
        if huc_gdf is None or huc_gdf.empty:
            gs.fatal("Could not retrieve HUC boundaries for: {}".format(
                ', '.join(huc_list)))
        return huc_gdf.geometry.union_all()
    else:
        xmin, ymin, xmax, ymax = bbox
        return shapely_box(xmin, ymin, xmax, ymax)


def fetch_flowlines(source, query_geom, min_order):
    """Fetch flowlines within query_geom (shapely geometry)."""
    if source == 'hr':
        from pynhd import NHDPlusHR
        gs.message("Querying NHDPlus HR flowlines...")
        nhd = NHDPlusHR('flowline')
        gdf = nhd.bygeom(query_geom)
    else:
        from pynhd import WaterData
        gs.message("Querying NHDPlus v2 flowlines...")
        wd = WaterData('nhdflowline_network')
        gdf = wd.bygeom(query_geom)

    if gdf is None or gdf.empty:
        return None
    gs.message("  {:,} flowline(s) returned.".format(len(gdf)))

    if min_order > 1:
        order_col = next(
            (c for c in gdf.columns if 'streamorde' in c.lower()), None)
        if order_col:
            gdf = gdf[gdf[order_col] >= min_order]
            gs.message("  {:,} after order >= {} filter.".format(
                len(gdf), min_order))
        else:
            gs.warning("Stream order column not found; min_order filter skipped.")

    keep = [c for c in _FLOWLINE_COLS if c in gdf.columns]
    return gdf[keep]


def fetch_catchments(source, query_geom):
    """Fetch catchments within query_geom (shapely geometry)."""
    if source == 'hr':
        from pynhd import NHDPlusHR
        gs.message("Querying NHDPlus HR catchments...")
        nhd = NHDPlusHR('catchment')
        gdf = nhd.bygeom(query_geom)
    else:
        from pynhd import WaterData
        gs.message("Querying NHDPlus v2 catchments...")
        wd = WaterData('catchmentsp')
        gdf = wd.bygeom(query_geom)

    if gdf is None or gdf.empty:
        return None
    gs.message("  {:,} catchment(s) returned.".format(len(gdf)))
    keep = [c for c in _CATCHMENT_COLS if c in gdf.columns]
    return gdf[keep]


def main():
    options, flags = gs.parser()
    atexit.register(cleanup)

    output    = options['output']
    feat_type = options['type']
    min_order = int(options['min_order'])
    source    = options['source']
    hucs_str  = options['hucs'] or ''
    huc_level_str = options['huc_level'] or ''

    require_package('pynhd')
    require_package('geopandas')
    require_package('shapely')

    import geopandas as gpd  # noqa: F401

    # --- parse HUC codes ---
    huc_list = [h.strip() for h in hucs_str.split(',') if h.strip()]
    huc_list_level = huc_level_from_codes(huc_list) if huc_list else None

    do_flowlines  = feat_type in ('flowlines', 'both')
    do_catchments = feat_type in ('catchments', 'both')
    do_wbd        = bool(huc_level_str)
    wbd_level     = int(huc_level_str) if huc_level_str else None

    bbox = get_geographic_bbox()
    gs.message("Bounding box (WGS84): W={:.4f} S={:.4f} E={:.4f} N={:.4f}".format(*bbox))

    if huc_list:
        gs.message("HUC filter: {} HUC{} code(s): {}".format(
            len(huc_list), huc_list_level,
            ', '.join(huc_list[:5]) + ('...' if len(huc_list) > 5 else '')
        ))

    # Build the spatial query geometry once (shared by flowlines + catchments)
    query_geom = None
    if do_flowlines or do_catchments:
        query_geom = get_query_geometry(huc_list, huc_list_level, bbox)

    # --- WBD boundaries ---
    if do_wbd:
        gdf = fetch_wbd(wbd_level, bbox=bbox if not huc_list else None,
                        huc_list=huc_list if huc_list else None)
        if gdf is None or gdf.empty:
            gs.warning("No HUC{} boundaries found.".format(wbd_level))
        else:
            out_wbd = '{}_huc{}'.format(output, wbd_level)
            geodataframe_to_grass(gdf, out_wbd)
            gs.message("WBD HUC{} boundaries imported to '{}'.".format(
                wbd_level, out_wbd))

    # --- flowlines ---
    if do_flowlines:
        gdf = fetch_flowlines(source, query_geom, min_order)
        if gdf is None or gdf.empty:
            gs.warning("No flowlines found.")
        else:
            if len(gdf) > 50000:
                gs.warning(
                    "{:,} flowlines is a large dataset. "
                    "Consider using min_order= to filter.".format(len(gdf))
                )
            out_fl = '{}_flowlines'.format(output)
            geodataframe_to_grass(gdf, out_fl)
            gs.message("Flowlines imported to '{}'.".format(out_fl))
            gs.message("  Columns: {}".format(
                ', '.join(c for c in gdf.columns if c != 'geometry')))

    # --- catchments ---
    if do_catchments:
        gdf = fetch_catchments(source, query_geom)
        if gdf is None or gdf.empty:
            gs.warning("No catchments found.")
        else:
            out_cat = '{}_catchments'.format(output)
            geodataframe_to_grass(gdf, out_cat)
            gs.message("Catchments imported to '{}'.".format(out_cat))

    if do_flowlines and do_catchments:
        gs.message(
            "Tip: flowlines and catchments share COMIDs via "
            "'comid' (flowlines) and 'featureid' (catchments)."
        )


if __name__ == '__main__':
    main()
