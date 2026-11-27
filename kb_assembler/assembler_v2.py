"""Mechanical, lossless document bundle merger; never enables retrieval."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from document_bundle_builder.builder import logical_digest, q, sha
from kb_assembler.assembler import canonical, ro, safe_file, safe_id, validate_bundle

VERSION = 'engine-kb-mechanical-v2.0'
SCHEMA = 'document-bundle-tables-v1'

def metadata(db):
    names=[r[0] for r in db.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
    return {n:[tuple(x[1:3]) for x in db.execute('pragma table_info('+q(n)+')')] for n in names}

def filtered_digest(db, table, document):
    columns=[r[1] for r in db.execute('pragma table_info('+q(table)+')') if r[1]!='bundle_document_id']
    import json as _json
    rows=[]
    for row in db.execute('select '+','.join(q(c) for c in columns)+' from '+q(table)+' where bundle_document_id=?',(document,)):
        values=[['blob',v.hex()] if isinstance(v,bytes) else ['null',None] if v is None else [type(v).__name__,v] for v in row]
        rows.append(_json.dumps(values,ensure_ascii=False,separators=(',',':'),allow_nan=False))
    return hashlib.sha256(('\n'.join(sorted(rows))).encode('utf8')).hexdigest()

def inspect_links(db, doc, pages):
    def ids(sql):return {r[0] for r in db.execute(sql)}
    text=ids('select chunk_id from text_chunks__chunks'); table=ids('select chunk_id from table_layer__table_chunks')
    blocks=ids('select block_id from text_chunks__blocks'); passports=ids('select passport_id from passport_layer__passports')
    figures=ids('select figure_id from figure_layer__figures'); regions=ids('select region_id from figure_layer__figure_regions')
    for name,sql,target in [('chunk parent','select block_id from text_chunks__chunks',blocks),('table links text','select chunk_id from text_table_links__semantic_links',text),('table links table','select table_chunk_id from text_table_links__semantic_links',table),('figure links text','select chunk_id from text_figure_links__figure_chunk_links',text),('figure links figure','select figure_id from text_figure_links__figure_chunk_links',figures),('passport mapping','select passport_id from passport_layer__passport_mapping',passports),('passport chunk','select chunk_id from passport_layer__passport_mapping',text)]:
        if ids(sql)-target:raise ValueError('broken '+name+' in '+doc)
    rows=db.execute('select kind,source_id,document_id,pages_json from bundle_search_records').fetchall()
    if len(rows)!=len({(r[0],r[1]) for r in rows}):raise ValueError('duplicate source reference within document')
    for kind,identifier,did,ps in rows:
        if did!=doc or identifier not in {'text':text,'table':table,'passport':passports}.get(kind,set()):raise ValueError('foreign search record')
        page=json.loads(ps)
        if not isinstance(page,list) or not page or any(type(x)!=int or x<1 or x>pages for x in page):raise ValueError('invalid source page')
    for did,page in db.execute('select document_id,page_no from figure_layer__figures'):
        if did!=doc or type(page)!=int or not 1<=page<=pages:raise ValueError('invalid figure provenance')
    for fid,page in db.execute('select figure_id,page_no from figure_layer__figure_regions'):
        if fid not in figures or type(page)!=int or not 1<=page<=pages:raise ValueError('invalid figure region provenance')
    for t,k in [('text_chunks__chunks','chunk_id'),('table_layer__table_chunks','chunk_id'),('passport_layer__passports','passport_id'),('figure_layer__figures','figure_id'),('figure_layer__figure_regions','region_id')]:
        count,unique=db.execute('select count(*), count(distinct '+q(k)+') from '+q(t)).fetchone()
        if count!=unique:raise ValueError('duplicate ID in '+t)
    return {'blocks':len(blocks),'text_chunks':len(text),'table_chunks':len(table),'passports':len(passports),'figures':len(figures),'regions':len(regions),'text_table_links':db.execute('select count(*) from text_table_links__semantic_links').fetchone()[0],'text_figure_links':db.execute('select count(*) from text_figure_links__figure_chunk_links').fetchone()[0]}

def assemble(engine_id,bundles,output_root,*,schema_version=SCHEMA):
    engine_id=safe_id(engine_id)
    if schema_version!=SCHEMA:raise ValueError('unsupported assembly schema')
    if not bundles:raise ValueError('at least one document bundle is required')
    docs=sorted((validate_bundle(p) for p in bundles),key=lambda x:x['doc'])
    if len({d['doc'] for d in docs})!=len(docs):raise ValueError('duplicate document_id')
    definitions=None;details=[]
    for d in docs:
        db=ro(d['path']/'knowledge.sqlite')
        try:
            structure=metadata(db)
            if definitions is None:definitions=structure
            elif definitions!=structure:raise ValueError('incompatible input SQLite schemas')
            pages=db.execute('select page_count from source_map__document_revisions where document_id=?',(d['doc'],)).fetchall()
            if len(pages)!=1 or type(pages[0][0])!=int or pages[0][0]<1:raise ValueError('invalid document pages')
            counts=inspect_links(db,d['doc'],pages[0][0]);details.append({'document_id':d['doc'],'bundle_id':d['manifest']['build_id'],'manifest_sha256':d['manifest_sha'],'pdf_sha256':d['manifest']['source_pdf_sha256'],'page_count':pages[0][0],'counts':counts})
        finally:db.close()
    identity={'assembler_version':VERSION,'assembler_sha256':sha(__file__),'schema_version':schema_version,'engine_id':engine_id,'documents':details}
    build_id=hashlib.sha256(canonical(identity)).hexdigest();base=Path(output_root).resolve()/('KB_'+engine_id);base.mkdir(parents=True,exist_ok=True);target=base/build_id
    with tempfile.TemporaryDirectory(prefix='.staging-v2-',dir=base) as tmp:
        stage=Path(tmp)/'kb';stage.mkdir();(stage/'sources').mkdir();(stage/'search').mkdir()
        out=sqlite3.connect(stage/'knowledge.sqlite');results=[];search={x:[] for x in ('text_bm25','text_dense','passport_dense','table_dense')}; resources={}
        try:
            for name,cols in definitions.items():
                if any(c=='bundle_document_id' for c,_ in cols):raise ValueError('reserved technical column collision')
                out.execute('create table '+q(name)+' (bundle_document_id TEXT NOT NULL,'+','.join(q(c)+(' '+typ if typ else '') for c,typ in cols)+')')
            for d,detail in zip(docs,details):
                root=d['path'];did=d['doc'];db=ro(root/'knowledge.sqlite');db.row_factory=sqlite3.Row
                try:
                    directory=stage/'sources'/did;directory.mkdir()
                    shutil.copyfile(root/'source.pdf',directory/'source.pdf')
                    if sha(directory/'source.pdf')!=detail['pdf_sha256']:raise ValueError('copied PDF hash mismatch')
                    if (root/'assets').is_dir():shutil.copytree(root/'assets',directory/'assets')
                    for extra in ('metadata','evaluation'):
                        if (root/extra).is_dir():shutil.copytree(root/extra,directory/extra)
                    for extra in ('assets','metadata','evaluation'):
                        if (root/extra).is_dir():
                            for file in (root/extra).rglob('*'):
                                if file.is_file() and sha(file)!=sha(safe_file(directory,file.relative_to(root).as_posix())):raise ValueError('copied resource hash mismatch')
                    for row in db.execute('select region_id,asset_path,asset_sha256 from figure_layer__figure_regions'):
                        src=safe_file(root,row['asset_path']);copied=safe_file(directory,row['asset_path'])
                        if sha(src)!=row['asset_sha256'] or sha(copied)!=row['asset_sha256']:raise ValueError('figure asset hash mismatch')
                    for name,cols in definitions.items():
                        out.executemany('insert into '+q(name)+' values ('+','.join('?' for _ in range(len(cols)+1))+')',((did,*tuple(row)) for row in db.execute('select * from '+q(name))))
                        before=logical_digest(db,name);after=filtered_digest(out,name,did)
                        if before!=after:raise ValueError('loss of source data: '+name)
                        results.append({'document_id':did,'table':name,'rows':db.execute('select count(*) from '+q(name)).fetchone()[0],'logical_sha256':before})
                    for row in db.execute('select * from bundle_search_records order by kind,source_id'):
                        x=dict(row)
                        if x['search_text'] is not None and hashlib.sha256(x['search_text'].encode('utf8')).hexdigest()!=x['search_sha256']:raise ValueError('search text hash mismatch')
                        for channel in {'text':['text_bm25','text_dense'],'passport':['passport_dense'],'table':['table_dense']}[x['kind']]:search[channel].append(x)
                finally:db.close()
            out.commit()
            if out.execute('pragma integrity_check').fetchone()[0]!='ok':raise ValueError('output SQLite integrity failed')
            logical={name:logical_digest(out,name) for name in definitions}
        finally:out.close()
        for c in search:search[c].sort(key=lambda x:(x['document_id'],x['source_id']))
        (stage/'search/search_inputs.json').write_bytes(canonical({'channels':search,'runtime_enabled':False,'filtered':False}))
        idx={'text_bm25':'not_built','text_dense':'missing_vectors','passport_dense':'missing_vectors','table_dense':'missing_vectors','runtime_enabled':False}
        (stage/'search/index_manifest.json').write_bytes(canonical(idx))
        for f in sorted((stage/'sources').rglob('*')):
            if f.is_file():resources[f.relative_to(stage).as_posix()]=sha(f)
        counts={k:sum(d['counts'][k] for d in details) for k in details[0]['counts']}
        manifest={'format':VERSION,'schema_version':schema_version,'build_id':build_id,'engine_id':engine_id,'identity':identity,'documents':details,'counts':counts,'resource_sha256':resources,'knowledge_logical_sha256':logical,'search_inputs_sha256':sha(stage/'search/search_inputs.json'),'index_manifest_sha256':sha(stage/'search/index_manifest.json'),'technical_validation':'passed','runtime_enabled':False,'indexes_built':False}
        report={'status':'technical_validation_passed','source_tables':results,'search_record_counts':{k:len(v) for k,v in search.items()},'counts':counts,'runtime_enabled':False,'indexes_built':False,'semantic_applicability_review':'not_performed'}
        (stage/'kb_manifest.json').write_bytes(canonical(manifest));(stage/'validation_report.json').write_bytes(canonical(report))
        if target.exists():
            for name in ('kb_manifest.json','validation_report.json','search/search_inputs.json','search/index_manifest.json'):
                if (target/name).read_bytes()!=(stage/name).read_bytes():raise ValueError('existing build mismatch')
            old=ro(target/'knowledge.sqlite')
            try:
                for name,digest in logical.items():
                    if logical_digest(old,name)!=digest:raise ValueError('existing database mismatch')
            finally:old.close()
            for name,digest in resources.items():
                if sha(safe_file(target,name))!=digest:raise ValueError('existing resource mismatch')
        else:os.rename(stage,target)
    return target

def resolve(kb_root,engine_id,document_id,kind,source_id):
    root=Path(kb_root).resolve(strict=True);manifest=json.loads((root/'kb_manifest.json').read_text(encoding='utf8'))
    if manifest['engine_id']!=engine_id:raise ValueError('engine mismatch')
    doc=next((d for d in manifest['documents'] if d['document_id']==document_id),None)
    if doc is None:raise ValueError('unknown document')
    db=ro(root/'knowledge.sqlite')
    try:
        if kind=='figure':
            rows=db.execute('select page_no,asset_path,asset_sha256 from figure_layer__figure_regions where bundle_document_id=? and figure_id=?',(document_id,source_id)).fetchall()
            if not rows:raise ValueError('unknown figure')
            paths=[]
            for page,path,checksum in rows:
                rel='sources/'+document_id+'/'+path.replace('\\','/');asset=safe_file(root,rel)
                if sha(asset)!=checksum or manifest['resource_sha256'].get(rel)!=checksum:raise ValueError('asset hash mismatch')
                paths.append({'page':page,'asset_path':str(asset)})
            pages=sorted({x['page'] for x in paths})
        else:
            row=db.execute('select pages_json from bundle_search_records where bundle_document_id=? and kind=? and source_id=?',(document_id,kind,source_id)).fetchone()
            if row is None:raise ValueError('unknown source')
            pages=json.loads(row[0]);paths=[]
        pdf=safe_file(root,'sources/'+document_id+'/source.pdf')
        if sha(pdf)!=doc['pdf_sha256']:raise ValueError('PDF hash mismatch')
        return {'document_id':document_id,'pdf_path':str(pdf),'pdf_pages':pages,'regions':paths,'note':'source resolution only; no runtime authorization'}
    finally:db.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',type=Path,required=True);args=parser.parse_args()
    cfg=json.loads(args.config.read_text(encoding='utf8'))
    print(assemble(cfg['engine_id'],cfg['bundles'],cfg['output'],schema_version=cfg.get('schema_version',SCHEMA)))
if __name__=='__main__':main()
