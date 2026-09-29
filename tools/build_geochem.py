"""Build a compact gold/arsenic anomaly file for Sentinel Gold Hunter.

Sources (USGS, public domain):
  - National Geochemical Survey (geochem.csv)            -> NGS
  - NURE-HSSR stream sediments (nuresed.csv)             -> NURE
  - NGDB heavy-mineral concentrates (ngdbconc/*.csv)     -> CONC
  - NGDB sediments (ngdbsed/*.csv)                       -> SED

Output: geochem.json with
  pts:  [lat, lng, src, medium, auScore, asScore, auPpb, asPpm]  (anomalous samples only)
  cov:  {"lat10,lng10": [samplesWithAu, samplesWithAs]}          (0.1 deg coverage grid)
Scores are 0..1 and relative within each source+medium, because detection
limits and methods differ wildly across 60 years of USGS sampling.
"""
import json, math, sys
import numpy as np
import pandas as pd

SRC = {'NGS': 0, 'NURE': 1, 'CONC': 2, 'SED': 3}
MED = {'sediment': 0, 'concentrate': 1, 'soil': 2, 'other': 3}


def med_of(s):
    s = str(s).lower()
    if 'conc' in s or 'pan' in s:
        return 'concentrate'
    if 'soil' in s:
        return 'soil'
    if 'sed' in s or 'stream' in s:
        return 'sediment'
    return 'other'


frames = []

# ---- NGS: several Au/As columns by method; take best detected value ----
ngs = pd.read_csv('geochem.csv', low_memory=False, encoding='latin-1')
# Per USGS OFR 2004-1001 metadata: every AU_<method> column is ppb, every AS_<method> is ppm.
# (AU_JOB / AS_JOB are lab job numbers, not assays.) Negative = below detection limit.
AU_PRI = ['AU_AA', 'AU_INAA', 'AU_ICP10', 'AU_ICP40', 'AU_NURE']
AS_PRI = ['AS_AA', 'AS_INAA', 'AS_ICP40', 'AS_ICP10', 'AS_NURE']
def pick(cols):
    val = pd.Series(np.nan, index=ngs.index); meth = pd.Series('', index=ngs.index); tested = pd.Series(False, index=ngs.index)
    for c in cols:
        v = pd.to_numeric(ngs[c], errors='coerce')
        tested |= v.notna()
        take = meth.eq('') & v.notna()      # first method that analysed the sample decides
        meth[take] = c.split('_')[1]
        val[take] = v[take].where(v[take] > 0)
    return val, meth, tested
au_v, au_m, au_t = pick(AU_PRI)
as_v, as_m, as_t = pick(AS_PRI)
frames.append(pd.DataFrame({
    'lat': pd.to_numeric(ngs['LATITUDE'], errors='coerce'),
    'lng': pd.to_numeric(ngs['LONGITUDE'], errors='coerce'),
    'src': 'NGS', 'medium': ngs['MEDIUM'].map(med_of) if 'MEDIUM' in ngs else 'sediment',
    'au_ppb': au_v, 'au_tested': au_t, 'au_method': au_m,
    'as_ppm': as_v, 'as_tested': as_t, 'as_method': as_m}))
print('NGS', len(ngs), 'au tested', int(au_t.sum()), 'au detected', int(au_v.notna().sum()), au_m.value_counts().to_dict())

# ---- NURE: au_ppm, as_ppm; negatives = below detection ----
nure = pd.read_csv('nuresed.csv', low_memory=False, usecols=['latitude', 'longitude', 'smpmedia', 'au_ppm', 'as_ppm'])
nau = pd.to_numeric(nure['au_ppm'], errors='coerce'); nas = pd.to_numeric(nure['as_ppm'], errors='coerce')
frames.append(pd.DataFrame({
    'lat': pd.to_numeric(nure['latitude'], errors='coerce'), 'lng': pd.to_numeric(nure['longitude'], errors='coerce'),
    'src': 'NURE', 'medium': nure['smpmedia'].map(med_of),
    'au_ppb': nau.where(nau > 0) * 1000.0, 'au_tested': nau.notna(),
    'as_ppm': nas.where(nas > 0), 'as_tested': nas.notna()}))
print('NURE', len(nure), 'au tested', int(nau.notna().sum()), 'au detected', int((nau > 0).sum()), 'as tested', int(nas.notna().sum()))


