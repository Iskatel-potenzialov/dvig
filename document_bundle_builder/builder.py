"""Lossless, offline packaging of seven prepared SQLite layers for one document."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

VERSION = 'document-bundle-v1.0'
LAYERS = ('source_map', 'text_chunks', 'table_layer', 'figure_layer',
          'text_table_links', 'text_figure_links', 'passport_layer')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def jbytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf8')


def q(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def connect_ro(path):
    return sqlite3.connect(path.resolve(strict=True).as_uri() + '?mode=ro', uri=True)


def input_files(root):
    source = root / 'source.pdf'
    prepared = root / 'prepared'
    required = [source, *(prepared / (x + '.sqlite') for x in LAYERS)]
    for directory in ('assets', 'metadata', 'evaluation'):
        d = prepared / directory
        if not d.is_dir():
            raise ValueError('Missing input directory: ' + str(d))
        if any(x.is_symlink() for x in d.rglob('*')):
            raise ValueError('Symbolic links are not accepted as bundle inputs')
        required.extend(x for x in d.rglob('*') if x.is_file())
    if any(not f.is_file() or f.is_symlink() for f in required):
        raise ValueError('Missing or unsafe required input file')
    return sorted(required, key=lambda x: x.relative_to(root).as_posix())


def inspect(root, document_id):
    files = input_files(root)
    hashes = {f.relative_to(root).as_posix(): sha(f) for f in files}
    registry_path = root / 'prepared/metadata/prepared_file_registry.json'
    if not registry_path.is_file():
        raise ValueError('Input registry is missing')
    for item in json.loads(registry_path.read_text(encoding='utf8')):
        if hashes.get(item['path']) != item['sha256']:
            raise ValueError('Input registry hash mismatch: ' + item['path'])
    dbs = {}
    try:
        for layer in LAYERS:
            db = connect_ro(root / 'prepared' / (layer + '.sqlite'))
            dbs[layer] = db
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('SQLite integrity check failed: ' + layer)
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('SQLite foreign key check failed: ' + layer)
        def ids(layer, sql):
            return {r[0] for r in dbs[layer].execute(sql)}
        docs = ids('source_map', 'SELECT document_id FROM documents')
        if docs != {document_id}:
            raise ValueError('Unexpected document_id in source map')
        for name, db in dbs.items():
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                cols = [x[1] for x in db.execute('PRAGMA table_info(' + q(table) + ')')]
                if 'document_id' in cols:
                    values = {r[0] for r in db.execute('SELECT DISTINCT document_id FROM ' + q(table)) if r[0] is not None}
                    if values - {document_id}:
                        raise ValueError('Foreign document in ' + name + '.' + table)
        revision = dbs['source_map'].execute('SELECT pdf_sha256,page_count FROM document_revisions WHERE document_id=?', (document_id,)).fetchall()
        if len(revision) != 1 or revision[0][0] != hashes['source.pdf']:
            raise ValueError('PDF differs from source map revision')
        page_count = revision[0][1]
        chunks = ids('text_chunks', 'SELECT chunk_id FROM chunks')
        blocks = ids('text_chunks', 'SELECT block_id FROM blocks')
        tables = ids('table_layer', 'SELECT chunk_id FROM table_chunks')
        figures = ids('figure_layer', 'SELECT figure_id FROM figures')
        regions = ids('figure_layer', 'SELECT region_id FROM figure_regions')
        passports = ids('passport_layer', 'SELECT passport_id FROM passports')
        checks = [('text_chunks','SELECT block_id FROM chunks',blocks),
                  ('text_table_links','SELECT chunk_id FROM semantic_links',chunks),
                  ('text_table_links','SELECT table_chunk_id FROM semantic_links',tables),
                  ('text_figure_links','SELECT chunk_id FROM figure_chunk_links',chunks),
                  ('text_figure_links','SELECT figure_id FROM figure_chunk_links',figures),
                  ('text_figure_links','SELECT region_id FROM figure_link_regions',regions),
                  ('passport_layer','SELECT passport_id FROM passport_mapping',passports),
                  ('passport_layer','SELECT chunk_id FROM passport_mapping',chunks),
                  ('passport_layer','SELECT passport_id FROM anchors',passports),
                  ('passport_layer','SELECT passport_id FROM aliases',passports),
                  ('passport_layer','SELECT passport_id FROM questions',passports),
                  ('passport_layer','SELECT passport_id FROM table_navigation_candidate',passports),
                  ('passport_layer','SELECT chunk_id FROM table_navigation_candidate',chunks),
                  ('passport_layer','SELECT table_chunk_id FROM table_navigation_candidate',tables)]
        for name, sql, targets in checks:
            if ids(name, sql) - targets:
                raise ValueError('Unresolved layer reference: ' + name + ' ' + sql)
        text_hashes = dict(dbs['text_chunks'].execute('SELECT chunk_id,raw_sha256 FROM chunks'))
        table_hashes = dict(dbs['table_layer'].execute('SELECT chunk_id,raw_sha256 FROM table_chunks'))
        for name, sql, expected in [('text_table_links','SELECT chunk_id,raw_sha256 FROM text_endpoints',text_hashes),
                                    ('text_figure_links','SELECT chunk_id,raw_sha256 FROM text_endpoints',text_hashes),
                                    ('text_table_links','SELECT table_chunk_id,raw_sha256 FROM table_endpoints',table_hashes),
                                    ('passport_layer','SELECT chunk_id,source_sha256 FROM source_chunks',text_hashes)]:
            if any(expected.get(key) != value for key, value in dbs[name].execute(sql)):
                raise ValueError('Endpoint hash mismatch: ' + name)
        for text, digest, pages in dbs['text_chunks'].execute('SELECT raw_text,raw_sha256,pages_json FROM chunks'):
            if hashlib.sha256(text.encode('utf8')).hexdigest() != digest or not json.loads(pages) or any(not isinstance(x,int) or x < 1 or x > page_count for x in json.loads(pages)):
                raise ValueError('Invalid chunk hash or pages')
        for text, digest, page in dbs['table_layer'].execute('SELECT raw_text,raw_sha256,page_no FROM table_chunks'):
            if hashlib.sha256(text.encode('utf8')).hexdigest() != digest or not isinstance(page,int) or not 1 <= page <= page_count:
                raise ValueError('Invalid table hash or page')
        assets = []
        for region, path, digest, page in dbs['figure_layer'].execute('SELECT region_id,asset_path,asset_sha256,page_no FROM figure_regions'):
            rel = Path(path)
            if rel.is_absolute() or '..' in rel.parts or not path.replace('\\','/').startswith('assets/'):
                raise ValueError('Invalid figure path')
            file = root / 'prepared' / rel
            if not file.is_file() or sha(file) != digest or not 1 <= page <= page_count:
                raise ValueError('Missing/corrupt PNG or invalid page: ' + region)
            assets.append(path.replace('\\','/'))
        if len(assets) != len(set(assets)):
            raise ValueError('Duplicate region asset path')
        counts = {key: dbs[layer].execute('SELECT count(*) FROM ' + q(table)).fetchone()[0] for key, layer, table in (
            ('blocks','text_chunks','blocks'),('chunks','text_chunks','chunks'),('table_chunks','table_layer','table_chunks'),
            ('passports','passport_layer','passports'),('figures','figure_layer','figures'),('regions','figure_layer','figure_regions'),
            ('text_table_links','text_table_links','semantic_links'),('text_figure_links','text_figure_links','figure_chunk_links'))}
        warnings = []
        null_regions = dbs['figure_layer'].execute('SELECT region_id FROM figure_regions WHERE source_object_id IS NULL').fetchall()
        if null_regions:
            warnings.append({'type':'region_source_object_missing','ids':[r[0] for r in null_regions], 'classification':'provenance_unresolved'})
        fingerprints = dict(dbs['figure_layer'].execute('SELECT figure_id,source_fingerprint FROM figures'))
        diff = [x for x, h in dbs['text_figure_links'].execute('SELECT figure_id,source_fingerprint FROM figure_endpoints') if fingerprints.get(x) != h]
        if diff:
            warnings.append({'type':'figure_source_fingerprint_mismatch','ids':diff,'classification':'provenance_unresolved'})
        missing = []
        for layer in ('source_map','text_chunks','table_layer'):
            db = dbs[layer]
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                for col in [x[1] for x in db.execute('PRAGMA table_info(' + q(table) + ')') if x[1] in ('asset_path','crop_path','render_asset')]:
                    for (path,) in db.execute('SELECT DISTINCT ' + q(col) + ' FROM ' + q(table) + ' WHERE ' + q(col) + ' IS NOT NULL AND ' + q(col) + " != ''"):
                        if not (root / 'prepared' / path).is_file():
                            missing.append({'layer':layer,'table':table,'column':col,'path':path,'classification':'unresolved_historical_reference'})
        if missing:
            warnings.append({'type':'historical_paths_unresolved','references':sorted(missing,key=lambda x:(x['layer'],x['table'],x['column'],x['path'])), 'classification':'unknown_not_assumed_required'})
        unlinked = dbs['text_table_links'].execute('SELECT count(*) FROM table_endpoints WHERE table_chunk_id NOT IN (SELECT table_chunk_id FROM semantic_links)').fetchone()[0]
        return {'hashes':hashes,'counts':counts,'page_count':page_count,'warnings':warnings,'unlinked_table_records':unlinked}
    finally:
        for db in dbs.values():
            db.close()


def logical_digest(db, table):
    rows = []
    for row in db.execute('SELECT * FROM ' + q(table)):
        parts = []
        for value in row:
            if isinstance(value, bytes):
                parts.append(['blob', value.hex()])
            elif value is None:
                parts.append(['null', None])
            else:
                parts.append([type(value).__name__, value])
        rows.append(json.dumps(parts, ensure_ascii=False, separators=(',', ':'), allow_nan=False))
    return hashlib.sha256(('\n'.join(sorted(rows))).encode('utf8')).hexdigest()


def create_database(root, target):
    out = sqlite3.connect(target)
    summary = []
    try:
        out.execute('CREATE TABLE bundle_source_schema (layer TEXT NOT NULL, source_name TEXT NOT NULL, output_name TEXT NOT NULL, source_ddl TEXT, row_count INTEGER NOT NULL, logical_sha256 TEXT NOT NULL, PRIMARY KEY(layer,source_name))')
        for layer in LAYERS:
            source = root / 'prepared' / (layer + '.sqlite')
            inp = connect_ro(source)
            try:
                tables = inp.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
                for name, ddl in tables:
                    dest = layer + '__' + name
                    cols = inp.execute('PRAGMA table_info(' + q(name) + ')').fetchall()
                    # Preserve values and column order exactly; original DDL is retained separately.
                    definitions = ','.join(q(c[1]) + (' ' + c[2] if c[2] else '') for c in cols)
                    out.execute('CREATE TABLE ' + q(dest) + ' (' + definitions + ')')
                    placeholders = ','.join('?' for _ in cols)
                    out.executemany('INSERT INTO ' + q(dest) + ' VALUES (' + placeholders + ')', inp.execute('SELECT * FROM ' + q(name)))
                    n = inp.execute('SELECT count(*) FROM ' + q(name)).fetchone()[0]
                    digest = logical_digest(inp, name)
                    if logical_digest(out, dest) != digest:
                        raise ValueError('Logical copy mismatch: ' + layer + '.' + name)
                    out.execute('INSERT INTO bundle_source_schema VALUES (?,?,?,?,?,?)', (layer,name,dest,ddl,n,digest))
                    summary.append({'layer':layer,'table':name,'output':dest,'rows':n,'logical_sha256':digest})
            finally:
                inp.close()
        out.execute('''CREATE TABLE bundle_search_records (
            kind TEXT NOT NULL, source_id TEXT NOT NULL, document_id TEXT NOT NULL,
            search_text TEXT, search_sha256 TEXT, pages_json TEXT, engine_ids_json TEXT,
            scope_conditions_json TEXT, future_candidate INTEGER, runtime_enabled INTEGER,
            source_layer TEXT NOT NULL, PRIMARY KEY(kind,source_id))''')
        queries = [
            ('text','text_chunks', '''SELECT c.chunk_id,c.document_id,c.search_text,c.pages_json,c.engine_ids_json,
                 c.scope_conditions_json,r.future_index_candidate,r.actual_retrieval_enabled
                 FROM text_chunks__chunks c JOIN text_chunks__chunk_readiness r ON c.chunk_id=r.chunk_id'''),
            ('table','table_layer', '''SELECT c.chunk_id,?,c.search_text,json_array(c.page_no),l.engine_ids_json,
                 l.scope_conditions_json,r.future_candidate,r.runtime_eligible
                 FROM table_layer__table_chunks c JOIN table_layer__logical_rows l ON c.logical_id=l.logical_id
                 JOIN table_layer__final_readiness_03_4 r ON r.logical_id=c.logical_id'''),
            ('passport','passport_layer', '''SELECT passport_id,document_id,search_text,source_pages_json,
                 engine_ids_json,conditions_json,eligible_candidate,actual_runtime_enabled FROM passport_layer__passports''')]
        doc_id = out.execute('SELECT document_id FROM source_map__documents').fetchone()[0]
        for kind, layer, query in queries:
            args = (doc_id,) if kind == 'table' else ()
            for identifier, doc, text, pages, engines, conditions, candidate, runtime in out.execute(query, args).fetchall():
                digest = hashlib.sha256(text.encode('utf8')).hexdigest() if text is not None else None
                out.execute('INSERT INTO bundle_search_records VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                            (kind,identifier,doc,text,digest,pages,engines,conditions,candidate,runtime,layer))
        summary.append({'layer':'bundle','table':'search_records','output':'bundle_search_records',
                        'rows':out.execute('SELECT count(*) FROM bundle_search_records').fetchone()[0],
                        'logical_sha256':logical_digest(out,'bundle_search_records')})
        out.commit()
        if out.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Output SQLite integrity check failed')
    finally:
        out.close()
    return summary


def build(root, output_root, document_id):
    root, output_root = Path(root).resolve(strict=True), Path(output_root).resolve()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', document_id) or document_id in ('.','..'):
        raise ValueError('Invalid document_id')
    if output_root == root or output_root.is_relative_to(root):
        raise ValueError('Output may not be inside input')
    result = inspect(root, document_id)
    identity = {'builder':VERSION,'builder_sha256':sha(__file__),'document_id':document_id,'input_sha256':result['hashes']}
    build_id = hashlib.sha256(jbytes(identity)).hexdigest()
    base = output_root / document_id
    base.mkdir(parents=True, exist_ok=True)
    target = base / ('build_' + build_id)
    with tempfile.TemporaryDirectory(prefix='.bundle-', dir=base) as temporary:
        temp = Path(temporary) / 'bundle'
        temp.mkdir()
        shutil.copyfile(root / 'source.pdf', temp / 'source.pdf')
        for folder in ('assets','metadata','evaluation'):
            shutil.copytree(root / 'prepared' / folder, temp / folder)
        schema = create_database(root, temp / 'knowledge.sqlite')
        if inspect(root, document_id)['hashes'] != result['hashes']:
            raise ValueError('Input changed during build')
        manifest = {'format':VERSION,'build_id':build_id,'identity':identity,'logical_tables':schema,
                    'publication':'offline_document_bundle_only','runtime_enabled':False,
                    'source_pdf_sha256':result['hashes']['source.pdf']}
        validation = {'status':'passed_with_warnings' if result['warnings'] else 'passed',
                      'counts':result['counts'],'page_count_from_source_map':result['page_count'],
                      'unlinked_table_records':result['unlinked_table_records'],'warnings':result['warnings'],
                      'runtime_enabled':False,'semantic_audit':'not_performed','object_specific_dependency_blocks':'not_inferred',
                      'source_table_logical_hashes_checked':len(schema)}
        (temp / 'manifest.json').write_bytes(jbytes(manifest))
        (temp / 'validation_report.json').write_bytes(jbytes(validation))
        if target.exists():
            if (target / 'manifest.json').read_bytes() != (temp / 'manifest.json').read_bytes() or (target / 'validation_report.json').read_bytes() != (temp / 'validation_report.json').read_bytes():
                raise ValueError('Existing build conflicts with deterministic output')
            existing = sqlite3.connect((target / 'knowledge.sqlite').resolve(strict=True).as_uri() + '?mode=ro', uri=True)
            try:
                if any(logical_digest(existing, x['output']) != x['logical_sha256'] for x in schema):
                    raise ValueError('Existing build data does not match manifest')
            finally:
                existing.close()
            if sha(target / 'source.pdf') != result['hashes']['source.pdf'] or any(sha(target / folder / path.relative_to(root / 'prepared' / folder)) != result['hashes'][path.relative_to(root).as_posix()] for folder in ('assets','metadata','evaluation') for path in (root / 'prepared' / folder).rglob('*') if path.is_file()):
                raise ValueError('Existing build assets differ')
        else:
            os.rename(temp, target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--document-id', required=True)
    args = parser.parse_args()
    print(build(args.input, args.output, args.document_id))


if __name__ == '__main__':
    main()
