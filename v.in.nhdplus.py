#!/usr/bin/env python3
############################################################################
#
# MODULE:       v.in.nhdplus
#
# AUTHOR(S):    Andrew Wickert
#
# PURPOSE:      Import NHDPlus stream flowlines and catchments into GRASS GIS
#
# COPYRIGHT:    (c) 2026 Andrew Wickert
#
#               This program is free software under the GNU General Public
#               License (>=v2). Read the file COPYING that comes with GRASS
#               for details.
#
#############################################################################

#%module
#% description: Download NHDPlus stream flowlines and catchments within the current region
#% keyword: vector
#% keyword: import
#% keyword: hydrology
#% keyword: NHD
#% keyword: stream network
#% keyword: watershed
#%end

#%option G_OPT_V_OUTPUT
#%  key: output
#%  label: Output map basename ({output}_flowlines and/or {output}_catchments)
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
#%  description: v2=NHDPlus v2 (medium resolution, 1:100k, richest attributes); hr=NHDPlus HR (high resolution, 1:24k)
#%end

import os
import sys
import tempfile
import atexit

import grass.script as gs

_TMPFILES = []

# Columns to retain for each feature type (keeps the attribute table manageable)
_FLOWLINE_COLS = [
    'comid', 'gnis_name', 'streamorde', 'streamcalc',
    'areasqkm', 'totdasqkm', 'lengthkm',
    'ftype', 'reachcode', 'geometry',
]
_CATCHMENT_COLS = [
    'featureid', 'areasqkm', 'geometry',
]


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
        (region['w'], region['s']),
        (region['w'], region['n']),
        (region['e'], region['s']),
        (region['e'], region['n']),
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
    import geopandas as gpd  # noqa: F401 — confirms package available

    if (os.path.exists('/usr/share/proj/proj.db')
            and not os.environ.get('PROJ_DATA')
            and not os.environ.get('PROJ_LIB')):
        os.environ['PROJ_DATA'] = '/usr/share/proj'

    # Cast non-geometry columns to object (pandas 3.x Arrow string compat)
    for col in gdf.columns:
        if col != 'geometry':
            gdf[col] = gdf[col].astype(object)

    tmp_gpkg = _tmpfile('.gpkg')
    if os.path.exists(tmp_gpkg):
        os.unlink(tmp_gpkg)
    gdf.to_file(tmp_gpkg, driver='GPKG')

    gs.run_command('v.import', input=tmp_gpkg, output=output, overwrite=True)


def fetch_flowlines_v2(bbox, min_order):
    """Fetch NHDPlus v2 flowlines within bbox via pynhd WaterData."""
    from pynhd import WaterData
    gs.message("Querying NHDPlus v2 flowlines...")
    wd = WaterData('nhdflowline_network')
    gdf = wd.bybox(bbox)
    if gdf is None or gdf.empty:
        return None
    gs.message("  {:,} flowline(s) returned.".format(len(gdf)))
    if min_order > 1:
        if 'streamorde' in gdf.columns:
            gdf = gdf[gdf['streamorde'] >= min_order]
            gs.message("  {:,} flowline(s) after order >= {} filter.".format(
                len(gdf), min_order))
        else:
            gs.warning("'streamorde' column not found; min_order filter skipped.")
    # Retain a manageable set of columns
    keep = [c for c in _FLOWLINE_COLS if c in gdf.columns]
    return gdf[keep]


def fetch_catchments_v2(bbox):
    """Fetch NHDPlus v2 catchments within bbox via pynhd WaterData."""
    from pynhd import WaterData
    gs.message("Querying NHDPlus v2 catchments...")
    wd = WaterData('catchmentsp')
    gdf = wd.bybox(bbox)
    if gdf is None or gdf.empty:
        return None
    gs.message("  {:,} catchment(s) returned.".format(len(gdf)))
    keep = [c for c in _CATCHMENT_COLS if c in gdf.columns]
    return gdf[keep]


def fetch_flowlines_hr(bbox, min_order):
    """Fetch NHDPlus HR flowlines within bbox via pynhd NHDPlusHR."""
    from pynhd import NHDPlusHR
    gs.message("Querying NHDPlus HR flowlines...")
    nhd = NHDPlusHR('flowline')
    gdf = nhd.bybox(bbox)
    if gdf is None or gdf.empty:
        return None
    gs.message("  {:,} flowline(s) returned.".format(len(gdf)))
    if min_order > 1:
        order_col = next((c for c in gdf.columns if 'streamorde' in c.lower()), None)
        if order_col:
            gdf = gdf[gdf[order_col] >= min_order]
            gs.message("  {:,} flowline(s) after order >= {} filter.".format(
                len(gdf), min_order))
    return gdf


def fetch_catchments_hr(bbox):
    """Fetch NHDPlus HR catchments within bbox via pynhd NHDPlusHR."""
    from pynhd import NHDPlusHR
    gs.message("Querying NHDPlus HR catchments...")
    nhd = NHDPlusHR('catchment')
    gdf = nhd.bybox(bbox)
    if gdf is None or gdf.empty:
        return None
    gs.message("  {:,} catchment(s) returned.".format(len(gdf)))
    return gdf


def main():
    options, flags = gs.parser()
    atexit.register(cleanup)

    output    = options['output']
    feat_type = options['type']
    min_order = int(options['min_order'])
    source    = options['source']

    require_package('pynhd')
    require_package('geopandas')
    require_package('shapely')

    import geopandas as gpd

    bbox = get_geographic_bbox()
    gs.message("Bounding box (WGS84): W={:.4f} S={:.4f} E={:.4f} N={:.4f}".format(*bbox))

    do_flowlines  = feat_type in ('flowlines', 'both')
    do_catchments = feat_type in ('catchments', 'both')
    out_fl  = '{}_flowlines'.format(output)
    out_cat = '{}_catchments'.format(output)

    # --- flowlines ---
    if do_flowlines:
        if source == 'hr':
            gdf = fetch_flowlines_hr(bbox, min_order)
        else:
            gdf = fetch_flowlines_v2(bbox, min_order)

        if gdf is None or gdf.empty:
            gs.warning("No flowlines found within the current region.")
        else:
            if len(gdf) > 50000:
                gs.warning(
                    "{:,} flowlines is a large dataset. "
                    "Consider using min_order= to filter to larger streams.".format(len(gdf))
                )
            geodataframe_to_grass(gdf, out_fl)
            gs.message("Flowlines imported to '{}'.".format(out_fl))
            gs.message(
                "  Attributes: {}".format(
                    ', '.join(c for c in gdf.columns if c != 'geometry')
                )
            )

    # --- catchments ---
    if do_catchments:
        if source == 'hr':
            gdf = fetch_catchments_hr(bbox)
        else:
            gdf = fetch_catchments_v2(bbox)

        if gdf is None or gdf.empty:
            gs.warning("No catchments found within the current region.")
        else:
            geodataframe_to_grass(gdf, out_cat)
            gs.message("Catchments imported to '{}'.".format(out_cat))
            gs.message(
                "  Attributes: {}".format(
                    ', '.join(c for c in gdf.columns if c != 'geometry')
                )
            )

    gs.message("")
    if do_flowlines:
        gs.message("Flowlines: {}".format(out_fl))
    if do_catchments:
        gs.message("Catchments: {}".format(out_cat))

    if do_flowlines and do_catchments:
        gs.message(
            "Tip: flowlines and catchments share COMID via the "
            "'comid' (flowlines) and 'featureid' (catchments) columns."
        )


if __name__ == '__main__':
    main()