def ngdb(folder, tag):
    want = ['lab_id', 'lat_wgs84', 'long_wgs84', 'primary_class', 'secondary_class', 'specific_name']
    main = pd.read_csv(f'{folder}/main.csv', low_memory=False, usecols=lambda c: c in want)
    for c in want:
        if c not in main: main[c] = ''
    vals = []
    for chunk in pd.read_csv(f'{folder}/bestvalue.csv', usecols=['lab_id', 'species', 'unit', 'qvalue'], chunksize=2_000_000):
        chunk = chunk[chunk['species'].isin(['Au', 'As'])]
        vals.append(chunk)
    v = pd.concat(vals)
    v['qvalue'] = pd.to_numeric(v['qvalue'], errors='coerce')
    unit = v['unit'].str.lower()
    factor_au = np.select([unit == 'ppm', unit == 'ppb', unit == 'pct'], [1000.0, 1.0, 1e7], np.nan)
    factor_as = np.select([unit == 'ppm', unit == 'ppb', unit == 'pct'], [1.0, 1e-3, 1e4], np.nan)
    v['val'] = np.where(v['species'] == 'Au', v['qvalue'] * factor_au, v['qvalue'] * factor_as)
    # one best value per lab_id+species: prefer max positive, remember "tested"
    g = v.groupby(['lab_id', 'species'])['val']
    pos = v[v['val'] > 0].groupby(['lab_id', 'species'])['val'].max().unstack()
    tested = g.size().unstack().notna()
    m = main.drop_duplicates('lab_id').set_index('lab_id')
    out = pd.DataFrame(index=m.index)
    out['lat'] = pd.to_numeric(m['lat_wgs84'], errors='coerce')
    out['lng'] = pd.to_numeric(m['long_wgs84'], errors='coerce')
    out['src'] = tag
    out['medium'] = (m['secondary_class'].fillna('') + ' ' + m['specific_name'].fillna('') + ' ' + m['primary_class'].fillna('')).map(med_of)
    out['au_ppb'] = pos.get('Au').reindex(out.index) if 'Au' in pos else np.nan
    out['as_ppm'] = pos.get('As').reindex(out.index) if 'As' in pos else np.nan
    out['au_tested'] = tested.get('Au', pd.Series(False, index=tested.index)).reindex(out.index).fillna(False)
    out['as_tested'] = tested.get('As', pd.Series(False, index=tested.index)).reindex(out.index).fillna(False)
    print(tag, len(out), 'au tested', int(out['au_tested'].sum()), 'au detected', int(out['au_ppb'].notna().sum()))
    return out.reset_index(drop=True)


frames.append(ngdb('ngdbconc', 'CONC'))
frames.append(ngdb('ngdbsed', 'SED'))

df = pd.concat(frames, ignore_index=True)
df = df[df['lat'].between(17, 72) & df['lng'].between(-180, -60)]
df['au_tested'] = df['au_tested'].astype(bool); df['as_tested'] = df['as_tested'].astype(bool)
print('total US samples', len(df))

# ---- Scores: relative within source + medium ----
df['au_score'] = 0.0
df['as_score'] = 0.0
df['au_method'] = df.get('au_method', pd.Series('', index=df.index)).fillna('')
df['as_method'] = df.get('as_method', pd.Series('', index=df.index)).fillna('')
# Gold: rank each sample against EVERY sample tested by the same source + medium + method,
# non-detects included. Only the upper tail (top ~12%) scores, so a faint detection where
# half the samples detect gold barely registers, while any detection in a group where gold
# is rarely found (e.g. old emission-spec pan concentrates) scores high.
for key, grp in df.groupby(['src', 'medium', 'au_method']):
    t = grp.loc[grp['au_tested'], 'au_ppb'].fillna(0)
    if len(t) > 20:
        r = t.rank(pct=True, method='max')
        sc = ((r - 0.88) / 0.12).clip(lower=0)
        sc[t <= 0] = 0
        df.loc[t.index, 'au_score'] = sc.values
# Arsenic: only the upper tail of each group is anomalous (90th pct -> 0, max -> 1).
for key, grp in df.groupby(['src', 'medium', 'as_method']):
    asv = grp.loc[grp['as_tested'], 'as_ppm'].fillna(0)
    if len(asv) > 20:
        r = asv.rank(pct=True)
        df.loc[asv.index, 'as_score'] = ((r - 0.90) / 0.10).clip(lower=0).values

anom = df[(df['au_score'] > 0.05) | (df['as_score'] > 0.2)].copy()
print('anomalous samples kept', len(anom), 'au', int((anom.au_score > 0).sum()), 'as', int((anom.as_score > 0.2).sum()))

def r4(x):
    return None if (x is None or (isinstance(x, float) and math.isnan(x))) else round(float(x), 4)

pts = []
for row in anom.itertuples(index=False):
    pts.append([round(row.lat, 4), round(row.lng, 4), SRC[row.src], MED[row.medium],
                round(row.au_score, 2), round(row.as_score, 2),
                None if pd.isna(row.au_ppb) else round(float(row.au_ppb), 1),
                None if pd.isna(row.as_ppm) else round(float(row.as_ppm), 1)])

# ---- Coverage grid: where gold / arsenic was actually tested (negative evidence) ----
df['k'] = (np.floor(df['lat'] * 10).astype(int)).astype(str) + ',' + (np.floor(df['lng'] * 10).astype(int)).astype(str)
cov_au = df[df['au_tested']].groupby('k').size()
cov_as = df[df['as_tested']].groupby('k').size()
keys = sorted(set(cov_au.index) | set(cov_as.index))
cov = {k: [int(cov_au.get(k, 0)), int(cov_as.get(k, 0))] for k in keys}

out = {
    'v': 1,
    'built': pd.Timestamp.utcnow().strftime('%Y-%m-%d'),
    'fields': ['lat', 'lng', 'src', 'medium', 'auScore', 'asScore', 'auPpb', 'asPpm'],
    'src': list(SRC.keys()), 'medium': list(MED.keys()),
    'pts': pts, 'cov': cov,
}
with open('geochem.json', 'w') as f:
    json.dump(out, f, separators=(',', ':'))
print('cov cells', len(cov))
