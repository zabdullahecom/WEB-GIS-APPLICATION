import os
import json
import secrets
import zipfile
import shutil
from flask import Flask, session, request, redirect, url_for, render_template, jsonify
import pandas as pd
import geopandas as gpd
import fiona

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# Directories
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
UPLOAD_GPKG = os.path.join(UPLOAD_FOLDER, 'uploads.gpkg')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Supported extensions
ALLOWED_TABULAR = {'.csv', '.xls', '.xlsx'}
ALLOWED_GEO = {'.geojson', '.json', '.kml', '.kmz', '.shp'}

###############################
# Helper Functions
###############################

def allowed_file(filename, exts):
    return os.path.splitext(filename)[1].lower() in exts


def try_load_csv(path):
    df0 = pd.read_csv(path, nrows=0, dtype=str)
    return pd.read_csv(path, names=df0.columns, header=0, dtype=str, engine='python', on_bad_lines='skip')


def try_load_excel(path):
    return pd.read_excel(path, dtype=str)


def try_load_kml(path):
    # attempt reading via GeoPandas (Fiona)
    try:
        return gpd.read_file(path, driver='KML')
    except Exception:
        pass
    # attempt via Pyogrio (GDAL)
    try:
        return gpd.read_file(path, engine='pyogrio')
    except Exception:
        pass
    # fallback: no driver, generic
    return gpd.read_file(path)


def try_load_kmz(path):
    # unzip single KML inside KMZ
    tmp = os.path.join(UPLOAD_FOLDER, 'tmp_kmz')
    os.makedirs(tmp, exist_ok=True)
    with zipfile.ZipFile(path, 'r') as z:
        kml_files = [f for f in z.namelist() if f.lower().endswith('.kml')]
        if not kml_files:
            raise ValueError('No .kml inside KMZ')
        z.extract(kml_files[0], tmp)
        kml_path = os.path.join(tmp, kml_files[0])
    gdf = try_load_kml(kml_path)
    shutil.rmtree(tmp, ignore_errors=True)
    return gdf


def try_load_geospatial(path):
    ext = os.path.splitext(path)[1].lower()
    # register KML/LIBKML driver
    fiona.drvsupport.supported_drivers['KML'] = 'r'
    fiona.drvsupport.supported_drivers['LIBKML'] = 'r'
    # choose loader
    if ext == '.kml':
        gdf = try_load_kml(path)
    elif ext == '.kmz':
        gdf = try_load_kmz(path)
    else:
        # shapefile or geojson
        gdf = gpd.read_file(path)

    if gdf.empty or 'geometry' not in gdf:
        raise ValueError('No valid geometry')

    # enforce EPSG:4326
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.set_crs(epsg=4326, allow_override=True).to_crs(epsg=4326)

    # explode multipart
    try:
        gdf = gdf.explode(index_parts=False).reset_index(drop=True)
    except Exception:
        pass

    return gdf


def save_to_gpkg(layer_name, gdf):
    gdf.to_file(UPLOAD_GPKG, layer=layer_name, driver='GPKG')


def add_marker(lat, lon, html, layer):
    m = session.get('markers', [])
    m.append({'lat': lat, 'lon': lon, 'popup': html, 'layer': layer})
    session['markers'] = m

###############################
# Routes
###############################

@app.route('/')
def index():
    session.setdefault('markers', [])
    return render_template('index.html')

@app.route('/api/upload', methods=['POST'])
def api_upload():
    file = request.files.get('data_file')
    if not file or not file.filename:
        return jsonify({'error': 'No file uploaded'}), 400
    name = file.filename
    ext = os.path.splitext(name)[1].lower()
    path = os.path.join(UPLOAD_FOLDER, name)
    file.save(path)

    # tabular data
    if ext in ALLOWED_TABULAR:
        try:
            df = try_load_csv(path) if ext == '.csv' else try_load_excel(path)
        except Exception as e:
            return jsonify({'error': f'Table read error: {e}'}), 400
        session['temp_table'] = df.to_json(orient='records')
        session['temp_cols'] = df.columns.tolist()
        session['temp_name'] = name
        return jsonify({
            'type': 'table',
            'columns': df.columns.tolist(),
            'preview': df.head(5).to_dict(orient='records')
        })

    # geospatial data
    if ext in ALLOWED_GEO:
        try:
            gdf = try_load_geospatial(path)
        except Exception as e:
            return jsonify({'error': f'Geo read error: {e}'}), 400

        layer = os.path.splitext(name)[0]
        save_to_gpkg(layer, gdf)
        # add point markers
        for _, r in gdf.iterrows():
            geom = r.geometry
            if geom is None:
                continue
            if geom.geom_type != 'Point':
                geom = geom.centroid
            lat, lon = geom.y, geom.x
            popup = ''.join(f'<b>{c}</b>: {r[c]}<br>' for c in gdf.columns)
            add_marker(lat, lon, popup, layer)

        return jsonify({
            'type': 'map',
            'layer': layer,
            'geojson': json.loads(gdf.to_json())
        })

    return jsonify({'error': 'Unsupported format'}), 400

@app.route('/api/layer/<layer>')
def api_layer(layer):
    try:
        minx, miny, maxx, maxy = [float(request.args[k]) for k in ('minx','miny','maxx','maxy')]
    except Exception:
        return jsonify({'error': 'BBox params required'}), 400
    try:
        slice_gdf = gpd.read_file(UPLOAD_GPKG, layer=layer, bbox=(minx, miny, maxx, maxy))
    except Exception as e:
        return jsonify({'error': f'Layer slice error: {e}'}), 500
    return jsonify(json.loads(slice_gdf.to_json()))

@app.route('/api/markers')
def api_markers():
    marks = session.get('markers', [])
    feats = [{'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [m['lon'], m['lat']]}, 'properties': {'popup': m['popup'], 'layer': m['layer']}} for m in marks]
    return jsonify({'type': 'FeatureCollection', 'features': feats})

@app.route('/api/plot_table', methods=['POST'])
def api_plot_table():
    d = request.get_json()
    latc, lonc = d.get('lat_col'), d.get('lon_col')
    tj = session.get('temp_table')
    if not all([latc, lonc, tj]):
        return jsonify({'error': 'Select lat/lon'}), 400
    df = pd.read_json(tj, orient='records')
    feats = []
    for _, r in df.iterrows():
        try:
            lat, lon = float(r[latc]), float(r[lonc])
        except:
            continue
        props = r.drop([latc, lonc]).to_dict()
        feats.append({'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [lon, lat]}, 'properties': props})
    return jsonify({'type': 'map', 'geojson': {'type': 'FeatureCollection', 'features': feats}})

@app.route('/reset')
def reset():
    session.clear()
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True)
    